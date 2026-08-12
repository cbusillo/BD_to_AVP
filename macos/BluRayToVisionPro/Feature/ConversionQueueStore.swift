import Foundation
import SwiftUI

@MainActor
final class ConversionQueueStore: ObservableObject {
    @Published private(set) var document: ConversionQueueDocument
    @Published private(set) var loadErrorMessage: String?
    @Published private(set) var writesBlocked = false

    private let fileURL: URL?
    private let fileManager: FileManager
    private let dataMover: (URL, URL) throws -> Void
    private let writer: ConversionQueueFileWriter?
    private var nextGeneration = 0

    init(
        fileURL: URL? = nil,
        fileManager: FileManager = .default,
        dataWriter: @escaping @Sendable (Data, URL) throws -> Void = { data, url in
            try data.write(to: url, options: .atomic)
        },
        dataMover: ((URL, URL) throws -> Void)? = nil,
        inMemory: Bool = false
    ) {
        self.fileManager = fileManager
        self.dataMover = dataMover ?? { sourceURL, destinationURL in
            try fileManager.moveItem(at: sourceURL, to: destinationURL)
        }
        if inMemory {
            self.fileURL = nil
            writer = nil
            document = ConversionQueueDocument()
            return
        }

        let resolvedURL = fileURL ?? Self.defaultFileURL(fileManager: fileManager)
        self.fileURL = resolvedURL
        writer = ConversionQueueFileWriter(fileURL: resolvedURL, dataWriter: dataWriter)
        document = ConversionQueueDocument()
        load(from: resolvedURL)
    }

    static func inMemory() -> ConversionQueueStore {
        ConversionQueueStore(inMemory: true)
    }

    var items: [DurableConversionQueueItem] {
        document.items
    }

    func replaceItems(_ items: [DurableConversionQueueItem]) async throws {
        try validate(items)
        let nextDocument = ConversionQueueDocument(items: items)
        try await persist(nextDocument)
    }

    func mutateItems(_ mutation: (inout [DurableConversionQueueItem]) throws -> Void) async throws {
        var items = document.items
        try mutation(&items)
        try await replaceItems(items)
    }

    private func persist(_ nextDocument: ConversionQueueDocument) async throws {
        guard !writesBlocked else {
            throw ConversionQueueStoreError.recoveryRequired
        }
        guard let writer else {
            document = nextDocument
            loadErrorMessage = nil
            return
        }

        let encoder = Self.encoder()
        let data: Data
        do {
            data = try encoder.encode(nextDocument)
        } catch {
            throw ConversionQueueStoreError.invalidDocument
        }

        nextGeneration += 1
        let generation = nextGeneration
        do {
            let didWrite = try await writer.write(data, generation: generation)
            if didWrite, generation == nextGeneration {
                document = nextDocument
                loadErrorMessage = nil
            }
        } catch {
            throw ConversionQueueStoreError.writeFailed
        }
    }

    private func load(from fileURL: URL) {
        guard fileManager.fileExists(atPath: fileURL.path) else {
            return
        }
        do {
            let data = try Data(contentsOf: fileURL)
            let version = try JSONDecoder().decode(ConversionQueueDocumentVersion.self, from: data).version
            guard version <= ConversionQueueDocument.currentVersion else {
                writesBlocked = true
                loadErrorMessage = "Queue data version \(version) is newer than this app. Queue changes are disabled to protect it."
                return
            }
            guard version == ConversionQueueDocument.currentVersion else {
                throw ConversionQueueStoreError.unsupportedVersion(version)
            }
            let loadedDocument = try Self.decoder().decode(ConversionQueueDocument.self, from: data)
            try validate(loadedDocument.items)
            document = loadedDocument.restoredAfterLaunch()
        } catch let error as ConversionQueueStoreError {
            if case let .unsupportedVersion(version) = error {
                writesBlocked = true
                loadErrorMessage = "Queue data version \(version) is not supported. Queue changes are disabled to protect it."
                return
            }
            recoverFromUnreadableFile()
        } catch {
            recoverFromUnreadableFile()
        }
    }

    private func validate(_ items: [DurableConversionQueueItem]) throws {
        var identifiers = Set<UUID>()
        var ordinals = Set<Int>()
        for item in items {
            guard item.ordinal >= 0,
                  identifiers.insert(item.id).inserted,
                  ordinals.insert(item.ordinal).inserted,
                  ConversionSourceKind(rawValue: item.intent.source.kind) != nil,
                  !item.intent.source.path.isEmpty,
                  !item.intent.profile.name.isEmpty,
                  !item.intent.destinationPath.isEmpty
            else {
                throw ConversionQueueStoreError.invalidDocument
            }
            if item.state == .attention, item.decision == nil {
                throw ConversionQueueStoreError.invalidDocument
            }
            if item.state == .completed, item.result == nil {
                throw ConversionQueueStoreError.invalidDocument
            }
        }
    }

    private func recoverFromUnreadableFile() {
        guard let fileURL else {
            return
        }
        if let recoveryURL = preserveUnreadableFile(fileURL) {
            document = ConversionQueueDocument()
            loadErrorMessage = "Queue data could not be loaded. The original file was preserved as \(recoveryURL.lastPathComponent)."
        } else {
            writesBlocked = true
            loadErrorMessage = "Queue data could not be loaded or preserved. Queue changes are disabled to protect the original file."
        }
    }

    private func preserveUnreadableFile(_ fileURL: URL) -> URL? {
        var recoveryURL = fileURL.appendingPathExtension("corrupt")
        var suffix = 2
        while fileManager.fileExists(atPath: recoveryURL.path) {
            recoveryURL = fileURL.appendingPathExtension("corrupt-\(suffix)")
            suffix += 1
        }
        do {
            try dataMover(fileURL, recoveryURL)
            return recoveryURL
        } catch {
            return nil
        }
    }

    private static func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }

    private static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        decoder.userInfo[.requiresVideoQualityIntent] = true
        return decoder
    }

    private static func defaultFileURL(fileManager: FileManager) -> URL {
        let applicationSupportURL = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        return applicationSupportURL
            .appendingPathComponent("3D Blu-ray to Vision Pro", isDirectory: true)
            .appendingPathComponent("queue.json")
    }
}

private actor ConversionQueueFileWriter {
    private let fileURL: URL
    private let dataWriter: @Sendable (Data, URL) throws -> Void
    private var latestGeneration = 0

    init(fileURL: URL, dataWriter: @escaping @Sendable (Data, URL) throws -> Void) {
        self.fileURL = fileURL
        self.dataWriter = dataWriter
    }

    func write(_ data: Data, generation: Int) throws -> Bool {
        guard generation >= latestGeneration else {
            return false
        }
        latestGeneration = generation
        try FileManager.default.createDirectory(
            at: fileURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try dataWriter(data, fileURL)
        return true
    }
}

enum ConversionQueueStoreError: LocalizedError, Equatable {
    case invalidDocument
    case recoveryRequired
    case unsupportedVersion(Int)
    case writeFailed

    var errorDescription: String? {
        switch self {
        case .invalidDocument:
            "The queue contains invalid data."
        case .recoveryRequired:
            "Queue changes are disabled until the original queue data can be protected."
        case let .unsupportedVersion(version):
            "Queue data version \(version) is not supported."
        case .writeFailed:
            "The queue could not be saved."
        }
    }
}

private struct ConversionQueueDocumentVersion: Decodable {
    let version: Int
}

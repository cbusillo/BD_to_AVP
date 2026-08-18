import Foundation
import SwiftUI

enum ResolutionMemoryScope: Codable, Equatable, Hashable, Identifiable {
    case profile(String)
    case sourceKind(ConversionSourceKind)
    case global

    var id: String {
        switch self {
        case let .profile(identifier): "profile:\(identifier)"
        case let .sourceKind(kind): "source:\(kind.rawValue)"
        case .global: "global"
        }
    }

    var precedence: Int {
        switch self {
        case .profile: 3
        case .sourceKind: 2
        case .global: 1
        }
    }

    private enum CodingKeys: String, CodingKey { case kind, profileID, sourceKind }
    private enum Kind: String, Codable { case profile, sourceKind, global }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        switch try values.decode(Kind.self, forKey: .kind) {
        case .profile:
            self = .profile(try values.decode(String.self, forKey: .profileID))
        case .sourceKind:
            self = .sourceKind(try values.decode(ConversionSourceKind.self, forKey: .sourceKind))
        case .global:
            self = .global
        }
    }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case let .profile(identifier):
            try values.encode(Kind.profile, forKey: .kind)
            try values.encode(identifier, forKey: .profileID)
        case let .sourceKind(sourceKind):
            try values.encode(Kind.sourceKind, forKey: .kind)
            try values.encode(sourceKind, forKey: .sourceKind)
        case .global:
            try values.encode(Kind.global, forKey: .kind)
        }
    }
}

struct ResolutionMemoryEntry: Codable, Equatable, Identifiable {
    let conflictID: String
    let resolutionID: String
    let scope: ResolutionMemoryScope
    let mappingVersion: Int

    var id: String { "\(conflictID)|\(scope.id)" }
}

struct ResolutionMemorySuggestion: Equatable {
    let entry: ResolutionMemoryEntry
    let isStale: Bool

    var staleExplanation: String? {
        isStale ? "Quality options changed after an update. Choose a current option before applying this suggestion." : nil
    }
}

struct ResolutionMemoryDocument: Codable, Equatable {
    static let currentVersion = 1

    let version: Int
    var entries: [ResolutionMemoryEntry]

    init(version: Int = currentVersion, entries: [ResolutionMemoryEntry] = []) {
        self.version = version
        self.entries = entries
    }
}

@MainActor
final class ResolutionMemoryStore: ObservableObject {
    @Published private(set) var entries: [ResolutionMemoryEntry]
    @Published private(set) var loadErrorMessage: String?
    @Published private(set) var writesBlocked = false

    private let fileURL: URL?
    private let fileManager: FileManager
    private let dataReader: (URL) throws -> Data
    private let dataWriter: (Data, URL) throws -> Void

    init(
        fileURL: URL? = nil,
        fileManager: FileManager = .default,
        dataReader: @escaping (URL) throws -> Data = { try Data(contentsOf: $0) },
        dataWriter: @escaping (Data, URL) throws -> Void = { data, url in
            try data.write(to: url, options: .atomic)
        },
        inMemory: Bool = false
    ) {
        self.fileManager = fileManager
        self.dataReader = dataReader
        self.dataWriter = dataWriter
        self.fileURL = inMemory ? nil : fileURL ?? Self.defaultFileURL(fileManager: fileManager)
        entries = []
        if let fileURL = self.fileURL {
            load(from: fileURL)
        }
    }

    static func inMemory() -> ResolutionMemoryStore {
        ResolutionMemoryStore(inMemory: true)
    }

    func suggestion(
        conflictID: String,
        profileID: String,
        sourceKind: ConversionSourceKind,
        mappingVersion: Int
    ) -> ResolutionMemorySuggestion? {
        let applicableScopes: Set<ResolutionMemoryScope> = [
            .profile(profileID), .sourceKind(sourceKind), .global,
        ]
        guard let entry = entries
            .filter({ $0.conflictID == conflictID && applicableScopes.contains($0.scope) })
            .max(by: { $0.scope.precedence < $1.scope.precedence })
        else {
            return nil
        }
        return ResolutionMemorySuggestion(entry: entry, isStale: entry.mappingVersion != mappingVersion)
    }

    func store(
        resolutionID: String,
        for conflictID: String,
        scope: ResolutionMemoryScope,
        mappingVersion: Int
    ) throws {
        guard !resolutionID.isEmpty, !conflictID.isEmpty else {
            throw ResolutionMemoryStoreError.invalidDocument
        }
        var updated = entries.filter { !($0.conflictID == conflictID && $0.scope == scope) }
        updated.append(
            ResolutionMemoryEntry(
                conflictID: conflictID,
                resolutionID: resolutionID,
                scope: scope,
                mappingVersion: mappingVersion
            )
        )
        try persist(updated)
        entries = updated
    }

    func forget(conflictID: String, scope: ResolutionMemoryScope) throws {
        let updated = entries.filter { !($0.conflictID == conflictID && $0.scope == scope) }
        guard updated.count != entries.count else { return }
        try persist(updated)
        entries = updated
    }

    func removeProfileMemories(profileID: String) throws {
        let updated = entries.filter { $0.scope != .profile(profileID) }
        guard updated.count != entries.count else { return }
        try persist(updated)
        entries = updated
    }

    func replaceEntriesForTransaction(_ updatedEntries: [ResolutionMemoryEntry]) throws {
        guard Set(updatedEntries.map(\.id)).count == updatedEntries.count,
              updatedEntries.allSatisfy({ !$0.conflictID.isEmpty && !$0.resolutionID.isEmpty })
        else {
            throw ResolutionMemoryStoreError.invalidDocument
        }
        try persist(updatedEntries)
        entries = updatedEntries
    }

    private func load(from fileURL: URL) {
        guard fileManager.fileExists(atPath: fileURL.path) else { return }
        do {
            let data = try dataReader(fileURL)
            let document = try JSONDecoder().decode(ResolutionMemoryDocument.self, from: data)
            guard document.version <= ResolutionMemoryDocument.currentVersion else {
                writesBlocked = true
                loadErrorMessage = "Resolution suggestions are newer than this app. Changes are disabled to protect them."
                return
            }
            guard document.version == ResolutionMemoryDocument.currentVersion,
                  Set(document.entries.map(\.id)).count == document.entries.count,
                  document.entries.allSatisfy({ !$0.conflictID.isEmpty && !$0.resolutionID.isEmpty })
            else {
                throw ResolutionMemoryStoreError.invalidDocument
            }
            entries = document.entries
        } catch {
            recoverFromUnreadableFile()
        }
    }

    private func persist(_ entries: [ResolutionMemoryEntry]) throws {
        guard !writesBlocked else { throw ResolutionMemoryStoreError.recoveryRequired }
        guard let fileURL else { return }
        try fileManager.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try dataWriter(encoder.encode(ResolutionMemoryDocument(entries: entries)), fileURL)
        loadErrorMessage = nil
    }

    private func recoverFromUnreadableFile() {
        guard let fileURL else { return }
        var recoveryURL = fileURL.appendingPathExtension("corrupt")
        var suffix = 2
        while fileManager.fileExists(atPath: recoveryURL.path) {
            recoveryURL = fileURL.appendingPathExtension("corrupt-\(suffix)")
            suffix += 1
        }
        do {
            try fileManager.moveItem(at: fileURL, to: recoveryURL)
            loadErrorMessage = "Resolution suggestions could not be loaded. The original file was preserved as \(recoveryURL.lastPathComponent)."
        } catch {
            writesBlocked = true
            loadErrorMessage = "Resolution suggestions could not be loaded or preserved. Changes are disabled to protect the original file."
        }
        entries = []
    }

    private static func defaultFileURL(fileManager: FileManager) -> URL {
        let applicationSupportURL = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        return applicationSupportURL
            .appendingPathComponent("3D Blu-ray to Vision Pro", isDirectory: true)
            .appendingPathComponent("resolution-memory.json")
    }
}

enum ResolutionMemoryStoreError: LocalizedError, Equatable {
    case invalidDocument
    case recoveryRequired

    var errorDescription: String? {
        switch self {
        case .invalidDocument: "Resolution suggestion data is invalid."
        case .recoveryRequired: "Resolution suggestion changes are disabled until the original data is protected."
        }
    }
}

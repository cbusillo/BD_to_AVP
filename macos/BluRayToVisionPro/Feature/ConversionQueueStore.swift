import Foundation
import SwiftUI

@MainActor
final class ConversionQueueStore: ObservableObject {
    static let maxRetainedTerminalUnits = 5
    static let maxRetainedItems = 200

    @Published private(set) var document: ConversionQueueDocument
    @Published private(set) var loadErrorMessage: String?
    @Published private(set) var writesBlocked = false

    private let fileURL: URL?
    private let fileManager: FileManager
    private let dataReader: (URL) throws -> Data
    private let dataMover: (URL, URL) throws -> Void
    private let writer: ConversionQueueFileWriter?
    private let mutationLock = ConversionQueueMutationLock()
    private var nextGeneration = 0
    private var documentRevision = 0

    init(
        fileURL: URL? = nil,
        fileManager: FileManager = .default,
        dataReader: @escaping (URL) throws -> Data = { try Data(contentsOf: $0) },
        dataWriter: @escaping @Sendable (Data, URL) throws -> Void = { data, url in
            try data.write(to: url, options: .atomic)
        },
        dataMover: ((URL, URL) throws -> Void)? = nil,
        inMemory: Bool = false
    ) {
        self.fileManager = fileManager
        self.dataReader = dataReader
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

    func moveWaitingItem(_ itemID: UUID, before targetID: UUID) async throws {
        guard itemID != targetID else {
            return
        }
        try await mutateItems { items in
            guard let sourceIndex = items.firstIndex(where: { $0.id == itemID }),
                  let targetIndex = items.firstIndex(where: { $0.id == targetID }),
                  items[sourceIndex].state == .waiting,
                  items[targetIndex].state == .waiting
            else {
                throw ConversionQueueStoreError.invalidDocument
            }
            let affectedGroupIDs = Set([items[sourceIndex].groupID, items[targetIndex].groupID].compactMap { $0 })
            let sourceRemovalRequests = Self.multiTitleSourceRemovalRequests(
                in: items,
                groupIDs: affectedGroupIDs
            )
            let item = items.remove(at: sourceIndex)
            guard let insertionIndex = items.firstIndex(where: { $0.id == targetID }) else {
                throw ConversionQueueStoreError.invalidDocument
            }
            items.insert(item, at: insertionIndex)
            Self.normalizeOrdinals(&items)
            Self.normalizeMultiTitleSourceRemoval(
                in: &items,
                requests: sourceRemovalRequests
            )
        }
    }

    func moveWaitingItemNext(_ itemID: UUID) async throws {
        try await mutateItems { items in
            guard let sourceIndex = items.firstIndex(where: { $0.id == itemID }),
                  items[sourceIndex].state == .waiting
            else {
                throw ConversionQueueStoreError.invalidDocument
            }
            let groupIDs = Set(items[sourceIndex].groupID.map { [$0] } ?? [])
            let sourceRemovalRequests = Self.multiTitleSourceRemovalRequests(in: items, groupIDs: groupIDs)
            let item = items.remove(at: sourceIndex)
            let insertionIndex = items.firstIndex(where: { $0.state == .waiting }) ?? min(sourceIndex, items.count)
            items.insert(item, at: insertionIndex)
            Self.normalizeOrdinals(&items)
            Self.normalizeMultiTitleSourceRemoval(in: &items, requests: sourceRemovalRequests)
        }
    }

    func removeWaitingItems(_ itemIDs: Set<UUID>) async throws -> PersistentQueueRemovalToken {
        var removedItems: [DurableConversionQueueItem] = []
        let revision = try await mutateItems { items in
            let matchingItems = items.filter { itemIDs.contains($0.id) }
            guard matchingItems.count == itemIDs.count,
                  matchingItems.allSatisfy({ $0.state == .waiting })
            else {
                throw ConversionQueueStoreError.invalidDocument
            }
            let affectedGroupIDs = Set(matchingItems.compactMap(\.groupID))
            let sourceRemovalRequests = Self.multiTitleSourceRemovalRequests(
                in: items,
                groupIDs: affectedGroupIDs
            )
            removedItems = matchingItems
            items.removeAll { itemIDs.contains($0.id) }
            Self.normalizeOrdinals(&items)
            Self.normalizeMultiTitleSourceRemoval(
                in: &items,
                requests: sourceRemovalRequests
            )
        }
        return PersistentQueueRemovalToken(items: removedItems, revision: revision)
    }

    func restoreRemovedItems(_ token: PersistentQueueRemovalToken) async throws {
        guard !token.isEmpty else {
            return
        }
        try await mutateItems { items in
            guard token.revision == documentRevision else {
                throw ConversionQueueStoreError.staleRemovalToken
            }
            let existingIDs = Set(items.map(\.id))
            guard token.items.allSatisfy({ !existingIDs.contains($0.id) }) else {
                throw ConversionQueueStoreError.invalidDocument
            }
            for item in token.items.sorted(by: { $0.ordinal < $1.ordinal }) {
                items.insert(item, at: min(item.ordinal, items.count))
            }
            Self.normalizeOrdinals(&items)
            Self.normalizeMultiTitleSourceRemoval(
                in: &items,
                requests: Self.multiTitleSourceRemovalRequests(
                    in: items,
                    groupIDs: Set(token.items.compactMap(\.groupID))
                )
            )
        }
    }

    func updateWaitingItemIntent(_ itemID: UUID, intent: DurableQueueItemIntent) async throws {
        try await mutateItems { items in
            guard let index = items.firstIndex(where: { $0.id == itemID }),
                  items[index].state == .waiting
            else {
                throw ConversionQueueStoreError.invalidDocument
            }
            items[index].intent = intent
            let groupIDs = Set(items[index].groupID.map { [$0] } ?? [])
            Self.normalizeMultiTitleSourceRemoval(
                in: &items,
                requests: Self.multiTitleSourceRemovalRequests(in: items, groupIDs: groupIDs)
            )
        }
    }

    func clearCompletedItems() async throws -> PersistentQueueRemovalToken {
        var removedItems: [DurableConversionQueueItem] = []
        let revision = try await mutateItems { items in
            let terminalGroupIDs = Set(items.compactMap { item -> UUID? in
                guard item.state == .completed, let groupID = item.groupID else {
                    return nil
                }
                return items.allSatisfy { candidate in
                    candidate.groupID != groupID
                        || candidate.state == .completed
                        || candidate.state == .stopped
                } ? groupID : nil
            })
            removedItems = items.filter { item in
                item.state == .completed
                    && (item.groupID == nil || item.groupID.map(terminalGroupIDs.contains) == true)
            }
            let removedIDs = Set(removedItems.map(\.id))
            items.removeAll { removedIDs.contains($0.id) }
            Self.normalizeOrdinals(&items)
        }
        return PersistentQueueRemovalToken(items: removedItems, revision: revision)
    }

    func replaceItems(_ items: [DurableConversionQueueItem]) async throws {
        await mutationLock.lock()
        do {
            try validate(items)
            let compactedItems = compacted(items)
            try validate(compactedItems)
            try await persist(ConversionQueueDocument(items: compactedItems))
            await mutationLock.unlock()
        } catch {
            await mutationLock.unlock()
            throw error
        }
    }

    @discardableResult
    func mutateItems(_ mutation: (inout [DurableConversionQueueItem]) throws -> Void) async throws -> Int {
        await mutationLock.lock()
        do {
            var items = document.items
            try mutation(&items)
            try validate(items)
            let compactedItems = compacted(items)
            try validate(compactedItems)
            let revision = try await persist(ConversionQueueDocument(items: compactedItems))
            await mutationLock.unlock()
            return revision
        } catch {
            await mutationLock.unlock()
            throw error
        }
    }

    private func persist(_ nextDocument: ConversionQueueDocument) async throws -> Int {
        guard !writesBlocked else {
            throw ConversionQueueStoreError.recoveryRequired
        }
        guard let writer else {
            document = nextDocument
            loadErrorMessage = nil
            documentRevision += 1
            return documentRevision
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
            try await writer.write(data, generation: generation)
            document = nextDocument
            loadErrorMessage = nil
            documentRevision += 1
            return documentRevision
        } catch let error as ConversionQueueStoreError {
            throw error
        } catch {
            throw ConversionQueueStoreError.writeFailed
        }
    }

    private func load(from fileURL: URL) {
        guard fileManager.fileExists(atPath: fileURL.path) else {
            return
        }
        let data: Data
        do {
            data = try dataReader(fileURL)
        } catch {
            writesBlocked = true
            loadErrorMessage = "Queue data could not be read. Queue changes are disabled to protect the original file."
            return
        }
        do {
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
        for (index, item) in items.enumerated() {
            guard item.ordinal == index,
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
            if item.state == .failed, item.failure == nil {
                throw ConversionQueueStoreError.invalidDocument
            }
        }
    }

    private func compacted(_ items: [DurableConversionQueueItem]) -> [DurableConversionQueueItem] {
        let retentionUnits = retentionUnits(from: items)
        let terminalUnitIndices = retentionUnits.indices.filter { retentionUnits[$0].isPrunable }
        guard !terminalUnitIndices.isEmpty else {
            return items
        }

        var retainedUnitIndices = Set(retentionUnits.indices.filter { !retentionUnits[$0].isPrunable })
        retainedUnitIndices.formUnion(terminalUnitIndices.suffix(Self.maxRetainedTerminalUnits))
        let newestTerminalUnitIndex = terminalUnitIndices.last
        var retainedItemCount = retainedUnitIndices.reduce(into: 0) { count, unitIndex in
            count += retentionUnits[unitIndex].items.count
        }

        // Whole retention units can make the soft item ceiling undershoot; correctness takes priority over exact size.
        for unitIndex in terminalUnitIndices where retainedItemCount > Self.maxRetainedItems {
            guard unitIndex != newestTerminalUnitIndex,
                  retainedUnitIndices.remove(unitIndex) != nil
            else {
                continue
            }
            retainedItemCount -= retentionUnits[unitIndex].items.count
        }

        let retainedItemIDs = Set(retentionUnits.indices
            .filter { retainedUnitIndices.contains($0) }
            .flatMap { retentionUnits[$0].items.map(\.id) })
        var compactedItems = items.filter { retainedItemIDs.contains($0.id) }
        for index in compactedItems.indices {
            compactedItems[index].ordinal = index
        }
        return compactedItems
    }

    private func retentionUnits(from items: [DurableConversionQueueItem]) -> [ConversionQueueRetentionUnit] {
        var units: [ConversionQueueRetentionUnit] = []
        var groupedUnitIndices: [UUID: Int] = [:]
        for item in items {
            guard let groupID = item.groupID else {
                units.append(ConversionQueueRetentionUnit(items: [item]))
                continue
            }
            if let unitIndex = groupedUnitIndices[groupID] {
                units[unitIndex].items.append(item)
            } else {
                groupedUnitIndices[groupID] = units.count
                units.append(ConversionQueueRetentionUnit(items: [item]))
            }
        }
        return units
    }

    private static func normalizeOrdinals(_ items: inout [DurableConversionQueueItem]) {
        for index in items.indices {
            items[index].ordinal = index
        }
    }

    private static func multiTitleSourceRemovalRequests(
        in items: [DurableConversionQueueItem],
        groupIDs: Set<UUID>
    ) -> [UUID: Bool] {
        Dictionary(uniqueKeysWithValues: groupIDs.map { groupID in
            let requested = items.contains { item in
                item.groupID == groupID
                    && item.origin == .multiTitle
                    && item.intent.options.job.removeOriginalAfterSuccess
            }
            return (groupID, requested)
        })
    }

    private static func normalizeMultiTitleSourceRemoval(
        in items: inout [DurableConversionQueueItem],
        requests: [UUID: Bool]
    ) {
        for (groupID, requested) in requests {
            let finalWaitingItemID = items.last { item in
                item.groupID == groupID
                    && item.origin == .multiTitle
                    && item.state == .waiting
            }?.id
            for index in items.indices where items[index].groupID == groupID
                && items[index].origin == .multiTitle
                && items[index].state == .waiting
            {
                items[index].intent.options.job.removeOriginalAfterSuccess = requested
                    && items[index].id == finalWaitingItemID
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

private struct ConversionQueueRetentionUnit {
    var items: [DurableConversionQueueItem]

    var isPrunable: Bool {
        items.allSatisfy { item in
            item.state == .completed || item.state == .stopped
        }
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

    func write(_ data: Data, generation: Int) throws {
        guard generation > latestGeneration else {
            throw ConversionQueueStoreError.staleWrite
        }
        latestGeneration = generation
        try FileManager.default.createDirectory(
            at: fileURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try dataWriter(data, fileURL)
    }
}

private actor ConversionQueueMutationLock {
    private var locked = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func lock() async {
        guard locked else {
            locked = true
            return
        }
        await withCheckedContinuation { continuation in
            waiters.append(continuation)
        }
    }

    func unlock() {
        guard !waiters.isEmpty else {
            locked = false
            return
        }
        waiters.removeFirst().resume()
    }
}

enum ConversionQueueStoreError: LocalizedError, Equatable {
    case invalidDocument
    case recoveryRequired
    case staleRemovalToken
    case staleWrite
    case unsupportedVersion(Int)
    case writeFailed

    var errorDescription: String? {
        switch self {
        case .invalidDocument:
            "The queue contains invalid data."
        case .recoveryRequired:
            "Queue changes are disabled until the original queue data can be protected."
        case .staleRemovalToken:
            "The queue changed after these items were removed, so Undo is no longer available."
        case .staleWrite:
            "A newer queue update was already saved."
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

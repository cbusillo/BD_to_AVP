import Foundation

/// The queue admission boundary automation uses; `ConversionViewModel` provides it.
@MainActor
protocol QueueAutomationAdmitting: AnyObject {
    func admitAutomatedPersistentQueueDrafts(_ drafts: [AutomatedQueueDraft]) async throws -> PersistentQueueAppendResult
}

extension ConversionViewModel: QueueAutomationAdmitting {}

struct WatchFolderScanReport: Equatable {
    let admittedCount: Int
    let duplicateCount: Int
    let pendingCount: Int
    /// When to scan again so pending candidates can settle; `nil` when nothing is pending.
    let nextEvaluationAt: Date?
}

enum WatchFolderScanOutcome: Equatable {
    case disabled
    /// The folder could not be reached, for example because its drive is disconnected.
    case unavailable
    case scanned(WatchFolderScanReport)
}

/// Owns queue automation independently of any window, so it can move to a separate helper later.
///
/// It keeps the watch-folder configuration and seen-sources record, and admits settled sources
/// through the same queue admission path as manual adds. File-system observation calls `scan(folder:)`
/// whenever a folder may have changed, and at launch to catch up on anything that arrived meanwhile.
@MainActor
final class QueueAutomationController: ObservableObject {
    typealias DraftFactoryProvider = @MainActor (WatchFolder) -> QueueDraftFactory
    typealias BookmarkResolver = (Data) throws -> (url: URL, isStale: Bool)
    typealias BookmarkCreator = (URL) throws -> Data

    let store: QueueAutomationStore
    let settleInterval: TimeInterval

    private let admitter: any QueueAutomationAdmitting
    private let draftFactory: DraftFactoryProvider
    private let now: () -> Date
    private let resolveBookmark: BookmarkResolver
    private let createBookmark: BookmarkCreator
    private var settleEvaluators: [UUID: WatchFolderSettleEvaluator] = [:]

    init(
        store: QueueAutomationStore,
        admitter: any QueueAutomationAdmitting,
        draftFactory: @escaping DraftFactoryProvider,
        settleInterval: TimeInterval = WatchFolderSettleEvaluator.defaultSettleInterval,
        now: @escaping () -> Date = Date.init,
        resolveBookmark: @escaping BookmarkResolver = QueueAutomationController.resolveSecurityScopedBookmark,
        createBookmark: @escaping BookmarkCreator = QueueAutomationController.createSecurityScopedBookmark
    ) {
        self.store = store
        self.admitter = admitter
        self.draftFactory = draftFactory
        self.settleInterval = settleInterval
        self.now = now
        self.resolveBookmark = resolveBookmark
        self.createBookmark = createBookmark
    }

    convenience init(
        store: QueueAutomationStore,
        viewModel: ConversionViewModel,
        settings: AppSettings,
        profileStore: ProfileStore
    ) {
        self.init(
            store: store,
            admitter: viewModel,
            draftFactory: { folder in
                QueueDraftFactory(settings: settings, profileStore: profileStore, profileID: folder.profileID)
            }
        )
    }

    var watchFolders: [WatchFolder] { store.watchFolders }

    @discardableResult
    func addWatchFolder(
        at url: URL,
        mode: WatchFolderAdmissionMode = .holdForReview,
        profileID: String? = nil,
        includesSubfolders: Bool = false
    ) throws -> WatchFolder {
        let folder = WatchFolder(
            bookmark: try createBookmark(url),
            displayName: FileManager.default.displayName(atPath: url.path),
            mode: mode,
            profileID: profileID,
            includesSubfolders: includesSubfolders
        )
        try store.saveWatchFolder(folder)
        return folder
    }

    func updateWatchFolder(_ folder: WatchFolder) throws {
        guard store.watchFolder(id: folder.id) != nil else {
            throw QueueAutomationStoreError.unknownWatchFolder
        }
        try store.saveWatchFolder(folder)
        if !folder.isEnabled {
            settleEvaluators[folder.id] = nil
        }
    }

    func removeWatchFolder(id: UUID) throws {
        try store.removeWatchFolder(id: id)
        settleEvaluators[id] = nil
    }

    func forgetSeenSource(path: String) throws {
        try store.forgetSeenSource(path: path)
    }

    func forgetSeenSources(watchFolderID: UUID) throws {
        try store.forgetSeenSources(watchFolderID: watchFolderID)
    }

    /// Scans every enabled watch folder, as at launch.
    func scanAllWatchFolders() async throws -> [UUID: WatchFolderScanOutcome] {
        var outcomes: [UUID: WatchFolderScanOutcome] = [:]
        for folder in store.watchFolders {
            outcomes[folder.id] = try await scan(folder: folder)
        }
        return outcomes
    }

    /// Looks for new sources in a watch folder and admits the ones that have finished arriving.
    func scan(folder: WatchFolder) async throws -> WatchFolderScanOutcome {
        guard let folder = store.watchFolder(id: folder.id) else {
            throw QueueAutomationStoreError.unknownWatchFolder
        }
        guard folder.isEnabled else {
            settleEvaluators[folder.id] = nil
            return .disabled
        }
        guard let directoryURL = try? resolvedDirectory(for: folder) else {
            settleEvaluators[folder.id] = nil
            return .unavailable
        }
        let includesSubfolders = folder.includesSubfolders
        let found: [WatchFolderCandidate]
        do {
            found = try await Task.detached(priority: .utility) {
                let accessing = directoryURL.startAccessingSecurityScopedResource()
                defer {
                    if accessing { directoryURL.stopAccessingSecurityScopedResource() }
                }
                return try WatchFolderCandidateScanner.candidates(
                    in: directoryURL,
                    includesSubfolders: includesSubfolders
                )
            }.value
        } catch {
            settleEvaluators[folder.id] = nil
            return .unavailable
        }

        let unseen = found.filter { !store.hasSeen($0.fingerprint) }
        var evaluator = settleEvaluators[folder.id] ?? WatchFolderSettleEvaluator(settleInterval: settleInterval)
        let evaluation = evaluator.evaluate(unseen, at: now())
        settleEvaluators[folder.id] = evaluator

        var result = PersistentQueueAppendResult(addedCount: 0, duplicateDisplayNames: [])
        if !evaluation.settled.isEmpty {
            let factory = draftFactory(folder)
            let admittedAt = now()
            let drafts = evaluation.settled.map { settled in
                AutomatedQueueDraft(
                    draft: factory.draft(for: settled.candidate.source),
                    admission: .watchFolder(
                        id: folder.id,
                        name: folder.displayName,
                        arrivedAt: settled.arrivedAt,
                        settledAt: settled.settledAt,
                        admittedAt: admittedAt
                    ),
                    holdForReview: folder.mode == .holdForReview
                )
            }
            result = try await admitter.admitAutomatedPersistentQueueDrafts(drafts)
            // Duplicates are already queued, so they are recorded as seen too.
            try store.recordSeen(
                evaluation.settled.map(\.candidate.fingerprint),
                watchFolderID: folder.id,
                at: admittedAt
            )
        }
        return .scanned(WatchFolderScanReport(
            admittedCount: result.addedCount,
            duplicateCount: result.duplicateCount,
            pendingCount: evaluation.pending.count,
            nextEvaluationAt: evaluation.nextEvaluationAt
        ))
    }

    private func resolvedDirectory(for folder: WatchFolder) throws -> URL {
        let resolved = try resolveBookmark(folder.bookmark)
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: resolved.url.path, isDirectory: &isDirectory),
              isDirectory.boolValue
        else {
            throw QueueAutomationStoreError.unknownWatchFolder
        }
        if resolved.isStale, let refreshed = try? createBookmark(resolved.url) {
            var updated = folder
            updated.bookmark = refreshed
            try? store.saveWatchFolder(updated)
        }
        return resolved.url
    }

    nonisolated static func resolveSecurityScopedBookmark(_ data: Data) throws -> (url: URL, isStale: Bool) {
        var isStale = false
        let url = try URL(
            resolvingBookmarkData: data,
            options: [.withSecurityScope, .withoutUI],
            relativeTo: nil,
            bookmarkDataIsStale: &isStale
        )
        return (url, isStale)
    }

    nonisolated static func createSecurityScopedBookmark(_ url: URL) throws -> Data {
        try url.bookmarkData(options: .withSecurityScope, includingResourceValuesForKeys: nil, relativeTo: nil)
    }
}

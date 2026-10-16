import AppKit
import Combine
import Foundation

/// The queue admission boundary automation uses; `ConversionViewModel` provides it.
@MainActor
protocol QueueAutomationAdmitting: AnyObject {
    func admitAutomatedPersistentQueueDrafts(_ drafts: [AutomatedQueueDraft]) async throws -> PersistentQueueAppendResult
    func startAutomaticallyAdmittedItems(
        _ itemIDs: [UUID],
        otherWorkIsActive: Bool
    ) async -> AutomaticQueueStartDecision
}

extension ConversionViewModel: QueueAutomationAdmitting {}

struct WatchFolderScanReport: Equatable {
    let admittedCount: Int
    let duplicateCount: Int
    let pendingCount: Int
    /// When to scan again so pending candidates can settle; `nil` when nothing is pending.
    let nextEvaluationAt: Date?
    var admittedItemIDs: [UUID] = []
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
/// through the same queue admission path as manual adds. While monitoring, it watches each enabled
/// folder for file-system changes, re-scans on a timer until arriving files settle, catches up on
/// anything that arrived while it was not running, and resumes a folder when its drive comes back.
@MainActor
final class QueueAutomationController: ObservableObject {
    typealias DraftFactoryProvider = @MainActor (WatchFolder) -> QueueDraftFactory
    typealias BookmarkResolver = (Data) throws -> (url: URL, isStale: Bool)
    typealias BookmarkCreator = (URL) throws -> Data

    /// Added to a settle time before re-scanning, so a timer that fires on the boundary sees the file settled.
    static let rescanTolerance: TimeInterval = 1

    let store: QueueAutomationStore
    let settleInterval: TimeInterval

    @Published private(set) var statuses: [UUID: WatchFolderStatus] = [:]
    @Published private(set) var scanErrorMessages: [UUID: String] = [:]
    @Published private(set) var isMonitoring = false

    /// Receives the number of items newly held for review, once per round of scans.
    var heldItemsAdmittedHandler: (@MainActor (Int) -> Void)?
    /// Reports work outside the queue, such as a preview, that an automatic start must not interrupt.
    var isOtherWorkActive: @MainActor () -> Bool = { false }

    private let admitter: any QueueAutomationAdmitting
    private let draftFactory: DraftFactoryProvider
    private let now: () -> Date
    private let resolveBookmark: BookmarkResolver
    private let createBookmark: BookmarkCreator
    private let eventSourceFactory: WatchFolderEventSourceFactory
    private let timerScheduler: any QueueAutomationTimerScheduling
    private let workspaceNotificationCenter: NotificationCenter
    private var settleEvaluators: [UUID: WatchFolderSettleEvaluator] = [:]
    private var eventSources: [UUID: any WatchFolderEventSource] = [:]
    private var timers: [UUID: any QueueAutomationTimer] = [:]
    private var scanTasks: [UUID: Task<Void, Never>] = [:]
    private var rescanRequested: Set<UUID> = []
    private var pendingHeldArrivalCount = 0
    private var resolvedPaths: [UUID: String] = [:]
    private var notificationObservers: [NSObjectProtocol] = []
    private var storeSubscription: AnyCancellable?

    init(
        store: QueueAutomationStore,
        admitter: any QueueAutomationAdmitting,
        draftFactory: @escaping DraftFactoryProvider,
        settleInterval: TimeInterval = WatchFolderSettleEvaluator.defaultSettleInterval,
        now: @escaping () -> Date = Date.init,
        resolveBookmark: @escaping BookmarkResolver = QueueAutomationController.resolveSecurityScopedBookmark,
        createBookmark: @escaping BookmarkCreator = QueueAutomationController.createSecurityScopedBookmark,
        eventSourceFactory: @escaping WatchFolderEventSourceFactory = FSEventsWatchFolderEventSource.make,
        timerScheduler: (any QueueAutomationTimerScheduling)? = nil,
        workspaceNotificationCenter: NotificationCenter = NSWorkspace.shared.notificationCenter
    ) {
        self.store = store
        self.admitter = admitter
        self.draftFactory = draftFactory
        self.settleInterval = settleInterval
        self.now = now
        self.resolveBookmark = resolveBookmark
        self.createBookmark = createBookmark
        self.eventSourceFactory = eventSourceFactory
        self.timerScheduler = timerScheduler ?? TaskQueueAutomationTimerScheduler(now: now)
        self.workspaceNotificationCenter = workspaceNotificationCenter
        storeSubscription = store.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }
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

    func status(for folder: WatchFolder) -> WatchFolderStatus {
        guard folder.isEnabled else {
            return .paused
        }
        return statuses[folder.id] ?? .checking
    }

    /// The folder's location, from its last successful resolution or else from its bookmark.
    func displayPath(for folder: WatchFolder) -> String? {
        if let path = resolvedPaths[folder.id] {
            return path
        }
        return URL.resourceValues(forKeys: [.pathKey], fromBookmarkData: folder.bookmark)?.path
    }

    @discardableResult
    func addWatchFolder(
        at url: URL,
        mode: WatchFolderAdmissionMode = .holdForReview,
        profileID: String? = nil,
        includesSubfolders: Bool = false
    ) throws -> WatchFolder {
        let path = url.standardizedFileURL.path
        guard !store.watchFolders.contains(where: { displayPath(for: $0) == path }) else {
            throw QueueAutomationStoreError.alreadyWatched
        }
        let folder = WatchFolder(
            bookmark: try createBookmark(url),
            displayName: FileManager.default.displayName(atPath: url.path),
            mode: mode,
            profileID: profileID,
            includesSubfolders: includesSubfolders
        )
        try store.saveWatchFolder(folder)
        resolvedPaths[folder.id] = url.standardizedFileURL.path
        if isMonitoring {
            requestScan(folderID: folder.id)
        }
        return folder
    }

    func updateWatchFolder(_ folder: WatchFolder) throws {
        guard store.watchFolder(id: folder.id) != nil else {
            throw QueueAutomationStoreError.unknownWatchFolder
        }
        try store.saveWatchFolder(folder)
        if !folder.isEnabled {
            settleEvaluators[folder.id] = nil
            stopWatching(folderID: folder.id)
        }
        if isMonitoring {
            requestScan(folderID: folder.id)
        }
    }

    func removeWatchFolder(id: UUID) throws {
        try store.removeWatchFolder(id: id)
        settleEvaluators[id] = nil
        stopWatching(folderID: id)
        statuses[id] = nil
        scanErrorMessages[id] = nil
        resolvedPaths[id] = nil
    }

    func forgetSeenSource(path: String) throws {
        try store.forgetSeenSource(path: path)
    }

    func forgetSeenSources(watchFolderID: UUID) throws {
        try store.forgetSeenSources(watchFolderID: watchFolderID)
        if isMonitoring {
            requestScan(folderID: watchFolderID)
        }
    }

    // MARK: - Monitoring

    /// Starts watching every enabled folder and catches up on anything that arrived while the app was closed.
    ///
    /// Returns after the catch-up scans finish. Files found then still settle before they are admitted.
    func startMonitoring() async {
        guard !isMonitoring else {
            return
        }
        isMonitoring = true
        let center = workspaceNotificationCenter
        notificationObservers = [NSWorkspace.didMountNotification, NSWorkspace.didUnmountNotification].map { name in
            center.addObserver(forName: name, object: nil, queue: nil) { [weak self] _ in
                if Thread.isMainThread {
                    MainActor.assumeIsolated { self?.volumesDidChange() }
                } else {
                    Task { @MainActor in self?.volumesDidChange() }
                }
            }
        }
        for folder in store.watchFolders {
            requestScan(folderID: folder.id)
        }
        await waitForPendingScans()
    }

    func stopMonitoring() {
        guard isMonitoring else {
            return
        }
        isMonitoring = false
        for observer in notificationObservers {
            workspaceNotificationCenter.removeObserver(observer)
        }
        notificationObservers = []
        for folderID in Set(eventSources.keys).union(timers.keys) {
            stopWatching(folderID: folderID)
        }
    }

    /// Scans a folder soon, coalescing with a scan already running for it.
    @discardableResult
    func requestScan(folderID: UUID) -> Task<Void, Never> {
        if let task = scanTasks[folderID] {
            rescanRequested.insert(folderID)
            return task
        }
        let task = Task { @MainActor [weak self] in
            guard let self else {
                return
            }
            repeat {
                rescanRequested.remove(folderID)
                await performMonitoredScan(folderID: folderID)
            } while rescanRequested.contains(folderID)
            scanTasks[folderID] = nil
            deliverHeldArrivalsIfIdle()
        }
        scanTasks[folderID] = task
        return task
    }

    /// Waits until no folder scan is running or queued.
    func waitForPendingScans() async {
        while let task = scanTasks.values.first {
            await task.value
        }
    }

    private func volumesDidChange() {
        guard isMonitoring else {
            return
        }
        for folder in store.watchFolders where folder.isEnabled {
            requestScan(folderID: folder.id)
        }
    }

    private func performMonitoredScan(folderID: UUID) async {
        guard let folder = store.watchFolder(id: folderID) else {
            stopWatching(folderID: folderID)
            return
        }
        let outcome: WatchFolderScanOutcome
        do {
            outcome = try await scan(folder: folder)
            scanErrorMessages[folderID] = nil
        } catch {
            scanErrorMessages[folderID] = error.localizedDescription
            if isMonitoring {
                scheduleRescan(folderID: folderID, at: now().addingTimeInterval(settleInterval))
            }
            return
        }
        switch outcome {
        case .disabled:
            statuses[folderID] = .paused
            stopWatching(folderID: folderID)
        case .unavailable:
            // Watching resumes when a volume mounts, or when the person scans or edits the folder.
            statuses[folderID] = .offline
            stopWatching(folderID: folderID)
        case let .scanned(report):
            statuses[folderID] = .watching
            if isMonitoring {
                startEventSourceIfNeeded(for: folder)
                if let nextEvaluationAt = report.nextEvaluationAt {
                    scheduleRescan(
                        folderID: folderID,
                        at: max(nextEvaluationAt, now()).addingTimeInterval(Self.rescanTolerance)
                    )
                } else {
                    cancelRescan(folderID: folderID)
                }
            }
            await handleAdmittedItems(report.admittedItemIDs, from: folder)
        }
    }

    private func handleAdmittedItems(_ itemIDs: [UUID], from folder: WatchFolder) async {
        guard !itemIDs.isEmpty else {
            return
        }
        switch folder.mode {
        case .holdForReview:
            pendingHeldArrivalCount += itemIDs.count
        case .startAutomatically:
            _ = await admitter.startAutomaticallyAdmittedItems(itemIDs, otherWorkIsActive: isOtherWorkActive())
        }
    }

    private func deliverHeldArrivalsIfIdle() {
        guard scanTasks.isEmpty, pendingHeldArrivalCount > 0 else {
            return
        }
        let count = pendingHeldArrivalCount
        pendingHeldArrivalCount = 0
        heldItemsAdmittedHandler?(count)
    }

    private func startEventSourceIfNeeded(for folder: WatchFolder) {
        guard eventSources[folder.id] == nil,
              let directoryURL = try? resolvedDirectory(for: folder)
        else {
            return
        }
        let folderID = folder.id
        eventSources[folderID] = eventSourceFactory(directoryURL) { [weak self] in
            self?.requestScan(folderID: folderID)
        }
    }

    private func scheduleRescan(folderID: UUID, at date: Date) {
        timers[folderID]?.cancel()
        timers[folderID] = timerScheduler.schedule(at: date) { [weak self] in
            self?.timers[folderID] = nil
            self?.requestScan(folderID: folderID)
        }
    }

    private func cancelRescan(folderID: UUID) {
        timers[folderID]?.cancel()
        timers[folderID] = nil
    }

    private func stopWatching(folderID: UUID) {
        eventSources[folderID]?.stop()
        eventSources[folderID] = nil
        cancelRescan(folderID: folderID)
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
            nextEvaluationAt: evaluation.nextEvaluationAt,
            admittedItemIDs: result.addedItemIDs
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
        resolvedPaths[folder.id] = resolved.url.standardizedFileURL.path
        return resolved.url
    }

    nonisolated static func resolveSecurityScopedBookmark(_ data: Data) throws -> (url: URL, isStale: Bool) {
        var isStale = false
        let url = try URL(
            resolvingBookmarkData: data,
            // Never mount a volume just to check a watch folder; a disconnected drive reads as offline.
            options: [.withSecurityScope, .withoutUI, .withoutMounting],
            relativeTo: nil,
            bookmarkDataIsStale: &isStale
        )
        return (url, isStale)
    }

    nonisolated static func createSecurityScopedBookmark(_ url: URL) throws -> Data {
        try url.bookmarkData(options: .withSecurityScope, includingResourceValuesForKeys: nil, relativeTo: nil)
    }
}

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

    /// Asked before every start the queue makes on its own, and before each further video of such a run.
    var runPolicyGate: (any QueueRunPolicyGating)? { get set }
    func runPolicyQueueSnapshot(otherWorkIsActive: Bool) -> QueueRunPolicyQueueSnapshot
    func startQueueForRunPolicy(_ itemIDs: [UUID], windowEnd: Date?) async -> PersistentQueueCommandOutcome
    func publishRunPolicyDecision(_ decision: QueueRunPolicyDecision)
    /// Emits when the queue may have changed, so the run policy can be checked again.
    var queueStateChanges: AnyPublisher<Void, Never> { get }
}

/// Answers whether the app may start a video on its own right now, and records what the policy did.
@MainActor
protocol QueueRunPolicyGating: AnyObject {
    func runPolicyHold(for scope: QueueRunPolicyScope) -> QueueRunPolicyHold?
    func recordRunPolicyActivity(_ kind: QueueRunPolicyActivity.Kind, hold: QueueRunPolicyHold?, itemCount: Int)
}

extension ConversionViewModel: QueueAutomationAdmitting {
    var queueStateChanges: AnyPublisher<Void, Never> {
        objectWillChange.map { _ in () }.eraseToAnyPublisher()
    }
}

struct WatchFolderScanReport: Equatable {
    let admittedCount: Int
    let duplicateCount: Int
    let pendingCount: Int
    /// When to scan again so pending candidates can settle; `nil` when nothing is pending.
    let nextEvaluationAt: Date?
    var admittedItemIDs: [UUID] = []
    /// Settled candidates held back because they are still being written; included in `pendingCount`.
    var stillBeingWrittenCount = 0
}

/// A file in a watch folder that stopped changing but is still being written.
struct WatchFolderWriteHoldNotice: Equatable {
    let path: String
    let hold: WatchFolderWriteHold

    var message: String {
        hold.waitingMessage(fileName: (path as NSString).lastPathComponent)
    }
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
///
/// It also applies the queue run policy: it starts waiting items when a run time opens or a machine
/// condition clears, answers whether a run it started may start another video, and records each
/// decision in the policy's activity record. Every decision comes from `QueueRunPolicyDecision.decide`
/// with an injected clock, calendar, and machine conditions.
@MainActor
final class QueueAutomationController: ObservableObject, QueueRunPolicyGating {
    typealias DraftFactoryProvider = @MainActor (WatchFolder) -> QueueDraftFactory
    typealias BookmarkResolver = (Data) throws -> (url: URL, isStale: Bool)
    typealias BookmarkCreator = (URL) throws -> Data

    /// Added to a settle time before re-scanning, so a timer that fires on the boundary sees the file settled.
    static let rescanTolerance: TimeInterval = 1
    /// How often an active run policy is checked again between run-time boundaries.
    static let runPolicyRecheckInterval: TimeInterval = 60

    let store: QueueAutomationStore
    let settleInterval: TimeInterval

    @Published private(set) var statuses: [UUID: WatchFolderStatus] = [:]
    @Published private(set) var scanErrorMessages: [UUID: String] = [:]
    @Published private(set) var isMonitoring = false
    /// Files that stopped changing but are still being written, by watch folder and candidate path.
    @Published private(set) var writeHolds: [UUID: [String: WatchFolderWriteHold]] = [:]
    /// Why the last write check for a folder fell back to the settle time alone, when it did.
    @Published private(set) var writeCheckFallbackReasons: [UUID: String] = [:]

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
    private let machineConditions: any QueueMachineConditionsProviding
    private let writeCompletionCheck: WatchFolderWriteCompletionCheck
    let calendar: Calendar
    private var runPolicyTimer: (any QueueAutomationTimer)?
    private var runPolicyTask: Task<Void, Never>?
    private var runPolicyEvaluationRequested = false
    private var queueChangeSubscription: AnyCancellable?
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
        workspaceNotificationCenter: NotificationCenter = NSWorkspace.shared.notificationCenter,
        machineConditions: (any QueueMachineConditionsProviding)? = nil,
        writeCompletionCheck: WatchFolderWriteCompletionCheck = .system,
        calendar: Calendar = .autoupdatingCurrent
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
        self.machineConditions = machineConditions ?? SystemQueueMachineConditions()
        self.writeCompletionCheck = writeCompletionCheck
        self.calendar = calendar
        storeSubscription = store.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }
        admitter.runPolicyGate = self
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

    /// Files in the folder that are waiting only because they are still being written, by name.
    func writeHoldNotices(for folder: WatchFolder) -> [WatchFolderWriteHoldNotice] {
        (writeHolds[folder.id] ?? [:])
            .map { WatchFolderWriteHoldNotice(path: $0.key, hold: $0.value) }
            .sorted { $0.path < $1.path }
    }

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
            forgetPendingState(folderID: folder.id)
            stopWatching(folderID: folder.id)
        }
        if isMonitoring {
            requestScan(folderID: folder.id)
        }
    }

    func removeWatchFolder(id: UUID) throws {
        try store.removeWatchFolder(id: id)
        forgetPendingState(folderID: id)
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
        notificationObservers.append(center.addObserver(
            forName: NSWorkspace.didWakeNotification,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            Task { @MainActor in self?.requestRunPolicyEvaluation() }
        })
        machineConditions.startObserving { [weak self] in
            self?.requestRunPolicyEvaluation()
        }
        queueChangeSubscription = admitter.queueStateChanges
            .debounce(for: .milliseconds(250), scheduler: RunLoop.main)
            .sink { [weak self] _ in
                self?.requestRunPolicyEvaluation()
            }
        for folder in store.watchFolders {
            requestScan(folderID: folder.id)
        }
        await waitForPendingScans()
        // Opening the app inside a run time starts eligible waiting work, as the window opening would.
        requestRunPolicyEvaluation()
        await waitForRunPolicyEvaluation()
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
        machineConditions.stopObserving()
        queueChangeSubscription = nil
        runPolicyTimer?.cancel()
        runPolicyTimer = nil
    }

    // MARK: - Run policy

    var runPolicy: QueueRunPolicy { store.runPolicy }
    var runPolicyActivity: [QueueRunPolicyActivity] { store.runPolicyActivity }
    var currentMachineConditions: QueueMachineConditions { machineConditions.current }

    func updateRunPolicy(_ policy: QueueRunPolicy) throws {
        try store.saveRunPolicy(policy)
        requestRunPolicyEvaluation()
    }

    /// The policy applied to `date`, or to now, with the Mac's current power and thermal state.
    func runPolicyEvaluation(at date: Date? = nil) -> QueueRunPolicyEvaluation {
        QueueRunPolicyEvaluation.evaluate(
            store.runPolicy,
            at: date ?? now(),
            conditions: machineConditions.current,
            calendar: calendar
        )
    }

    func runPolicyHold(for scope: QueueRunPolicyScope) -> QueueRunPolicyHold? {
        runPolicyEvaluation().hold(for: scope)
    }

    /// Records what the policy did. A hold that repeats the previous entry is not recorded again.
    func recordRunPolicyActivity(_ kind: QueueRunPolicyActivity.Kind, hold: QueueRunPolicyHold?, itemCount: Int) {
        if kind != .started,
           let last = store.runPolicyActivity.last,
           last.kind == kind,
           last.hold == hold?.kind
        {
            return
        }
        try? store.recordRunPolicyActivity(QueueRunPolicyActivity(
            occurredAt: now(),
            kind: kind,
            hold: hold,
            itemCount: itemCount
        ))
    }

    /// Checks the policy soon, coalescing with a check already running.
    func requestRunPolicyEvaluation() {
        guard isMonitoring else {
            return
        }
        guard runPolicyTask == nil else {
            runPolicyEvaluationRequested = true
            return
        }
        runPolicyTask = Task { @MainActor [weak self] in
            guard let self else {
                return
            }
            repeat {
                runPolicyEvaluationRequested = false
                await evaluateRunPolicy()
            } while runPolicyEvaluationRequested
            runPolicyTask = nil
        }
    }

    func waitForRunPolicyEvaluation() async {
        while let task = runPolicyTask {
            await task.value
        }
    }

    /// Applies the policy to the queue now: starts it when a run time is open or a condition has
    /// cleared, records holds, and publishes what the queue is waiting for.
    @discardableResult
    func evaluateRunPolicy() async -> QueueRunPolicyDecision {
        let policy = store.runPolicy
        let evaluation = runPolicyEvaluation()
        var decision = QueueRunPolicyDecision.decide(
            policy: policy,
            evaluation: evaluation,
            queue: admitter.runPolicyQueueSnapshot(otherWorkIsActive: isOtherWorkActive())
        )
        switch decision {
        case let .start(itemIDs, windowEnd):
            let outcome = await admitter.startQueueForRunPolicy(itemIDs, windowEnd: windowEnd)
            switch outcome {
            case .accepted:
                recordRunPolicyActivity(.started, hold: nil, itemCount: itemIDs.count)
            case .noChange:
                break
            case .rejected:
                recordRunPolicyActivity(.startFailed, hold: nil, itemCount: itemIDs.count)
            }
            decision = QueueRunPolicyDecision.decide(
                policy: policy,
                evaluation: runPolicyEvaluation(),
                queue: admitter.runPolicyQueueSnapshot(otherWorkIsActive: isOtherWorkActive())
            )
            if case .start = decision {
                // The start did not take; the queue's own banners explain why.
                decision = .inactive
            }
        case let .waiting(hold, itemCount):
            recordRunPolicyActivity(.held, hold: hold, itemCount: itemCount)
        case .inactive, .running, .blockedByOtherWork:
            break
        }
        admitter.publishRunPolicyDecision(decision)
        scheduleRunPolicyRecheck(after: evaluation, isNeeded: decision != .inactive || policy.isActive)
        return decision
    }

    private func scheduleRunPolicyRecheck(after evaluation: QueueRunPolicyEvaluation, isNeeded: Bool) {
        runPolicyTimer?.cancel()
        runPolicyTimer = nil
        guard isMonitoring, isNeeded else {
            return
        }
        // Boundaries are exact; the periodic recheck catches changes no notification reports.
        let recheck = evaluation.date.addingTimeInterval(Self.runPolicyRecheckInterval)
        let date = min(evaluation.nextBoundary ?? recheck, recheck)
        runPolicyTimer = timerScheduler.schedule(at: date) { [weak self] in
            self?.runPolicyTimer = nil
            self?.requestRunPolicyEvaluation()
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
            forgetPendingState(folderID: folder.id)
            return .disabled
        }
        guard let directoryURL = try? resolvedDirectory(for: folder) else {
            forgetPendingState(folderID: folder.id)
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
            forgetPendingState(folderID: folder.id)
            return .unavailable
        }

        let unseen = found.filter { !store.hasSeen($0.fingerprint) }
        var evaluator = settleEvaluators[folder.id] ?? WatchFolderSettleEvaluator(settleInterval: settleInterval)
        let evaluation = evaluator.evaluate(unseen, at: now())
        settleEvaluators[folder.id] = evaluator

        // Holds for files that are gone or seen are dropped; the rest stay until the file is checked again.
        let unseenPaths = Set(unseen.map(\.fingerprint.path))
        var holds = (writeHolds[folder.id] ?? [:]).filter { unseenPaths.contains($0.key) }
        var admittable = evaluation.settled
        var stillBeingWritten: [SettledWatchFolderCandidate] = []
        if !evaluation.settled.isEmpty {
            // Only candidates about to be admitted are checked, so an idle folder costs nothing.
            let check = writeCompletionCheck
            let candidates = evaluation.settled.map(\.candidate)
            let checkResult = await Task.detached(priority: .utility) {
                let accessing = directoryURL.startAccessingSecurityScopedResource()
                defer {
                    if accessing { directoryURL.stopAccessingSecurityScopedResource() }
                }
                return check.check(candidates)
            }.value
            for settled in evaluation.settled {
                holds[settled.candidate.fingerprint.path] = checkResult.holds[settled.candidate.fingerprint.path]
            }
            admittable = evaluation.settled.filter { checkResult.holds[$0.candidate.fingerprint.path] == nil }
            stillBeingWritten = evaluation.settled.filter { checkResult.holds[$0.candidate.fingerprint.path] != nil }
            writeCheckFallbackReasons[folder.id] = checkResult.fallbackReason
            // The folder may have been paused or removed during the check; then nothing stays pending.
            if !stillBeingWritten.isEmpty, var current = settleEvaluators[folder.id] {
                current.restartSettling(stillBeingWritten, at: now())
                settleEvaluators[folder.id] = current
                evaluator = current
            }
        }
        writeHolds[folder.id] = holds.isEmpty ? nil : holds

        var result = PersistentQueueAppendResult(addedCount: 0, duplicateDisplayNames: [])
        if !admittable.isEmpty {
            let factory = draftFactory(folder)
            let admittedAt = now()
            let drafts = admittable.map { settled in
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
                admittable.map(\.candidate.fingerprint),
                watchFolderID: folder.id,
                at: admittedAt
            )
        }
        return .scanned(WatchFolderScanReport(
            admittedCount: result.addedCount,
            duplicateCount: result.duplicateCount,
            pendingCount: evaluation.pending.count + stillBeingWritten.count,
            nextEvaluationAt: evaluator.nextEvaluationAt,
            admittedItemIDs: result.addedItemIDs,
            stillBeingWrittenCount: stillBeingWritten.count
        ))
    }

    private func forgetPendingState(folderID: UUID) {
        settleEvaluators[folderID] = nil
        writeHolds[folderID] = nil
        writeCheckFallbackReasons[folderID] = nil
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

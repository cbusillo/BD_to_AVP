import AppKit
import Combine
import SwiftUI
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class WatchFolderMonitoringTests: XCTestCase {
    private var temporaryURLs: [URL] = []

    override func tearDown() {
        for url in temporaryURLs {
            try? FileManager.default.removeItem(at: url)
        }
        temporaryURLs = []
        super.tearDown()
    }

    // MARK: - Watching and timed re-scans

    func testLaunchCatchUpFindsExistingFileAndAdmitsItWhenTheSettleTimerFires() async throws {
        let harness = try makeHarness()
        let folder = try harness.controller.addWatchFolder(at: harness.directory)
        try writeFile(harness.directory.appendingPathComponent("Movie.mkv"), size: 4)
        let start = harness.clock.now

        await harness.controller.startMonitoring()

        XCTAssertEqual(harness.controller.status(for: folder), .watching)
        XCTAssertEqual(harness.events.sources.count, 1)
        XCTAssertTrue(harness.queueStore.items.isEmpty)
        let timer = try XCTUnwrap(harness.timers.pending.only)
        XCTAssertGreaterThanOrEqual(timer.date, start.addingTimeInterval(harness.controller.settleInterval))

        harness.clock.now = timer.date
        harness.timers.fire(timer)
        await harness.controller.waitForPendingScans()

        XCTAssertEqual(harness.queueStore.items.map(\.state), [.heldForReview])
        XCTAssertTrue(harness.timers.pending.isEmpty)
    }

    func testFileEventsStartSettlingAndAChangeMovesTheTimerLater() async throws {
        let harness = try makeHarness()
        _ = try harness.controller.addWatchFolder(at: harness.directory)
        await harness.controller.startMonitoring()
        XCTAssertTrue(harness.timers.pending.isEmpty)

        let movieURL = harness.directory.appendingPathComponent("Movie.mkv")
        try writeFile(movieURL, size: 4)
        try XCTUnwrap(harness.events.sources.first).change()
        await harness.controller.waitForPendingScans()
        let firstTimer = try XCTUnwrap(harness.timers.pending.only)

        harness.clock.advance(by: 30)
        try writeFile(movieURL, size: 8)
        try XCTUnwrap(harness.events.sources.first).change()
        await harness.controller.waitForPendingScans()
        let secondTimer = try XCTUnwrap(harness.timers.pending.only)
        XCTAssertTrue(firstTimer.isCancelled)
        XCTAssertGreaterThan(secondTimer.date, firstTimer.date)

        harness.clock.now = firstTimer.date
        harness.timers.fire(firstTimer)
        await harness.controller.waitForPendingScans()
        XCTAssertTrue(harness.queueStore.items.isEmpty)

        harness.clock.now = secondTimer.date
        harness.timers.fire(secondTimer)
        await harness.controller.waitForPendingScans()
        XCTAssertEqual(harness.queueStore.items.count, 1)
    }

    func testOfflineFolderResumesWhenADriveMountsAndGoesOfflineWhenItUnmounts() async throws {
        let harness = try makeHarness()
        let folder = try harness.controller.addWatchFolder(at: harness.directory)
        try FileManager.default.removeItem(at: harness.directory)

        await harness.controller.startMonitoring()
        XCTAssertEqual(harness.controller.status(for: folder), .offline)
        XCTAssertTrue(harness.events.sources.isEmpty)

        try FileManager.default.createDirectory(at: harness.directory, withIntermediateDirectories: true)
        harness.workspaceCenter.post(name: NSWorkspace.didMountNotification, object: nil)
        await harness.controller.waitForPendingScans()
        XCTAssertEqual(harness.controller.status(for: folder), .watching)
        let source = try XCTUnwrap(harness.events.sources.only)
        XCTAssertFalse(source.isStopped)

        try FileManager.default.removeItem(at: harness.directory)
        harness.workspaceCenter.post(name: NSWorkspace.didUnmountNotification, object: nil)
        await harness.controller.waitForPendingScans()
        XCTAssertEqual(harness.controller.status(for: folder), .offline)
        XCTAssertTrue(source.isStopped)
    }

    func testPausingAFolderStopsWatchingItAndResumingWatchesAgain() async throws {
        let harness = try makeHarness()
        var folder = try harness.controller.addWatchFolder(at: harness.directory)
        try writeFile(harness.directory.appendingPathComponent("Movie.mkv"), size: 4)
        await harness.controller.startMonitoring()
        let source = try XCTUnwrap(harness.events.sources.only)
        let timer = try XCTUnwrap(harness.timers.pending.only)

        folder.isEnabled = false
        try harness.controller.updateWatchFolder(folder)
        await harness.controller.waitForPendingScans()
        XCTAssertEqual(harness.controller.status(for: folder), .paused)
        XCTAssertTrue(source.isStopped)
        XCTAssertTrue(timer.isCancelled)

        folder.isEnabled = true
        try harness.controller.updateWatchFolder(folder)
        await harness.controller.waitForPendingScans()
        XCTAssertEqual(harness.controller.status(for: folder), .watching)
        XCTAssertEqual(harness.events.sources.filter { !$0.isStopped }.count, 1)
    }

    func testFolderAddedWhileMonitoringIsWatchedImmediately() async throws {
        let harness = try makeHarness()
        await harness.controller.startMonitoring()

        let folder = try harness.controller.addWatchFolder(at: harness.directory)
        await harness.controller.waitForPendingScans()

        XCTAssertEqual(harness.controller.status(for: folder), .watching)
        XCTAssertEqual(harness.events.sources.count, 1)
        XCTAssertThrowsError(try harness.controller.addWatchFolder(at: harness.directory))
    }

    // MARK: - Launch

    func testLaunchStartsWatchingOnlyForAPersonUsingTheApp() async throws {
        let harness = try makeHarness()
        let folder = try harness.controller.addWatchFolder(at: harness.directory)
        let delegate = AppDelegate()
        delegate.queueAutomationController = harness.controller

        XCTAssertNil(delegate.startQueueAutomationIfNeeded(
            arguments: ["app"],
            environment: [AppDelegate.unitTestHomeEnvironmentKey: "/tmp/home"]
        ))
        XCTAssertNil(delegate.startQueueAutomationIfNeeded(
            arguments: ["app", AppDelegate.startupSmokeArgument],
            environment: [:]
        ))
        XCTAssertFalse(harness.controller.isMonitoring)
        XCTAssertEqual(harness.controller.status(for: folder), .checking)

        let task = try XCTUnwrap(delegate.startQueueAutomationIfNeeded(arguments: ["app"], environment: [:]))
        await task.value

        XCTAssertTrue(harness.controller.isMonitoring)
        XCTAssertEqual(harness.controller.status(for: folder), .watching)
    }

    // MARK: - Held items and automatic starts

    func testHeldArrivalsFromSeveralFoldersAreAnnouncedOnce() async throws {
        let harness = try makeHarness()
        let second = harness.directory.deletingLastPathComponent().appendingPathComponent("Second", isDirectory: true)
        try FileManager.default.createDirectory(at: second, withIntermediateDirectories: true)
        _ = try harness.controller.addWatchFolder(at: harness.directory)
        _ = try harness.controller.addWatchFolder(at: second)
        try writeFile(harness.directory.appendingPathComponent("One.mkv"), size: 4)
        try writeFile(second.appendingPathComponent("Two.mkv"), size: 4)
        var announcements: [Int] = []
        harness.controller.heldItemsAdmittedHandler = { announcements.append($0) }
        await harness.controller.startMonitoring()
        XCTAssertEqual(harness.timers.pending.count, 2)

        harness.clock.advance(by: harness.controller.settleInterval + QueueAutomationController.rescanTolerance)
        for timer in harness.timers.pending {
            harness.timers.fire(timer)
        }
        await harness.controller.waitForPendingScans()

        XCTAssertEqual(harness.queueStore.items.count, 2)
        XCTAssertEqual(announcements, [2])
    }

    func testOnlyStartAutomaticallyFoldersAskTheQueueToStart() async throws {
        let harness = try makeHarness()
        let spy = StartRecordingAdmitter(viewModel: harness.viewModel)
        let controller = harness.makeController(admitter: spy)
        let automatic = harness.directory.deletingLastPathComponent().appendingPathComponent("Automatic", isDirectory: true)
        try FileManager.default.createDirectory(at: automatic, withIntermediateDirectories: true)
        _ = try controller.addWatchFolder(at: harness.directory, mode: .holdForReview)
        _ = try controller.addWatchFolder(at: automatic, mode: .startAutomatically)
        try writeFile(harness.directory.appendingPathComponent("Held.mkv"), size: 4)
        try writeFile(automatic.appendingPathComponent("Automatic.mkv"), size: 4)
        controller.isOtherWorkActive = { true }

        await controller.startMonitoring()
        harness.clock.advance(by: controller.settleInterval + QueueAutomationController.rescanTolerance)
        for timer in harness.timers.pending {
            harness.timers.fire(timer)
        }
        await controller.waitForPendingScans()

        let automaticItem = try XCTUnwrap(harness.queueStore.items.first { $0.intent.source.displayName == "Automatic.mkv" })
        XCTAssertEqual(spy.startRequests.map(\.itemIDs), [[automaticItem.id]])
        XCTAssertEqual(spy.startRequests.map(\.otherWorkIsActive), [true])
        XCTAssertEqual(spy.startDecisions, [.leaveWaiting(.otherWorkActive)])
        XCTAssertEqual(automaticItem.state, .waiting)
    }

    func testAutomaticStartNeverOverridesAPauseScheduleOrOtherWork() {
        XCTAssertEqual(
            AutomaticQueueStartDecision.decide(runState: .idle, hasOffPeakSchedule: false, otherWorkIsActive: false),
            .startQueue
        )
        XCTAssertEqual(
            AutomaticQueueStartDecision.decide(runState: .running, hasOffPeakSchedule: false, otherWorkIsActive: true),
            .joinRunningQueue
        )
        for (runState, hold) in [
            (PersistentQueueRunState.paused, AutomaticQueueStartHold.paused),
            (.pauseAfterCurrent, .pauseRequested),
        ] {
            XCTAssertEqual(
                AutomaticQueueStartDecision.decide(runState: runState, hasOffPeakSchedule: false, otherWorkIsActive: false),
                .leaveWaiting(hold)
            )
        }
        XCTAssertEqual(
            AutomaticQueueStartDecision.decide(runState: .idle, hasOffPeakSchedule: true, otherWorkIsActive: false),
            .leaveWaiting(.scheduled)
        )
        XCTAssertEqual(
            AutomaticQueueStartDecision.decide(runState: .idle, hasOffPeakSchedule: false, otherWorkIsActive: true),
            .leaveWaiting(.otherWorkActive)
        )
    }

    func testAutomaticStartLeavesItemsWaitingWhileAScheduleIsPending() async throws {
        let harness = try makeHarness()
        let ids = try await admit(["Scheduled.mkv"], hold: false, in: harness)
        try await harness.viewModel.saveOffPeakSchedule(
            startAt: Date().addingTimeInterval(3_600),
            endAt: Date().addingTimeInterval(7_200)
        )

        let decision = await harness.viewModel.startAutomaticallyAdmittedItems(ids, otherWorkIsActive: false)

        XCTAssertEqual(decision, .leaveWaiting(.scheduled))
        XCTAssertEqual(harness.viewModel.persistentQueueRunState, .idle)
        XCTAssertEqual(harness.queueStore.items.map(\.state), [.waiting])
        XCTAssertNotNil(harness.viewModel.offPeakSchedule)
    }

    // MARK: - Review banner

    func testReviewBannerGroupsHeldItemsByFolderAndDismissalLastsUntilSomethingNewArrives() throws {
        let rips = UUID()
        let downloads = UUID()
        let first = try makeHeldItem("A.mkv", ordinal: 0, folderID: rips, folderName: "Rips")
        let other = try makeHeldItem("B.mkv", ordinal: 1, folderID: downloads, folderName: "Downloads")
        let second = try makeHeldItem("C.mkv", ordinal: 2, folderID: rips, folderName: "Rips")
        let waiting = try makeHeldItem("D.mkv", ordinal: 3, folderID: rips, folderName: "Rips", state: .waiting)
        var state = QueueReviewBannerState()

        let groups = state.visibleGroups(for: [first, other, second, waiting])
        XCTAssertEqual(groups.map(\.folderName), ["Rips", "Downloads"])
        XCTAssertEqual(groups.map(\.itemIDs), [[first.id, second.id], [other.id]])
        XCTAssertEqual(groups.first?.firstItemID, first.id)

        state.dismiss(items: [first, other, second, waiting])
        XCTAssertTrue(state.visibleGroups(for: [first, other, second, waiting]).isEmpty)
        XCTAssertTrue(state.visibleGroups(for: [first, second]).isEmpty)

        let arrival = try makeHeldItem("E.mkv", ordinal: 4, folderID: downloads, folderName: "Downloads")
        let reshown = state.visibleGroups(for: [first, other, second, arrival])
        XCTAssertEqual(reshown.map(\.itemIDs), [[first.id, second.id], [other.id, arrival.id]])
    }

    func testStartAllReleasesOnlyThatFoldersHeldItems() async throws {
        let harness = try makeHarness()
        let ripsIDs = try await admit(["One.mkv", "Two.mkv"], hold: true, folderName: "Rips", in: harness)
        let otherIDs = try await admit(["Three.mkv"], hold: true, folderName: "Downloads", in: harness)
        // A schedule needs a runnable item; it keeps the released items from starting.
        _ = try await admit(["Manual.mkv"], hold: false, in: harness)
        try await harness.viewModel.saveOffPeakSchedule(
            startAt: Date().addingTimeInterval(3_600),
            endAt: Date().addingTimeInterval(7_200)
        )
        let group = try XCTUnwrap(
            WatchFolderReviewGroup.groups(for: harness.viewModel.persistentQueueItems)
                .first { $0.folderName == "Rips" }
        )

        let decision = try await harness.viewModel.releaseAndStartHeldPersistentQueueItems(
            group.itemIDs,
            otherWorkIsActive: false
        )

        XCTAssertEqual(decision, .leaveWaiting(.scheduled))
        let states = Dictionary(uniqueKeysWithValues: harness.queueStore.items.map { ($0.id, $0.state) })
        XCTAssertEqual(ripsIDs.map { states[$0] }, [.waiting, .waiting])
        XCTAssertEqual(otherIDs.map { states[$0] }, [.heldForReview])
    }

    // MARK: - Screenshots

    func testWatchFoldersSettingsRendersWatchingAndOfflineFolders() async throws {
        let root = try makeDirectory()
        let rips = root.appendingPathComponent("Rips", isDirectory: true)
        let offline = root.appendingPathComponent("Media Drive", isDirectory: true)
            .appendingPathComponent("Incoming", isDirectory: true)
        for directory in [rips, offline] {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        }
        let queueStore = ConversionQueueStore.inMemory()
        let viewModel = ConversionViewModel(durableQueueStore: queueStore)
        let events = FakeEventSources()
        let controller = QueueAutomationController(
            store: QueueAutomationStore.inMemory(),
            admitter: viewModel,
            draftFactory: { _ in
                QueueDraftFactory(
                    profile: BuiltInProfile.balanced.profile,
                    destinationURL: root,
                    jobOptions: JobOptions()
                )
            },
            eventSourceFactory: events.make,
            timerScheduler: ManualTimers(),
            workspaceNotificationCenter: NotificationCenter()
        )
        _ = try controller.addWatchFolder(at: rips)
        _ = try controller.addWatchFolder(
            at: offline,
            mode: .startAutomatically,
            profileID: BuiltInProfile.originalResolution.id,
            includesSubfolders: true
        )
        try FileManager.default.removeItem(at: offline)
        await controller.startMonitoring()
        XCTAssertEqual(controller.watchFolders.map { controller.status(for: $0) }, [.watching, .offline])

        let profileStore = ProfileStore(fileURL: root.appendingPathComponent("profiles.json"))
        let pane = WatchFoldersSettingsPane(controller: controller, profileStore: profileStore)
            .padding(20)
            .preferredColorScheme(.light)
        try ReviewScreenshot.capture(pane, size: CGSize(width: 860, height: 560), named: "watch-folders-settings")
        let content = pane.frame(width: 860, height: 560)
        let hostingView = NSHostingView(rootView: content)
        hostingView.appearance = NSAppearance(named: .aqua)
        hostingView.frame = NSRect(x: 0, y: 0, width: 860, height: 560)
        hostingView.layoutSubtreeIfNeeded()
        let bitmap = try XCTUnwrap(hostingView.bitmapImageRepForCachingDisplay(in: hostingView.bounds))
        hostingView.cacheDisplay(in: hostingView.bounds, to: bitmap)
        XCTAssertEqual(bitmap.pixelsWide, 860)
    }

    // MARK: - Helpers

    private final class TestClock {
        var now = Date(timeIntervalSince1970: 1_800_000_000)

        func advance(by interval: TimeInterval) {
            now = now.addingTimeInterval(interval)
        }
    }

    final class FakeEventSource: WatchFolderEventSource {
        let directoryURL: URL
        let onChange: @MainActor () -> Void
        private(set) var isStopped = false

        init(directoryURL: URL, onChange: @escaping @MainActor () -> Void) {
            self.directoryURL = directoryURL
            self.onChange = onChange
        }

        func change() {
            onChange()
        }

        func stop() {
            isStopped = true
        }
    }

    @MainActor
    final class FakeEventSources {
        private(set) var sources: [FakeEventSource] = []

        func make(directoryURL: URL, onChange: @escaping @MainActor () -> Void) -> (any WatchFolderEventSource)? {
            let source = FakeEventSource(directoryURL: directoryURL, onChange: onChange)
            sources.append(source)
            return source
        }
    }

    final class ManualTimer: QueueAutomationTimer {
        let date: Date
        let action: @MainActor () -> Void
        private(set) var isCancelled = false
        fileprivate(set) var hasFired = false

        init(date: Date, action: @escaping @MainActor () -> Void) {
            self.date = date
            self.action = action
        }

        func cancel() {
            isCancelled = true
        }
    }

    @MainActor
    final class ManualTimers: QueueAutomationTimerScheduling {
        private(set) var scheduled: [ManualTimer] = []

        var pending: [ManualTimer] {
            scheduled.filter { !$0.isCancelled && !$0.hasFired }
        }

        func schedule(at date: Date, _ action: @escaping @MainActor () -> Void) -> any QueueAutomationTimer {
            let timer = ManualTimer(date: date, action: action)
            scheduled.append(timer)
            return timer
        }

        func fire(_ timer: ManualTimer) {
            guard !timer.isCancelled, !timer.hasFired else {
                return
            }
            timer.hasFired = true
            timer.action()
        }
    }

    private final class StartRecordingAdmitter: QueueAutomationAdmitting {
        struct StartRequest: Equatable {
            let itemIDs: [UUID]
            let otherWorkIsActive: Bool
        }

        let viewModel: ConversionViewModel
        private(set) var startRequests: [StartRequest] = []
        private(set) var startDecisions: [AutomaticQueueStartDecision] = []

        init(viewModel: ConversionViewModel) {
            self.viewModel = viewModel
        }

        var runPolicyGate: (any QueueRunPolicyGating)? {
            get { viewModel.runPolicyGate }
            set { viewModel.runPolicyGate = newValue }
        }

        var queueStateChanges: AnyPublisher<Void, Never> { viewModel.queueStateChanges }

        var queueRunCompletions: AnyPublisher<Void, Never> { viewModel.queueRunCompletions }

        func postRunQueueSnapshot() -> QueuePostRunQueueSnapshot {
            viewModel.postRunQueueSnapshot()
        }

        func runPolicyQueueSnapshot(otherWorkIsActive: Bool) -> QueueRunPolicyQueueSnapshot {
            viewModel.runPolicyQueueSnapshot(otherWorkIsActive: otherWorkIsActive)
        }

        func startQueueForRunPolicy(_ itemIDs: [UUID], windowEnd: Date?) async -> PersistentQueueCommandOutcome {
            await viewModel.startQueueForRunPolicy(itemIDs, windowEnd: windowEnd)
        }

        func publishRunPolicyDecision(_ decision: QueueRunPolicyDecision) {
            viewModel.publishRunPolicyDecision(decision)
        }

        func admitAutomatedPersistentQueueDrafts(_ drafts: [AutomatedQueueDraft]) async throws -> PersistentQueueAppendResult {
            try await viewModel.admitAutomatedPersistentQueueDrafts(drafts)
        }

        func startAutomaticallyAdmittedItems(
            _ itemIDs: [UUID],
            otherWorkIsActive: Bool
        ) async -> AutomaticQueueStartDecision {
            startRequests.append(StartRequest(itemIDs: itemIDs, otherWorkIsActive: otherWorkIsActive))
            let decision = await viewModel.startAutomaticallyAdmittedItems(itemIDs, otherWorkIsActive: otherWorkIsActive)
            startDecisions.append(decision)
            return decision
        }
    }

    @MainActor
    private final class Harness {
        let root: URL
        let directory: URL
        let clock = TestClock()
        let queueStore = ConversionQueueStore.inMemory()
        let viewModel: ConversionViewModel
        let events = FakeEventSources()
        let timers = ManualTimers()
        let workspaceCenter = NotificationCenter()
        lazy var controller = makeController(admitter: viewModel)

        init(root: URL, directory: URL) {
            self.root = root
            self.directory = directory
            viewModel = ConversionViewModel(durableQueueStore: queueStore)
        }

        func makeController(admitter: any QueueAutomationAdmitting) -> QueueAutomationController {
            let root = root
            let clock = clock
            return QueueAutomationController(
                store: QueueAutomationStore.inMemory(),
                admitter: admitter,
                draftFactory: { _ in
                    QueueDraftFactory(
                        profile: BuiltInProfile.balanced.profile,
                        destinationURL: root.appendingPathComponent("Output", isDirectory: true),
                        jobOptions: JobOptions()
                    )
                },
                now: { clock.now },
                resolveBookmark: WatchFolderMonitoringTests.resolvePathBookmark,
                createBookmark: WatchFolderMonitoringTests.createPathBookmark,
                eventSourceFactory: events.make,
                timerScheduler: timers,
                workspaceNotificationCenter: workspaceCenter
            )
        }
    }

    private func makeHarness() throws -> Harness {
        let root = try makeDirectory()
        let directory = root.appendingPathComponent("Watched", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return Harness(root: root, directory: directory)
    }

    private func admit(
        _ names: [String],
        hold: Bool,
        folderName: String = "Incoming",
        in harness: Harness
    ) async throws -> [UUID] {
        let arrivedAt = Date(timeIntervalSince1970: 1_800_000_000)
        let folderID = UUID()
        var ids: [UUID] = []
        for name in names {
            let sourceURL = harness.directory.appendingPathComponent(name)
            try writeFile(sourceURL, size: 4)
            let result = try await harness.viewModel.admitAutomatedPersistentQueueDrafts([
                AutomatedQueueDraft(
                    draft: ConversionDraft(
                        source: ConversionSource(kind: .matroska, url: sourceURL),
                        sourceDetails: nil,
                        profile: BuiltInProfile.balanced.profile,
                        destinationURL: harness.root,
                        options: ConversionOptions()
                    ),
                    admission: .watchFolder(
                        id: folderID,
                        name: folderName,
                        arrivedAt: arrivedAt,
                        settledAt: arrivedAt.addingTimeInterval(60),
                        admittedAt: arrivedAt.addingTimeInterval(61)
                    ),
                    holdForReview: hold
                ),
            ])
            ids += result.addedItemIDs
        }
        return ids
    }

    private func makeHeldItem(
        _ name: String,
        ordinal: Int,
        folderID: UUID,
        folderName: String,
        state: DurableQueueItemState = .heldForReview
    ) throws -> PersistentQueueItem {
        let arrivedAt = Date(timeIntervalSince1970: 1_800_000_000)
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/Volumes/Media/\(name)")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies"),
            options: ConversionOptions()
        )
        return try PersistentQueueItem(item: DurableConversionQueueItem(
            ordinal: ordinal,
            origin: .singleSource,
            intent: DurableQueueItemIntent(draft: draft),
            state: state,
            admission: .watchFolder(
                id: folderID,
                name: folderName,
                arrivedAt: arrivedAt,
                settledAt: arrivedAt.addingTimeInterval(60),
                admittedAt: arrivedAt.addingTimeInterval(61)
            )
        ))
    }

    nonisolated private static func resolvePathBookmark(_ data: Data) throws -> (url: URL, isStale: Bool) {
        (URL(fileURLWithPath: String(decoding: data, as: UTF8.self), isDirectory: true), false)
    }

    nonisolated private static func createPathBookmark(_ url: URL) throws -> Data {
        Data(url.path.utf8)
    }

    private func makeDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("WatchFolderMonitoringTests.\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        temporaryURLs.append(url)
        return url
    }

    private func writeFile(_ url: URL, size: Int) throws {
        try Data(repeating: 0, count: size).write(to: url)
    }
}

private extension Array {
    /// The single element, or `nil` when there are none or several.
    var only: Element? {
        count == 1 ? first : nil
    }
}

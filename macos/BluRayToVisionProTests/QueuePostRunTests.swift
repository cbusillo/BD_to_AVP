import AppKit
import Combine
import SwiftUI
import XCTest
@testable import BluRayToVisionPro

final class QueuePostRunTests: XCTestCase {
    private let idle = QueuePostRunDecision.defaultIdleThreshold + 60
    private var temporaryURLs: [URL] = []

    override func tearDown() {
        for url in temporaryURLs {
            try? FileManager.default.removeItem(at: url)
        }
        temporaryURLs = []
        super.tearDown()
    }

    // MARK: - Decisions

    func testAFinishedQueueOnAnIdleMacProceedsOnlyWhenAnActionIsChosen() {
        let finished = QueuePostRunQueueSnapshot(itemStates: [.completed, .completed])
        let conditions = QueuePostRunConditions(idleTime: idle)

        XCTAssertEqual(decide(.doNothing, finished, conditions), .off)
        XCTAssertEqual(decide(.sleep, finished, conditions), .proceed(.sleep))
        XCTAssertEqual(decide(.shutDown, finished, conditions), .proceed(.shutDown))
        // An empty queue after Clear Completed is finished too.
        XCTAssertEqual(decide(.sleep, QueuePostRunQueueSnapshot(), conditions), .proceed(.sleep))
    }

    func testAQueueThatIsRunningOrPausedNeverTriggersTheAction() {
        for runState in [PersistentQueueRunState.running, .pauseAfterCurrent, .paused] {
            XCTAssertEqual(
                decide(.sleep, QueuePostRunQueueSnapshot(runState: runState, itemStates: [.completed]), QueuePostRunConditions(idleTime: idle)),
                .skip(.queueNotIdle),
                "\(runState)"
            )
        }
    }

    func testVideosThatNeedAPersonOrHaveNotFinishedKeepTheMacOn() {
        let conditions = QueuePostRunConditions(idleTime: idle)
        let cases: [([DurableQueueItemState], QueuePostRunReason)] = [
            ([.completed, .failed], .videosNeedAttention(count: 1)),
            ([.attention, .needsChoice, .interrupted], .videosNeedAttention(count: 3)),
            ([.completed, .heldForReview], .videosHeldForReview(count: 1)),
            // Left waiting by a run time ending, battery, or heat, or stopped by a person.
            ([.completed, .waiting], .videosNotFinished(count: 1)),
            ([.stopped, .notStarted], .videosNotFinished(count: 2)),
            // Attention is reported first, so the person learns about it.
            ([.waiting, .failed, .heldForReview], .videosNeedAttention(count: 1)),
        ]
        for (states, reason) in cases {
            XCTAssertEqual(decide(.shutDown, QueuePostRunQueueSnapshot(itemStates: states), conditions), .skip(reason), "\(states)")
        }
        XCTAssertEqual(
            decide(.sleep, QueuePostRunQueueSnapshot(hasPendingStartLaterSchedule: true, itemStates: [.completed]), conditions),
            .skip(.startLaterPending)
        )
    }

    func testRecentInputOrAnUnknownIdleTimeKeepsTheMacOn() {
        let finished = QueuePostRunQueueSnapshot(itemStates: [.completed])
        let threshold = QueuePostRunDecision.defaultIdleThreshold

        XCTAssertEqual(decide(.sleep, finished, QueuePostRunConditions(idleTime: threshold - 1)), .skip(.macInUse))
        XCTAssertEqual(decide(.sleep, finished, QueuePostRunConditions(idleTime: 0)), .skip(.macInUse))
        XCTAssertEqual(decide(.sleep, finished, QueuePostRunConditions(idleTime: nil)), .skip(.idleTimeUnavailable))
        XCTAssertEqual(decide(.sleep, finished, QueuePostRunConditions(idleTime: threshold)), .proceed(.sleep))
    }

    func testOtherWorkKeepsTheMacOn() {
        for work in QueuePostRunOtherWork.allCases {
            XCTAssertEqual(
                decide(.shutDown, QueuePostRunQueueSnapshot(itemStates: [.completed]), QueuePostRunConditions(idleTime: idle, otherWork: [work])),
                .skip(.otherWork(work))
            )
        }
        XCTAssertEqual(
            decide(.sleep, QueuePostRunQueueSnapshot(itemStates: [.completed], hasActiveConversion: true), QueuePostRunConditions(idleTime: idle)),
            .skip(.otherWork(.conversion))
        )
    }

    // MARK: - Countdown

    @MainActor
    func testTheActionIsRequestedOnlyAfterTheFullCountdown() async throws {
        let harness = try PostRunHarness(action: .sleep)

        await harness.controller.queueDidFinish()
        let countdown = try XCTUnwrap(harness.controller.countdown)
        XCTAssertEqual(countdown.deadline.timeIntervalSince(countdown.startedAt), QueuePostRunController.countdownDuration)
        XCTAssertEqual(harness.presented, [countdown])
        XCTAssertEqual(harness.timers.pending.count, 1)

        await harness.advance(by: QueuePostRunController.countdownDuration - 1)
        XCTAssertEqual(harness.performer.performed, [])
        XCTAssertNotNil(harness.controller.countdown)

        await harness.advance(by: 1)
        XCTAssertEqual(harness.performer.performed, [.sleep])
        XCTAssertNil(harness.controller.countdown)
        XCTAssertEqual(harness.controller.activity.map(\.kind), [.countdownStarted, .requested])
    }

    @MainActor
    func testInputDuringTheCountdownCancelsIt() async throws {
        let harness = try PostRunHarness(action: .shutDown)
        await harness.controller.queueDidFinish()
        await harness.advance(by: 20)

        harness.idleTime = 0.4
        await harness.advance(by: 1)
        await harness.advance(by: QueuePostRunController.countdownDuration)

        XCTAssertNil(harness.controller.countdown)
        XCTAssertEqual(harness.performer.performed, [])
        XCTAssertEqual(harness.controller.activity.last?.kind, .countdownCancelled)
        XCTAssertEqual(harness.controller.activity.last?.reason, .macInUse)
    }

    @MainActor
    func testCancellingChangingTheSettingOrSleepingEndsTheCountdownWithoutActing() async throws {
        let endings: [(String, QueuePostRunReason, @MainActor (PostRunHarness) async throws -> Void)] = [
            ("cancel", .cancelledByPerson, { $0.controller.cancel() }),
            ("setting", .settingChanged, { harness in
                var policy = harness.store.runPolicy
                policy.postRunAction = .doNothing
                try harness.store.saveRunPolicy(policy)
                harness.controller.settingDidChange()
            }),
            ("system sleep", .countdownInterrupted, { $0.controller.systemPowerStateDidChange() }),
            // A tick long after the deadline means the Mac slept through the countdown.
            ("late tick", .countdownInterrupted, { await $0.advance(by: QueuePostRunController.countdownDuration * 10) }),
            ("new work", .videosNotFinished(count: 1), { harness in
                harness.queue = QueuePostRunQueueSnapshot(itemStates: [.completed, .waiting])
                await harness.advance(by: 1)
            }),
            ("preview", .otherWork(.preview), { harness in
                harness.controller.otherWork = { [.preview] }
                await harness.advance(by: 1)
            }),
        ]
        for (name, reason, end) in endings {
            let harness = try PostRunHarness(action: .sleep)
            await harness.controller.queueDidFinish()
            XCTAssertNotNil(harness.controller.countdown, name)

            try await end(harness)
            await harness.advance(by: QueuePostRunController.countdownDuration)

            XCTAssertNil(harness.controller.countdown, name)
            XCTAssertEqual(harness.performer.performed, [], name)
            XCTAssertEqual(harness.controller.activity.map(\.kind), [.countdownStarted, .countdownCancelled], name)
            XCTAssertEqual(harness.controller.activity.last?.reason, reason, name)
        }
    }

    @MainActor
    func testMissingPermissionIsRecordedAndNothingHappens() async throws {
        let denied = try PostRunHarness(action: .shutDown)
        for permission in [QueuePostRunPermission.denied, .notDetermined, .unavailable] {
            denied.performer.permissionResult = permission
            await denied.controller.queueDidFinish()
            XCTAssertNil(denied.controller.countdown)
        }
        XCTAssertEqual(denied.controller.activity.map(\.kind), [.skipped, .skipped, .skipped])
        XCTAssertEqual(denied.controller.activity.map(\.reason), [.permissionDenied, .permissionDenied, .permissionDenied])
        // Asking happens only while a person chooses the setting, never when the queue finishes.
        XCTAssertEqual(denied.performer.permissionRequests, 0)

        // Permission withdrawn during the countdown stops it at the deadline.
        let revoked = try PostRunHarness(action: .shutDown)
        await revoked.controller.queueDidFinish()
        revoked.performer.permissionResult = .denied
        await revoked.advance(by: QueuePostRunController.countdownDuration)
        XCTAssertNil(revoked.controller.countdown)
        XCTAssertEqual(revoked.controller.activity.last?.reason, .permissionDenied)

        XCTAssertEqual(denied.performer.performed + revoked.performer.performed, [])
    }

    @MainActor
    func testThePerformerIsNeverCalledWhenAnySkipConditionHolds() async throws {
        let skips: [(String, @MainActor (PostRunHarness) -> Void)] = [
            ("do nothing", { try? $0.setAction(.doNothing) }),
            ("running", { $0.queue.runState = .running }),
            ("paused", { $0.queue.runState = .paused }),
            ("attention", { $0.queue.itemStates = [.completed, .attention] }),
            ("failed", { $0.queue.itemStates = [.failed] }),
            ("held", { $0.queue.itemStates = [.heldForReview] }),
            ("waiting", { $0.queue.itemStates = [.waiting] }),
            ("start later", { $0.queue.hasPendingStartLaterSchedule = true }),
            ("conversion", { $0.queue.hasActiveConversion = true }),
            ("recent input", { $0.idleTime = 30 }),
            ("unknown idle", { $0.idleTime = nil }),
            ("preview", { $0.controller.otherWork = { [.preview] } }),
            ("permission", { $0.performer.permissionResult = .denied }),
        ]
        for action in [QueuePostRunAction.sleep, .shutDown] {
            for (name, apply) in skips {
                let harness = try PostRunHarness(action: action)
                apply(harness)
                await harness.controller.queueDidFinish()
                for _ in 0...Int(QueuePostRunController.countdownDuration) {
                    await harness.advance(by: 1)
                }
                XCTAssertNil(harness.controller.countdown, name)
                XCTAssertEqual(harness.performer.performed, [], "\(action) \(name)")
                XCTAssertFalse(harness.controller.activity.contains { $0.kind == .countdownStarted }, name)
            }
        }
    }

    // MARK: - With the queue

    @MainActor
    func testACompletedRunStartsTheCountdownButAHeldRunDoesNot() async throws {
        let harness = try await QueueHarness.make(states: [.waiting, .waiting], action: .sleep)
        await harness.controller.startMonitoring()

        _ = await harness.viewModel.startPersistentQueue()
        await harness.waitUntil { harness.controller.postRun.countdown != nil }
        XCTAssertEqual(harness.queueStore.items.map(\.state), [.completed, .completed])
        XCTAssertEqual(harness.performer.performed, [])
        harness.controller.postRun.cancel()
        harness.controller.stopMonitoring()

        // A run stopped by battery power leaves videos waiting: the run did not finish, so nothing happens.
        let held = try await QueueHarness.make(states: [.waiting, .waiting], action: .sleep)
        var policy = held.controller.runPolicy
        policy.waitsOnBatteryPower = true
        try held.controller.updateRunPolicy(policy)
        held.onConversion = { [conditions = held.conditions] in
            conditions.current = QueueMachineConditions(isOnBatteryPower: true)
        }
        await held.controller.startMonitoring()
        _ = await held.viewModel.startAutomaticallyAdmittedItems(held.queueStore.items.map(\.id), otherWorkIsActive: false)
        await held.waitUntil { held.queueStore.items.first?.state == .completed && !held.viewModel.hasActiveWorker && held.viewModel.persistentQueueRunState == .idle }
        try await Task.sleep(for: .milliseconds(50))
        XCTAssertNil(held.controller.postRun.countdown)
        XCTAssertEqual(held.controller.postRun.activity, [])
        XCTAssertEqual(held.performer.performed, [])
        held.controller.stopMonitoring()
    }

    @MainActor
    func testARunThatEndsWithAFailedVideoRecordsTheSkip() async throws {
        let harness = try await QueueHarness.make(states: [.waiting, .failed], action: .shutDown)
        await harness.controller.startMonitoring()

        _ = await harness.viewModel.startPersistentQueue(onlyItemIDs: [harness.queueStore.items[0].id])
        await harness.waitUntil { !harness.controller.postRun.activity.isEmpty }

        XCTAssertNil(harness.controller.postRun.countdown)
        XCTAssertEqual(harness.controller.postRun.activity.map(\.kind), [.skipped])
        XCTAssertEqual(harness.controller.postRun.activity.last?.reason, .videosNeedAttention(count: 1))
        XCTAssertEqual(harness.performer.performed, [])
        harness.controller.stopMonitoring()
    }

    // MARK: - Persistence

    @MainActor
    func testTheSettingAndItsActivitySurviveAReload() throws {
        let fileURL = try makeDirectory().appendingPathComponent("queue.automation.json")
        let store = QueueAutomationStore(fileURL: fileURL)
        try store.saveRunPolicy(QueueRunPolicy(waitsOnBatteryPower: true, postRunAction: .shutDown))
        try store.recordPostRunActivity(QueuePostRunActivity(
            occurredAt: Date(timeIntervalSince1970: 1_800_000_000),
            action: .shutDown,
            kind: .skipped,
            reason: .videosNeedAttention(count: 2)
        ))

        let reloaded = QueueAutomationStore(fileURL: fileURL)
        XCTAssertNil(reloaded.loadErrorMessage)
        XCTAssertEqual(reloaded.runPolicy, store.runPolicy)
        XCTAssertEqual(reloaded.postRunActivity, store.postRunActivity)
    }

    @MainActor
    func testAnActivityReasonThisBuildNoLongerHasStillLoads() throws {
        let fileURL = try makeDirectory().appendingPathComponent("queue.automation.json")
        let store = QueueAutomationStore(fileURL: fileURL)
        try store.recordPostRunActivity(QueuePostRunActivity(
            occurredAt: Date(timeIntervalSince1970: 1_800_000_000),
            action: .sleep,
            kind: .skipped,
            reason: .otherWork(.preview)
        ))
        var file = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: fileURL)) as? [String: Any])
        var activity = try XCTUnwrap(file["postRunActivity"] as? [[String: Any]])
        activity[0]["reason"] = ["otherWork": ["_0": "retiredWork"]]
        file["postRunActivity"] = activity
        try JSONSerialization.data(withJSONObject: file).write(to: fileURL)

        let reloaded = QueueAutomationStore(fileURL: fileURL)
        XCTAssertNil(reloaded.loadErrorMessage)
        XCTAssertFalse(reloaded.writesBlocked)
        XCTAssertEqual(reloaded.postRunActivity.map(\.kind), [.skipped])
        XCTAssertNil(reloaded.postRunActivity.first?.reason)
    }

    @MainActor
    func testARunPolicyFromBeforeTheSettingReadsAsDoNothing() throws {
        // A file as the run-times build wrote it, before this setting existed.
        let earlier = try JSONEncoder().encode(EarlierAutomationFile(
            version: QueueAutomationDocument.currentVersion,
            runPolicy: ["waitsOnBatteryPower": true]
        ))
        let fileURL = try makeDirectory().appendingPathComponent("queue.automation.json")
        try earlier.write(to: fileURL)

        let store = QueueAutomationStore(fileURL: fileURL)
        XCTAssertNil(store.loadErrorMessage)
        XCTAssertFalse(store.writesBlocked)
        XCTAssertTrue(store.runPolicy.waitsOnBatteryPower)
        XCTAssertEqual(store.runPolicy.postRunAction, .doNothing)
        XCTAssertEqual(store.postRunActivity, [])

        // An action from a later build that this one does not know is never acted on.
        let later = try JSONSerialization.data(withJSONObject: [
            "version": QueueAutomationDocument.currentVersion,
            "watchFolders": [],
            "seenSources": [],
            "runPolicy": ["postRunAction": "restart"],
            "runPolicyActivity": [],
        ])
        try later.write(to: fileURL)
        let laterStore = QueueAutomationStore(fileURL: fileURL)
        XCTAssertNil(laterStore.loadErrorMessage)
        XCTAssertEqual(laterStore.runPolicy.postRunAction, .doNothing)
    }

    // MARK: - Screenshots

    @MainActor
    func testScheduleSettingsShowTheShutDownWarningAndTheCountdownRenders() async throws {
        let harness = try PostRunHarness(action: .shutDown)
        let controller = QueueAutomationController(
            store: harness.store,
            admitter: ConversionViewModel(durableQueueStore: ConversionQueueStore.inMemory()),
            draftFactory: { _ in
                QueueDraftFactory(profile: BuiltInProfile.balanced.profile, destinationURL: URL(fileURLWithPath: "/tmp/Output"), jobOptions: JobOptions())
            },
            timerScheduler: harness.timers,
            workspaceNotificationCenter: NotificationCenter(),
            idleTime: { nil },
            postRunPerformer: harness.performer
        )
        XCTAssertEqual(controller.postRun.action, .shutDown)

        for (scheme, appearance, suffix) in [(ColorScheme.light, NSAppearance.Name.aqua, "light"), (.dark, .darkAqua, "dark")] {
            try ReviewScreenshot.capture(
                QueueScheduleSettingsPane(controller: controller).padding(20).preferredColorScheme(scheme),
                size: CGSize(width: 860, height: 620),
                appearance: appearance,
                named: "post-run-schedule-settings-shut-down-\(suffix)"
            )
        }

        let startedAt = Date()
        let countdown = QueuePostRunCountdown(action: .shutDown, startedAt: startedAt, deadline: startedAt.addingTimeInterval(QueuePostRunController.countdownDuration))
        XCTAssertEqual(countdown.title(at: startedAt.addingTimeInterval(17.2)), "The Mac will shut down in 43 seconds")
        try ReviewScreenshot.capture(
            QueuePostRunCountdownView(countdown: countdown, cancel: {}, displayDate: startedAt.addingTimeInterval(17)).preferredColorScheme(.light),
            size: CGSize(width: 380, height: 280),
            named: "post-run-countdown-light"
        )
    }

    // MARK: - Helpers

    private func decide(_ action: QueuePostRunAction, _ queue: QueuePostRunQueueSnapshot, _ conditions: QueuePostRunConditions) -> QueuePostRunDecision {
        QueuePostRunDecision.decide(action: action, queue: queue, conditions: conditions)
    }

    private func makeDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("QueuePostRunTests.\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        temporaryURLs.append(url)
        return url
    }
}

private struct EarlierAutomationFile: Encodable {
    let version: Int
    let watchFolders: [String] = []
    let seenSources: [String] = []
    let runPolicy: [String: Bool]
    let runPolicyActivity: [String] = []
}

/// Records what it was asked to do; it never touches the Mac's power state.
@MainActor
final class FakePostRunPerformer: QueuePostRunPerforming {
    var permissionResult = QueuePostRunPermission.granted
    var requestResult = QueuePostRunPermission.granted
    private(set) var performed: [QueuePostRunAction] = []
    private(set) var permissionRequests = 0

    func permission(for action: QueuePostRunAction) async -> QueuePostRunPermission {
        permissionResult
    }

    func requestPermission(for action: QueuePostRunAction) async -> QueuePostRunPermission {
        permissionRequests += 1
        return requestResult
    }

    func perform(_ action: QueuePostRunAction) async throws {
        performed.append(action)
    }
}

/// A post-run controller over a queue snapshot, idle time, and clock the test sets directly.
@MainActor
private final class PostRunHarness {
    let store = QueueAutomationStore.inMemory()
    let performer = FakePostRunPerformer()
    let timers = WatchFolderMonitoringTests.ManualTimers()
    var clock = Date(timeIntervalSince1970: 1_800_000_000)
    var queue = QueuePostRunQueueSnapshot(itemStates: [.completed])
    var idleTime: TimeInterval? = QueuePostRunDecision.defaultIdleThreshold + 60
    private(set) var presented: [QueuePostRunCountdown] = []
    lazy var controller: QueuePostRunController = {
        let controller = QueuePostRunController(
            store: store,
            queueSnapshot: { [unowned self] in queue },
            idleTime: { [unowned self] in
                // Idle time keeps growing while nobody touches the Mac.
                idleTime
            },
            performer: performer,
            timerScheduler: timers,
            now: { [unowned self] in clock }
        )
        controller.countdownStartedHandler = { [unowned self] in presented.append($0) }
        return controller
    }()

    init(action: QueuePostRunAction) throws {
        try setAction(action)
    }

    func setAction(_ action: QueuePostRunAction) throws {
        var policy = store.runPolicy
        policy.postRunAction = action
        try store.saveRunPolicy(policy)
    }

    func advance(by seconds: TimeInterval) async {
        clock = clock.addingTimeInterval(seconds)
        if let idleTime, idleTime >= QueuePostRunDecision.defaultIdleThreshold {
            self.idleTime = idleTime + seconds
        }
        await controller.tick()
    }
}

private final class PostRunMachineConditions: QueueMachineConditionsProviding, @unchecked Sendable {
    private let lock = NSLock()
    nonisolated(unsafe) private var stored = QueueMachineConditions()

    nonisolated var current: QueueMachineConditions {
        get { lock.withLock { stored } }
        set { lock.withLock { stored = newValue } }
    }

    func startObserving(_ onChange: @escaping @MainActor () -> Void) {}

    func stopObserving() {}
}

private final class PostRunConversionAction: @unchecked Sendable {
    private let lock = NSLock()
    private var action: (@Sendable () -> Void)?

    var value: (@Sendable () -> Void)? {
        get { lock.withLock { action } }
        set { lock.withLock { action = newValue } }
    }
}

/// A real queue and automation controller with a fake converter and a fake performer.
@MainActor
private final class QueueHarness {
    let conditions = PostRunMachineConditions()
    let queueStore = ConversionQueueStore.inMemory()
    let performer = FakePostRunPerformer()
    let viewModel: ConversionViewModel
    let controller: QueueAutomationController
    private let conversionAction = PostRunConversionAction()
    var onConversion: (@Sendable () -> Void)? {
        get { conversionAction.value }
        set { conversionAction.value = newValue }
    }

    private init(action: QueuePostRunAction) throws {
        let conversionAction = conversionAction
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { _ in
            conversionAction.value?()
        })
        viewModel = ConversionViewModel(
            clientFactory: { worker },
            durableQueueStore: queueStore,
            offPeakScheduleStore: OffPeakScheduleStore.inMemory(),
            sourceAvailabilityResolver: { _ in true }
        )
        let store = QueueAutomationStore.inMemory()
        try store.saveRunPolicy(QueueRunPolicy(postRunAction: action))
        controller = QueueAutomationController(
            store: store,
            admitter: viewModel,
            draftFactory: { _ in
                QueueDraftFactory(profile: BuiltInProfile.balanced.profile, destinationURL: URL(fileURLWithPath: "/tmp/Output"), jobOptions: JobOptions())
            },
            timerScheduler: WatchFolderMonitoringTests.ManualTimers(),
            workspaceNotificationCenter: NotificationCenter(),
            machineConditions: conditions,
            idleTime: { QueuePostRunDecision.defaultIdleThreshold * 10 },
            postRunPerformer: performer
        )
    }

    static func make(states: [DurableQueueItemState], action: QueuePostRunAction) async throws -> QueueHarness {
        let harness = try QueueHarness(action: action)
        let items = states.enumerated().map { ordinal, state in
            DurableConversionQueueItem(
                ordinal: ordinal,
                origin: .singleSource,
                intent: DurableQueueItemIntent(draft: ConversionDraft(
                    source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/tmp/post-run-\(ordinal).mkv")),
                    sourceDetails: nil,
                    profile: BuiltInProfile.balanced.profile,
                    destinationURL: URL(fileURLWithPath: "/tmp/Output"),
                    options: ConversionOptions()
                )),
                state: state,
                failure: state == .failed
                    ? DurableQueueFailure(code: "test", message: "The conversion failed.", details: nil, retryable: true)
                    : nil
            )
        }
        try await harness.queueStore.replaceItems(items)
        return harness
    }

    func waitUntil(
        _ condition: @MainActor () -> Bool,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async {
        for _ in 0..<2_000 where !condition() {
            await Task.yield()
            try? await Task.sleep(for: .milliseconds(1))
        }
        if !condition() {
            XCTFail("Timed out waiting for the queue", file: file, line: line)
        }
    }
}

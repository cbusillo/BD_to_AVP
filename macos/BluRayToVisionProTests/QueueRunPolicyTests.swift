import AppKit
import Combine
import SwiftUI
import XCTest
@testable import BluRayToVisionPro

final class QueueRunPolicyTests: XCTestCase {
    private static let newYork: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/New_York")!
        calendar.locale = Locale(identifier: "en_US")
        return calendar
    }()

    private var calendar: Calendar { Self.newYork }
    private var temporaryURLs: [URL] = []

    override func tearDown() {
        for url in temporaryURLs {
            try? FileManager.default.removeItem(at: url)
        }
        temporaryURLs = []
        super.tearDown()
    }

    // MARK: - Run-time evaluation

    func testRunTimeThatCrossesMidnightRunsIntoTheNextDayOnlyFromItsStartDays() {
        // Friday nights only, 10:00 PM until 6:00 AM.
        let policy = windowsPolicy([window(days: [.friday], from: 22, to: 6)])

        XCTAssertNil(evaluate(policy, at: date(2026, 10, 16, 21, 59)).currentWindow)
        XCTAssertEqual(
            evaluate(policy, at: date(2026, 10, 16, 21, 59)).holds,
            [.outsideRunTimes(nextStart: date(2026, 10, 16, 22, 0))]
        )
        XCTAssertEqual(
            evaluate(policy, at: date(2026, 10, 16, 22, 0)).currentWindow,
            DateInterval(start: date(2026, 10, 16, 22, 0), end: date(2026, 10, 17, 6, 0))
        )
        XCTAssertTrue(evaluate(policy, at: date(2026, 10, 17, 5, 59)).allowsStarting)
        XCTAssertFalse(evaluate(policy, at: date(2026, 10, 17, 6, 0)).allowsStarting)
        // Saturday is not a start day, so Saturday night stays closed until next Friday.
        XCTAssertEqual(
            evaluate(policy, at: date(2026, 10, 17, 23, 0)).holds,
            [.outsideRunTimes(nextStart: date(2026, 10, 23, 22, 0))]
        )
    }

    func testRunTimeStartIsInclusiveAndEndIsExclusive() {
        let policy = windowsPolicy([window(days: QueueWeekday.everyDay, from: 1, to: 7)])

        XCTAssertFalse(evaluate(policy, at: date(2026, 10, 16, 0, 59)).allowsStarting)
        XCTAssertTrue(evaluate(policy, at: date(2026, 10, 16, 1, 0)).allowsStarting)
        let atEnd = evaluate(policy, at: date(2026, 10, 16, 7, 0))
        XCTAssertNil(atEnd.currentWindow)
        XCTAssertEqual(atEnd.nextWindowStart, date(2026, 10, 17, 1, 0))
        XCTAssertEqual(evaluate(policy, at: date(2026, 10, 16, 3, 0)).nextBoundary, date(2026, 10, 16, 7, 0))
    }

    func testWeekdayRunTimesSkipTheWeekend() {
        let policy = windowsPolicy([window(days: QueueWeekday.weekdays, from: 1, to: 7)])

        XCTAssertTrue(evaluate(policy, at: date(2026, 10, 16, 2, 0)).allowsStarting)
        XCTAssertEqual(
            evaluate(policy, at: date(2026, 10, 17, 2, 0)).holds,
            [.outsideRunTimes(nextStart: date(2026, 10, 19, 1, 0))]
        )
    }

    func testRunTimesFollowTheWallClockAcrossDaylightSavingChanges() {
        let nightly = windowsPolicy([window(days: QueueWeekday.everyDay, from: 1, to: 7)])

        // Spring forward: the night is an hour shorter but still ends at 7:00 AM local time.
        let spring = evaluate(nightly, at: date(2026, 3, 8, 6, 59)).currentWindow
        XCTAssertEqual(spring?.end, date(2026, 3, 8, 7, 0))
        XCTAssertEqual(spring?.duration, 5 * 3_600)
        // A start time the clock skips opens the run time when the clock jumps.
        let skipped = windowsPolicy([window(days: [.sunday], from: QueueClockTime(hour: 2, minute: 30), to: QueueClockTime(hour: 4))])
        XCTAssertEqual(evaluate(skipped, at: date(2026, 3, 8, 0, 0)).nextWindowStart, date(2026, 3, 8, 3, 0))

        // Fall back: the repeated hour is inside the run time and the night is an hour longer.
        let fall = evaluate(nightly, at: date(2026, 11, 1, 6, 30)).currentWindow
        XCTAssertEqual(fall?.end, date(2026, 11, 1, 7, 0))
        XCTAssertEqual(fall?.duration, 7 * 3_600)
        let repeatedHour = date(2026, 11, 1, 1, 30).addingTimeInterval(3_600)
        XCTAssertEqual(calendar.component(.hour, from: repeatedHour), 1)
        XCTAssertTrue(evaluate(nightly, at: repeatedHour).allowsStarting)
    }

    func testAdjoiningRunTimesJoinAndMatchingStartAndEndCoversAFullDay() {
        let adjoining = windowsPolicy([
            window(days: [.friday], from: 22, to: 0),
            window(days: [.saturday], from: 0, to: 6),
        ])
        XCTAssertEqual(evaluate(adjoining, at: date(2026, 10, 16, 23, 0)).currentWindow?.end, date(2026, 10, 17, 6, 0))

        let allDay = windowsPolicy([window(days: [.saturday], from: 9, to: 9)])
        XCTAssertEqual(
            evaluate(allDay, at: date(2026, 10, 18, 8, 59)).currentWindow,
            DateInterval(start: date(2026, 10, 17, 9, 0), end: date(2026, 10, 18, 9, 0))
        )
    }

    func testMachineConditionsHoldOnlyWhenChosen() {
        let now = date(2026, 10, 16, 12, 0)
        let battery = QueueMachineConditions(isOnBatteryPower: true)
        let hot = QueueMachineConditions(thermalState: .serious)

        XCTAssertTrue(QueueRunPolicyEvaluation.evaluate(.none, at: now, conditions: battery, calendar: calendar).allowsStarting)
        let waitsOnBattery = QueueRunPolicy(waitsOnBatteryPower: true)
        XCTAssertEqual(evaluate(waitsOnBattery, at: now, conditions: battery).holds, [.onBatteryPower])
        XCTAssertTrue(evaluate(waitsOnBattery, at: now, conditions: hot).allowsStarting)

        let waitsWhenHot = QueueRunPolicy(waitsUnderThermalPressure: true)
        XCTAssertEqual(evaluate(waitsWhenHot, at: now, conditions: hot).holds, [.thermalPressure])
        XCTAssertEqual(
            evaluate(waitsWhenHot, at: now, conditions: QueueMachineConditions(thermalState: .critical)).holds,
            [.thermalPressure]
        )
        XCTAssertTrue(evaluate(waitsWhenHot, at: now, conditions: QueueMachineConditions(thermalState: .fair)).allowsStarting)

        // A Start Later window ignores run times but not machine conditions.
        var everything = windowsPolicy([window(days: [.monday], from: 1, to: 2)])
        everything.waitsOnBatteryPower = true
        let held = evaluate(everything, at: now, conditions: battery)
        XCTAssertEqual(held.hold(for: .all)?.kind, .outsideRunTimes)
        XCTAssertEqual(held.hold(for: .machineConditionsOnly), .onBatteryPower)
        XCTAssertNil(evaluate(everything, at: now).hold(for: .machineConditionsOnly))
    }

    // MARK: - Decisions

    func testPolicyNeverOverridesAPauseOrAPendingStartLaterWindow() {
        let policy = windowsPolicy([window(days: QueueWeekday.everyDay, from: 1, to: 7)])
        let inside = evaluate(policy, at: date(2026, 10, 16, 2, 0))
        let waiting = [UUID(), UUID()]

        for runState in [PersistentQueueRunState.paused, .pauseAfterCurrent] {
            XCTAssertEqual(
                QueueRunPolicyDecision.decide(policy: policy, evaluation: inside, queue: QueueRunPolicyQueueSnapshot(runState: runState, waitingItemIDs: waiting)),
                .inactive
            )
        }
        XCTAssertEqual(
            QueueRunPolicyDecision.decide(
                policy: policy,
                evaluation: inside,
                queue: QueueRunPolicyQueueSnapshot(hasPendingStartLaterSchedule: true, waitingItemIDs: waiting)
            ),
            .inactive
        )
        XCTAssertEqual(
            QueueRunPolicyDecision.decide(policy: policy, evaluation: inside, queue: QueueRunPolicyQueueSnapshot(waitingItemIDs: waiting)),
            .start(itemIDs: waiting, windowEnd: nil)
        )
        XCTAssertEqual(
            QueueRunPolicyDecision.decide(
                policy: policy,
                evaluation: inside,
                queue: QueueRunPolicyQueueSnapshot(isOtherWorkActive: true, waitingItemIDs: waiting)
            ),
            .blockedByOtherWork(itemCount: 2)
        )
        XCTAssertEqual(
            QueueRunPolicyDecision.decide(
                policy: policy,
                evaluation: inside,
                queue: QueueRunPolicyQueueSnapshot(runState: .running, isRunningUnderPolicy: true)
            ),
            .running(until: date(2026, 10, 16, 7, 0))
        )
    }

    func testWithoutRunTimesOnlyDeferredItemsStartWhenAConditionClears() {
        let policy = QueueRunPolicy(waitsOnBatteryPower: true)
        let deferred = UUID()
        let personAdded = UUID()
        let snapshot = QueueRunPolicyQueueSnapshot(waitingItemIDs: [personAdded, deferred], deferredItemIDs: [deferred])
        let now = date(2026, 10, 16, 12, 0)

        XCTAssertEqual(
            QueueRunPolicyDecision.decide(
                policy: policy,
                evaluation: evaluate(policy, at: now, conditions: QueueMachineConditions(isOnBatteryPower: true)),
                queue: snapshot
            ),
            .waiting(.onBatteryPower, itemCount: 1)
        )
        XCTAssertEqual(
            QueueRunPolicyDecision.decide(policy: policy, evaluation: evaluate(policy, at: now), queue: snapshot),
            .start(itemIDs: [deferred], windowEnd: nil)
        )
        XCTAssertEqual(
            QueueRunPolicyDecision.decide(
                policy: policy,
                evaluation: evaluate(policy, at: now),
                queue: QueueRunPolicyQueueSnapshot(waitingItemIDs: [personAdded])
            ),
            .inactive
        )
    }

    func testItemsDeferredFromAStartLaterRunKeepThatWindowAndIgnoreRunTimes() {
        var policy = windowsPolicy([window(days: [.monday], from: 1, to: 2)])
        policy.waitsOnBatteryPower = true
        let item = UUID()
        let windowEnd = date(2026, 10, 16, 14, 0)
        let snapshot = QueueRunPolicyQueueSnapshot(waitingItemIDs: [item], deferredItemIDs: [item], deferredWindowEnd: windowEnd)

        XCTAssertEqual(
            QueueRunPolicyDecision.decide(policy: policy, evaluation: evaluate(policy, at: date(2026, 10, 16, 13, 0)), queue: snapshot),
            .start(itemIDs: [item], windowEnd: windowEnd)
        )
        XCTAssertEqual(
            QueueRunPolicyDecision.decide(policy: policy, evaluation: evaluate(policy, at: windowEnd), queue: snapshot),
            .waiting(.outsideRunTimes(nextStart: date(2026, 10, 19, 1, 0)), itemCount: 1)
        )
    }

    // MARK: - Queue behavior

    @MainActor
    func testLaunchInsideARunTimeStartsOnlyWaitingItems() async throws {
        let harness = try await PolicyHarness.make(now: date(2026, 10, 16, 2, 0), calendar: calendar, states: [.waiting, .interrupted, .stopped, .heldForReview])
        try harness.controller.updateRunPolicy(windowsPolicy([window(days: QueueWeekday.everyDay, from: 1, to: 7)]))

        await harness.controller.startMonitoring()
        await harness.waitUntil { harness.queueStore.items.first?.state == .completed && !harness.viewModel.hasActiveWorker && harness.viewModel.persistentQueueRunState == .idle }

        XCTAssertEqual(harness.convertedPaths, [harness.path(0)])
        XCTAssertEqual(harness.queueStore.items.map(\.state), [.completed, .interrupted, .stopped, .heldForReview])
        XCTAssertEqual(harness.controller.runPolicyActivity.map(\.kind), [.started])
        harness.controller.stopMonitoring()
    }

    @MainActor
    func testOutsideARunTimeTheQueueWaitsAndStartsWhenTheRunTimeOpens() async throws {
        let harness = try await PolicyHarness.make(now: date(2026, 10, 16, 23, 0), calendar: calendar, states: [.waiting])
        try harness.controller.updateRunPolicy(windowsPolicy([window(days: QueueWeekday.everyDay, from: 1, to: 7)]))

        await harness.controller.startMonitoring()

        let opening = date(2026, 10, 17, 1, 0)
        XCTAssertEqual(harness.viewModel.runPolicyDecision, .waiting(.outsideRunTimes(nextStart: opening), itemCount: 1))
        XCTAssertEqual(harness.viewModel.persistentQueueRunState, .idle)
        XCTAssertEqual(harness.controller.runPolicyActivity.map(\.kind), [.held])
        let timer = try XCTUnwrap(harness.timers.pending.last)
        XCTAssertEqual(timer.date, date(2026, 10, 16, 23, 1))

        harness.clock.value = opening
        harness.timers.fire(timer)
        await harness.controller.waitForRunPolicyEvaluation()
        await harness.waitUntil { harness.queueStore.items.first?.state == .completed && !harness.viewModel.hasActiveWorker && harness.viewModel.persistentQueueRunState == .idle }

        XCTAssertEqual(harness.convertedPaths, [harness.path(0)])
        XCTAssertEqual(harness.controller.runPolicyActivity.map(\.kind), [.held, .started])
        harness.controller.stopMonitoring()
    }

    @MainActor
    func testARunTimeMissedWhileTheMacSleptIsNotMadeUp() async throws {
        let harness = try await PolicyHarness.make(now: date(2026, 10, 16, 23, 0), calendar: calendar, states: [.waiting])
        try harness.controller.updateRunPolicy(windowsPolicy([window(days: QueueWeekday.everyDay, from: 1, to: 7)]))
        await harness.controller.startMonitoring()

        // The Mac slept through the whole run time and wakes at 8:00 AM.
        harness.clock.value = date(2026, 10, 17, 8, 0)
        harness.workspaceCenter.post(name: NSWorkspace.didWakeNotification, object: nil)
        let expected = QueueRunPolicyDecision.waiting(.outsideRunTimes(nextStart: date(2026, 10, 18, 1, 0)), itemCount: 1)
        await harness.waitUntil { harness.viewModel.runPolicyDecision == expected }
        await harness.controller.waitForRunPolicyEvaluation()

        XCTAssertEqual(harness.viewModel.runPolicyDecision, expected)
        XCTAssertTrue(harness.convertedPaths.isEmpty)
        XCTAssertEqual(harness.queueStore.items.map(\.state), [.waiting])
        harness.controller.stopMonitoring()
    }

    @MainActor
    func testRunTimeEndLetsTheCurrentVideoFinishAndStartsNoOtherUntilTheNextRunTime() async throws {
        let harness = try await PolicyHarness.make(now: date(2026, 10, 16, 6, 30), calendar: calendar, states: [.waiting, .waiting])
        let end = date(2026, 10, 16, 7, 0)
        harness.onConversion = { [clock = harness.clock] in clock.value = end }
        try harness.controller.updateRunPolicy(windowsPolicy([window(days: QueueWeekday.everyDay, from: 1, to: 7)]))

        await harness.controller.evaluateRunPolicy()
        await harness.waitUntil { harness.queueStore.items.first?.state == .completed && !harness.viewModel.hasActiveWorker && harness.viewModel.persistentQueueRunState == .idle }

        XCTAssertEqual(harness.convertedPaths, [harness.path(0)])
        XCTAssertEqual(harness.queueStore.items.map(\.state), [.completed, .waiting])
        // The queue is idle, not paused, so the next run time can start it.
        XCTAssertEqual(harness.viewModel.persistentQueueRunState, .idle)
        XCTAssertEqual(harness.controller.runPolicyActivity.map(\.kind), [.started, .stoppedStartingNewItems])

        harness.onConversion = nil
        harness.clock.value = date(2026, 10, 17, 1, 0)
        await harness.controller.evaluateRunPolicy()
        await harness.waitUntil { harness.queueStore.items.last?.state == .completed && !harness.viewModel.hasActiveWorker }
        XCTAssertEqual(harness.convertedPaths, [harness.path(0), harness.path(1)])
    }

    @MainActor
    func testPressingStartRunsTheWholeQueueOutsideRunTimes() async throws {
        let harness = try await PolicyHarness.make(now: date(2026, 10, 16, 12, 0), calendar: calendar, states: [.waiting, .waiting])
        var policy = windowsPolicy([window(days: QueueWeekday.everyDay, from: 1, to: 7)])
        policy.waitsOnBatteryPower = true
        try harness.controller.updateRunPolicy(policy)
        harness.conditions.current = QueueMachineConditions(isOnBatteryPower: true)

        _ = await harness.viewModel.startPersistentQueue()
        await harness.waitUntil { harness.queueStore.items.last?.state == .completed && !harness.viewModel.hasActiveWorker }

        XCTAssertEqual(harness.convertedPaths, [harness.path(0), harness.path(1)])
    }

    @MainActor
    func testAPendingStartLaterWindowTakesPrecedenceOverRunTimes() async throws {
        let harness = try await PolicyHarness.make(now: date(2026, 10, 16, 2, 0), calendar: calendar, states: [.waiting])
        try await harness.viewModel.saveOffPeakSchedule(startAt: date(2026, 10, 16, 12, 0), endAt: date(2026, 10, 16, 14, 0))
        let schedule = try XCTUnwrap(harness.viewModel.offPeakSchedule)
        try harness.controller.updateRunPolicy(windowsPolicy([window(days: QueueWeekday.everyDay, from: 1, to: 7)]))

        let decision = await harness.controller.evaluateRunPolicy()

        XCTAssertEqual(decision, .inactive)
        XCTAssertEqual(harness.viewModel.persistentQueueRunState, .idle)
        XCTAssertNotNil(harness.viewModel.offPeakSchedule)
        XCTAssertTrue(harness.convertedPaths.isEmpty)

        // The Start Later window runs at its own time, outside every run time.
        harness.clock.value = date(2026, 10, 16, 12, 0)
        let evaluation = await harness.viewModel.evaluateOffPeakSchedule()
        XCTAssertEqual(evaluation, .start(schedule))
        await harness.waitUntil { harness.queueStore.items.first?.state == .completed && !harness.viewModel.hasActiveWorker && harness.viewModel.persistentQueueRunState == .idle }
        XCTAssertEqual(harness.convertedPaths, [harness.path(0)])
    }

    @MainActor
    func testStartLaterWindowWaitsForPowerAndIsMissedForThatReasonIfItEnds() async throws {
        let harness = try await PolicyHarness.make(now: date(2026, 10, 16, 11, 0), calendar: calendar, states: [.waiting])
        try harness.controller.updateRunPolicy(QueueRunPolicy(waitsOnBatteryPower: true))
        try await harness.viewModel.saveOffPeakSchedule(startAt: date(2026, 10, 16, 12, 0), endAt: date(2026, 10, 16, 14, 0))
        harness.conditions.current = QueueMachineConditions(isOnBatteryPower: true)

        harness.clock.value = date(2026, 10, 16, 12, 0)
        let held = await harness.viewModel.evaluateOffPeakSchedule()
        guard case .waiting = held else {
            return XCTFail("Expected the window to wait for power, got \(held)")
        }
        XCTAssertNotNil(harness.viewModel.offPeakSchedule)

        harness.clock.value = date(2026, 10, 16, 14, 0)
        _ = await harness.viewModel.evaluateOffPeakSchedule()
        XCTAssertNil(harness.viewModel.offPeakSchedule)
        XCTAssertEqual(harness.scheduleStore.lastOutcome?.missReason, .windowEndedWhileHeldByRunPolicy)
        XCTAssertTrue(harness.convertedPaths.isEmpty)
    }

    @MainActor
    func testBatteryPowerStopsStartingNewVideosAndTheRunResumesWhenPowerReturns() async throws {
        let harness = try await PolicyHarness.make(now: date(2026, 10, 16, 12, 0), calendar: calendar, states: [.waiting, .waiting, .waiting])
        try harness.controller.updateRunPolicy(QueueRunPolicy(waitsOnBatteryPower: true))
        harness.onConversion = { [conditions = harness.conditions] in
            conditions.current = QueueMachineConditions(isOnBatteryPower: true)
        }
        let automatic = Array(harness.queueStore.items.prefix(2).map(\.id))

        let decision = await harness.viewModel.startAutomaticallyAdmittedItems(automatic, otherWorkIsActive: false)
        XCTAssertEqual(decision, .startQueue)
        await harness.waitUntil { harness.queueStore.items.first?.state == .completed && !harness.viewModel.hasActiveWorker && harness.viewModel.persistentQueueRunState == .idle }

        // The running video finished; the next one waits for power instead of starting.
        XCTAssertEqual(harness.convertedPaths, [harness.path(0)])
        XCTAssertEqual(harness.queueStore.items.map(\.state), [.completed, .waiting, .waiting])
        XCTAssertEqual(harness.viewModel.persistentQueueRunState, .idle)
        let waitingForPower = await harness.controller.evaluateRunPolicy()
        XCTAssertEqual(waitingForPower, .waiting(.onBatteryPower, itemCount: 1))

        harness.onConversion = nil
        harness.conditions.current = QueueMachineConditions()
        await harness.controller.evaluateRunPolicy()
        await harness.waitUntil { harness.queueStore.items[1].state == .completed && !harness.viewModel.hasActiveWorker && harness.viewModel.persistentQueueRunState == .idle }

        // Only the automatic run resumes; the item a person left waiting still waits for them.
        XCTAssertEqual(harness.convertedPaths, [harness.path(0), harness.path(1)])
        XCTAssertEqual(harness.queueStore.items.map(\.state), [.completed, .completed, .waiting])
        XCTAssertEqual(
            harness.controller.runPolicyActivity.map(\.kind),
            [.stoppedStartingNewItems, .held, .started]
        )
    }

    @MainActor
    func testThermalPressureHoldsAnAutomaticStartUntilTheMacCools() async throws {
        let harness = try await PolicyHarness.make(now: date(2026, 10, 16, 12, 0), calendar: calendar, states: [.waiting])
        try harness.controller.updateRunPolicy(QueueRunPolicy(waitsUnderThermalPressure: true))
        harness.conditions.current = QueueMachineConditions(thermalState: .serious)
        let itemIDs = harness.queueStore.items.map(\.id)

        let decision = await harness.viewModel.startAutomaticallyAdmittedItems(itemIDs, otherWorkIsActive: false)
        XCTAssertEqual(decision, .leaveWaiting(.runPolicy(.thermalPressure)))
        XCTAssertEqual(harness.viewModel.persistentQueueRunState, .idle)

        harness.conditions.current = QueueMachineConditions(thermalState: .fair)
        await harness.controller.evaluateRunPolicy()
        await harness.waitUntil { harness.queueStore.items.first?.state == .completed && !harness.viewModel.hasActiveWorker && harness.viewModel.persistentQueueRunState == .idle }
        XCTAssertEqual(harness.convertedPaths, [harness.path(0)])
    }

    @MainActor
    func testAutomaticStartOutsideRunTimesWaitsForTheRunTime() async throws {
        let harness = try await PolicyHarness.make(now: date(2026, 10, 16, 12, 0), calendar: calendar, states: [.waiting])
        try harness.controller.updateRunPolicy(windowsPolicy([window(days: QueueWeekday.everyDay, from: 1, to: 7)]))

        let decision = await harness.viewModel.startAutomaticallyAdmittedItems(
            harness.queueStore.items.map(\.id),
            otherWorkIsActive: false
        )

        XCTAssertEqual(decision, .leaveWaiting(.runPolicy(.outsideRunTimes(nextStart: date(2026, 10, 17, 1, 0)))))
        XCTAssertTrue(harness.convertedPaths.isEmpty)
    }

    // MARK: - Persistence

    @MainActor
    func testWatchFolderFileFromBeforeRunPoliciesLoadsAndStaysReadableByEarlierBuilds() throws {
        let fixtureURL = try XCTUnwrap(
            Bundle(for: Self.self).url(forResource: "queue-automation-v1-watch-folders", withExtension: "json")
        )
        let fileURL = try makeDirectory().appendingPathComponent("queue.automation.json")
        try FileManager.default.copyItem(at: fixtureURL, to: fileURL)

        let store = QueueAutomationStore(fileURL: fileURL)
        XCTAssertNil(store.loadErrorMessage)
        XCTAssertFalse(store.writesBlocked)
        XCTAssertEqual(store.watchFolders.map(\.mode), [.startAutomatically])
        XCTAssertEqual(store.seenSources.count, 1)
        XCTAssertEqual(store.runPolicy, .none)

        // Without a run policy, the rewritten file keeps the version earlier builds can edit.
        var folder = try XCTUnwrap(store.watchFolders.first)
        folder.isEnabled = false
        try store.saveWatchFolder(folder)
        XCTAssertEqual(try writtenVersion(of: fileURL), try writtenVersion(of: fixtureURL))

        let policy = QueueRunPolicy(
            limitsToOperatingWindows: true,
            operatingWindows: [
                window(days: QueueWeekday.weekdays, from: 1, to: 7),
                window(days: [.saturday, .sunday], from: 22, to: 8),
            ],
            waitsOnBatteryPower: true,
            waitsUnderThermalPressure: true
        )
        try store.saveRunPolicy(policy)
        try store.recordRunPolicyActivity(QueueRunPolicyActivity(occurredAt: Date(timeIntervalSince1970: 1_800_000_000), kind: .held, hold: .onBatteryPower, itemCount: 2))
        XCTAssertGreaterThan(try writtenVersion(of: fileURL), try writtenVersion(of: fixtureURL))

        let reloaded = QueueAutomationStore(fileURL: fileURL)
        XCTAssertNil(reloaded.loadErrorMessage)
        XCTAssertEqual(reloaded.runPolicy, policy)
        XCTAssertEqual(reloaded.runPolicyActivity, store.runPolicyActivity)
        XCTAssertEqual(reloaded.watchFolders, store.watchFolders)
        XCTAssertEqual(reloaded.seenSources, store.seenSources)
    }

    @MainActor
    func testActivityRecordKeepsOnlyTheMostRecentEntries() throws {
        let store = QueueAutomationStore.inMemory()
        let total = QueueAutomationStore.maximumRunPolicyActivity + 3
        for offset in 0..<total {
            try store.recordRunPolicyActivity(QueueRunPolicyActivity(
                occurredAt: Date(timeIntervalSince1970: TimeInterval(offset)),
                kind: .started,
                itemCount: 1
            ))
        }
        XCTAssertEqual(store.runPolicyActivity.count, QueueAutomationStore.maximumRunPolicyActivity)
        XCTAssertEqual(store.runPolicyActivity.last?.occurredAt, Date(timeIntervalSince1970: TimeInterval(total - 1)))
    }

    // MARK: - Screenshots

    @MainActor
    func testScheduleSettingsRendersRunTimesAndTheNextStart() async throws {
        let harness = try await PolicyHarness.make(now: Date(), calendar: .autoupdatingCurrent, states: [])
        let policy = QueueRunPolicy(
            limitsToOperatingWindows: true,
            operatingWindows: [
                window(days: QueueWeekday.weekdays, from: 1, to: 7),
                window(days: [.friday, .saturday], from: 23, to: 9),
            ],
            waitsOnBatteryPower: true
        )
        try harness.controller.updateRunPolicy(policy)
        XCTAssertNotNil(harness.controller.runPolicyEvaluation().nextWindowStart ?? harness.controller.runPolicyEvaluation().currentWindow?.end)

        harness.controller.recordRunPolicyActivity(.started, hold: nil, itemCount: 3)
        harness.controller.recordRunPolicyActivity(.stoppedStartingNewItems, hold: .onBatteryPower, itemCount: 2)
        let pane = QueueScheduleSettingsPane(controller: harness.controller)
            .padding(20)
            .preferredColorScheme(.light)
        try ReviewScreenshot.capture(pane, size: CGSize(width: 860, height: 700), named: "queue-schedule-settings")
        let hostingView = NSHostingView(rootView: pane.frame(width: 860, height: 700))
        hostingView.appearance = NSAppearance(named: .aqua)
        hostingView.frame = NSRect(x: 0, y: 0, width: 860, height: 700)
        hostingView.layoutSubtreeIfNeeded()
        let bitmap = try XCTUnwrap(hostingView.bitmapImageRepForCachingDisplay(in: hostingView.bounds))
        hostingView.cacheDisplay(in: hostingView.bounds, to: bitmap)
        XCTAssertEqual(bitmap.pixelsWide, 860)
    }

    // MARK: - Helpers

    private func date(_ year: Int, _ month: Int, _ day: Int, _ hour: Int, _ minute: Int) -> Date {
        calendar.date(from: DateComponents(year: year, month: month, day: day, hour: hour, minute: minute))!
    }

    private func window(days: Set<QueueWeekday>, from start: Int, to end: Int) -> QueueOperatingWindow {
        window(days: days, from: QueueClockTime(hour: start), to: QueueClockTime(hour: end))
    }

    private func window(days: Set<QueueWeekday>, from start: QueueClockTime, to end: QueueClockTime) -> QueueOperatingWindow {
        QueueOperatingWindow(days: days, start: start, end: end)
    }

    private func windowsPolicy(_ windows: [QueueOperatingWindow]) -> QueueRunPolicy {
        QueueRunPolicy(limitsToOperatingWindows: true, operatingWindows: windows)
    }

    private func evaluate(
        _ policy: QueueRunPolicy,
        at date: Date,
        conditions: QueueMachineConditions = QueueMachineConditions()
    ) -> QueueRunPolicyEvaluation {
        QueueRunPolicyEvaluation.evaluate(policy, at: date, conditions: conditions, calendar: calendar)
    }

    private func writtenVersion(of url: URL) throws -> Int {
        let object = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any]
        return try XCTUnwrap(object?["version"] as? Int)
    }

    private func makeDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("QueueRunPolicyTests.\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        temporaryURLs.append(url)
        return url
    }
}

/// Machine conditions a test changes from any thread, including from inside a conversion.
private final class FakeMachineConditions: QueueMachineConditionsProviding, @unchecked Sendable {
    private let lock = NSLock()
    nonisolated(unsafe) private var stored = QueueMachineConditions()

    nonisolated var current: QueueMachineConditions {
        get { lock.withLock { stored } }
        set { lock.withLock { stored = newValue } }
    }

    func startObserving(_ onChange: @escaping @MainActor () -> Void) {}

    func stopObserving() {}
}

private final class LockedPaths: @unchecked Sendable {
    private let lock = NSLock()
    private var paths: [String] = []

    var value: [String] { lock.withLock { paths } }

    func append(_ path: String) {
        lock.withLock { paths.append(path) }
    }
}

private final class LockedAction: @unchecked Sendable {
    private let lock = NSLock()
    private var action: (@Sendable () -> Void)?

    var value: (@Sendable () -> Void)? {
        get { lock.withLock { action } }
        set { lock.withLock { action = newValue } }
    }
}

/// A queue, its automation controller, and a fake converter, all on one injected clock.
@MainActor
private final class PolicyHarness {
    let clock: LockedTestDate
    let conditions = FakeMachineConditions()
    let timers = WatchFolderMonitoringTests.ManualTimers()
    let workspaceCenter = NotificationCenter()
    let queueStore = ConversionQueueStore.inMemory()
    let scheduleStore = OffPeakScheduleStore.inMemory()
    let viewModel: ConversionViewModel
    let controller: QueueAutomationController
    private let paths = LockedPaths()
    private let conversionAction = LockedAction()
    var convertedPaths: [String] { paths.value }
    /// Runs inside each conversion, before it finishes, on the conversion's thread.
    var onConversion: (@Sendable () -> Void)? {
        get { conversionAction.value }
        set { conversionAction.value = newValue }
    }

    private init(now: Date, calendar: Calendar, states: [DurableQueueItemState]) throws {
        clock = LockedTestDate(now)
        let clock = clock
        let paths = paths
        let conversionAction = conversionAction
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { job in
            paths.append(job.source.path)
            conversionAction.value?()
        })
        viewModel = ConversionViewModel(
            clientFactory: { worker },
            diagnosticClock: { clock.value },
            durableQueueStore: queueStore,
            offPeakScheduleStore: scheduleStore,
            sourceAvailabilityResolver: { _ in true }
        )
        controller = QueueAutomationController(
            store: QueueAutomationStore.inMemory(),
            admitter: viewModel,
            draftFactory: { _ in
                QueueDraftFactory(profile: BuiltInProfile.balanced.profile, destinationURL: URL(fileURLWithPath: "/tmp/Output"), jobOptions: JobOptions())
            },
            now: { clock.value },
            timerScheduler: timers,
            workspaceNotificationCenter: workspaceCenter,
            machineConditions: conditions,
            calendar: calendar
        )
        let items = states.enumerated().map { ordinal, state in
            let draft = ConversionDraft(
                source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: Self.path(ordinal))),
                sourceDetails: nil,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/tmp/Output"),
                options: ConversionOptions()
            )
            let arrivedAt = Date(timeIntervalSince1970: 1_800_000_000)
            return DurableConversionQueueItem(
                ordinal: ordinal,
                origin: .singleSource,
                intent: DurableQueueItemIntent(draft: draft),
                state: state,
                admission: state == .heldForReview
                    ? .watchFolder(id: UUID(), name: "Incoming", arrivedAt: arrivedAt, settledAt: arrivedAt, admittedAt: arrivedAt)
                    : nil
            )
        }
        initialItems = items
    }

    private var initialItems: [DurableConversionQueueItem] = []

    static func make(now: Date, calendar: Calendar, states: [DurableQueueItemState]) async throws -> PolicyHarness {
        let harness = try PolicyHarness(now: now, calendar: calendar, states: states)
        if !harness.initialItems.isEmpty {
            try await harness.queueStore.replaceItems(harness.initialItems)
        }
        return harness
    }

    static func path(_ ordinal: Int) -> String {
        "/tmp/run-policy-\(ordinal).mkv"
    }

    func path(_ ordinal: Int) -> String {
        Self.path(ordinal)
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
        // A silent timeout would let later assertions observe a half-finished run.
        if !condition() {
            XCTFail("Timed out waiting for the queue to settle", file: file, line: line)
        }
    }
}

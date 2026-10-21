import Foundation
import XCTest
@testable import BluRayToVisionPro

/// The PR 1 live contracts, exercised through the shared fakes that later host and player tests
/// build on: the starter's cancellation contract, the slot's drain, and cancel admission.
final class LiveRelayAdmissionContractTests: XCTestCase {
    private let sessionA = try! RelaySessionIdentifier(rawValue: "11111111-1111-4111-8111-111111111111")
    private let sessionB = try! RelaySessionIdentifier(rawValue: "22222222-2222-4222-8222-222222222222")

    func testCancelledStartStopsWhatItLaunchedBeforeThrowing() async throws {
        let starter = FakeLiveRelayStreamStarter()
        let start = Task { try await starter.start(makeFakeLiveStreamRequest(), id: UUID()) }
        await starter.waitForCalls(1)
        start.cancel()

        do {
            _ = try await start.value
            XCTFail("Expected cancellation")
        } catch {
            XCTAssertTrue(error is CancellationError)
        }
        let launched = await starter.launched
        XCTAssertEqual(launched.count, 1)
        let stopCalls = await launched[0].stopCalls
        let stopFinished = await launched[0].isStopFinished
        XCTAssertEqual(stopCalls, [.cancelled])
        XCTAssertTrue(stopFinished)
    }

    func testStartCancelledBeforeItRunsLeavesNothingRunning() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let start = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try await starter.start(makeFakeLiveStreamRequest(), id: UUID())
        }

        await XCTAssertThrowsAsync(try await start.value)
        let launched = await starter.launched
        XCTAssertEqual(launched.count, 1)
        let stopFinished = await launched[0].isStopFinished
        XCTAssertTrue(stopFinished)
    }

    func testEndedStreamAnswersWithStreamEndedBody() async throws {
        let stream = FakeLiveRelayStream(states: [.live(earliestMs: 0, latestMs: 4_000), .failed(code: "worker_failed", message: "boom")])
        let request = RelayHTTPRequest(method: "GET", requestTarget: RelayWireContract.playlistPath, headers: [:], body: Data())
        await stream.advance()

        let failed = await stream.relayResponse(for: request)
        let failedBody = try JSONDecoder().decode(RelayLiveStreamEnded.self, from: failed.body)
        XCTAssertEqual(failed.statusCode, RelayLiveStreamEnded.Kind.streamFailed.statusCode)
        XCTAssertEqual(failedBody, RelayLiveStreamEnded(error: .streamFailed, code: "worker_failed", message: "boom"))

        await stream.stop(.idle)
        let ended = await stream.relayResponse(for: request)
        let endedBody = try JSONDecoder().decode(RelayLiveStreamEnded.self, from: ended.body)
        XCTAssertEqual(ended.statusCode, RelayLiveStreamEnded.Kind.streamEnded.statusCode)
        XCTAssertEqual(endedBody, RelayLiveStreamEnded(error: .streamEnded, code: LiveRelayStopReason.idle.rawValue, message: nil))
    }

    func testStreamEndedBodyRejectsAnUnknownKind() {
        XCTAssertThrowsError(
            try JSONDecoder().decode(RelayLiveStreamEnded.self, from: Data(#"{"error":"session_ended"}"#.utf8))
        )
    }

    func testReplacingASuspendedStartWaitsForItsCleanupBeforeTheNextStart() async throws {
        let starter = FakeLiveRelayStreamStarter()
        let admission = FakeLiveStartAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let first = key(sessionA)
        guard case .started = await admission.start(first, request: makeFakeLiveStreamRequest()) else {
            return XCTFail("Expected the first start")
        }
        await starter.waitForCalls(1)
        let firstStream = await starter.launched[0]
        await firstStream.holdStop()

        let replacement = Task { await admission.start(self.key(self.sessionA), request: makeFakeLiveStreamRequest()) }
        try await waitUntil { await !firstStream.stopCalls.isEmpty }
        let callsWhileDraining = await starter.calls.count
        XCTAssertEqual(callsWhileDraining, 1)
        await firstStream.releaseStop()

        guard case .started = await replacement.value else { return XCTFail("Expected the replacement start") }
        await starter.waitForCalls(2)
        let firstStopCalls = await firstStream.stopCalls
        XCTAssertEqual(firstStopCalls, [.cancelled])
    }

    func testOtherSessionWaitsForADrainThenGetsTheSlot() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = FakeLiveStartAdmission(starter: starter, liveParentSessionIDs: [sessionA, sessionB])
        _ = await admission.start(key(sessionA), request: makeFakeLiveStreamRequest())
        await starter.waitForCalls(1)
        try await waitUntil {
            if case .running = await admission.slot { return true }
            return false
        }
        let running = await starter.launched[0]
        await running.holdStop()
        let busy = await admission.start(key(sessionB), request: makeFakeLiveStreamRequest())
        XCTAssertEqual(busy, .busy)

        let release = Task { await admission.release(reason: .sessionEnded) }
        try await waitUntil { await !running.stopCalls.isEmpty }
        let waiting = Task { await admission.start(self.key(self.sessionB), request: makeFakeLiveStreamRequest()) }
        await running.releaseStop()
        await release.value

        guard case .started = await waiting.value else { return XCTFail("Expected the start after the drain") }
        let stopCalls = await running.stopCalls
        XCTAssertEqual(stopCalls, [.sessionEnded])
    }

    func testStartIsIdempotentPerKeyAndConflictsOnChangedParameters() async throws {
        let admission = FakeLiveStartAdmission(starter: FakeLiveRelayStreamStarter(), liveParentSessionIDs: [sessionA])
        let original = key(sessionA)
        let changed = LiveStartKey(
            parentSessionID: original.parentSessionID,
            startID: original.startID,
            titleID: original.titleID,
            revision: original.revision,
            audioPID: original.audioPID + 1
        )

        let first = await admission.start(original, request: makeFakeLiveStreamRequest())
        let repeated = await admission.start(original, request: makeFakeLiveStreamRequest())
        let conflict = await admission.start(changed, request: makeFakeLiveStreamRequest())

        XCTAssertEqual(first, repeated)
        XCTAssertEqual(conflict, .conflict)
    }

    func testRetryAfterCancellingAnAdmittedStartIsRefused() async throws {
        let starter = FakeLiveRelayStreamStarter()
        let admission = FakeLiveStartAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)
        _ = await admission.start(start, request: makeFakeLiveStreamRequest())
        await starter.waitForCalls(1)

        let cancel = await admission.cancel(parentSessionID: sessionA, startID: start.startID)
        let retry = await admission.start(start, request: makeFakeLiveStreamRequest())

        XCTAssertEqual(cancel, .acknowledged)
        XCTAssertEqual(retry, .cancelled)
        let calls = await starter.calls.count
        XCTAssertEqual(calls, 1)
    }

    func testRetryOfAReplacedStartReturnsItsStreamWithoutDisturbingTheCurrentOne() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = FakeLiveStartAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let first = key(sessionA)
        let original = await admission.start(first, request: makeFakeLiveStreamRequest())
        _ = await admission.start(key(sessionA), request: makeFakeLiveStreamRequest())
        await starter.waitForCalls(2)
        try await waitUntil {
            if case .running = await admission.slot { return true }
            return false
        }

        let retry = await admission.start(first, request: makeFakeLiveStreamRequest())

        XCTAssertEqual(retry, original)
        let calls = await starter.calls.count
        XCTAssertEqual(calls, 2)
        let current = await starter.launched[1]
        let currentStopCalls = await current.stopCalls
        XCTAssertEqual(currentStopCalls, [])
    }

    func testRetryWithChangedParametersAfterReleaseIsAConflict() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = FakeLiveStartAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let original = key(sessionA)
        _ = await admission.start(original, request: makeFakeLiveStreamRequest())
        await admission.release(reason: .sessionEnded)
        let changed = LiveStartKey(
            parentSessionID: original.parentSessionID,
            startID: original.startID,
            titleID: "other title",
            revision: original.revision,
            audioPID: original.audioPID
        )

        let retry = await admission.start(changed, request: makeFakeLiveStreamRequest())

        XCTAssertEqual(retry, .conflict)
        let calls = await starter.calls.count
        XCTAssertEqual(calls, 1)
    }

    func testRetryDuringACancelDrainIsRefusedWithoutWaiting() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = FakeLiveStartAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)
        _ = await admission.start(start, request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        let stream = await starter.launched[0]
        await stream.holdStop()
        let cancel = Task { await admission.cancel(parentSessionID: self.sessionA, startID: start.startID) }
        try await waitUntil { await !stream.stopCalls.isEmpty }

        let retry = try await outcome(of: admission, start)

        XCTAssertEqual(retry, .cancelled)
        await stream.releaseStop()
        let acknowledged = await cancel.value
        XCTAssertEqual(acknowledged, .acknowledged)
    }

    func testRecordOutlivesLongSlotOwnershipByTheClientDeadline() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = FakeLiveStartAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)
        let original = await admission.start(start, request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        await admission.advanceClock(by: 100)
        await admission.release(reason: .sharingStopped)
        let changed = withChangedParameters(start)

        await admission.advanceClock(by: LiveStartLimits.clientCancelDeadlineAfterStart - 1)
        let retry = await admission.start(start, request: makeFakeLiveStreamRequest())
        let conflict = await admission.start(changed, request: makeFakeLiveStreamRequest())
        XCTAssertEqual(retry, original)
        XCTAssertEqual(conflict, .conflict)

        await admission.advanceClock(by: 2)
        let records = await admission.startRecordCount
        XCTAssertEqual(records, 0)
    }

    func testRecordIsKeptWhileItsDrainLasts() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = FakeLiveStartAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)
        _ = await admission.start(start, request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        let stream = await starter.launched[0]
        await stream.holdStop()
        let cancel = Task { await admission.cancel(parentSessionID: self.sessionA, startID: start.startID) }
        try await waitUntil { await !stream.stopCalls.isEmpty }

        await admission.advanceClock(by: 200)
        let duringDrain = try await outcome(of: admission, start)
        XCTAssertEqual(duringDrain, .cancelled)

        await stream.releaseStop()
        _ = await cancel.value
        await admission.advanceClock(by: LiveStartLimits.clientCancelDeadlineAfterStart - 1)
        let afterDrain = await admission.start(start, request: makeFakeLiveStreamRequest())
        XCTAssertEqual(afterDrain, .cancelled)
        await admission.advanceClock(by: 2)
        let records = await admission.startRecordCount
        XCTAssertEqual(records, 0)
    }

    func testRepeatedCancelsDoNotExtendARecord() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = FakeLiveStartAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)
        _ = await admission.start(start, request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        await admission.release(reason: .sharingStopped)

        await admission.advanceClock(by: 30)
        _ = await admission.cancel(parentSessionID: sessionA, startID: start.startID)
        await admission.advanceClock(by: 30)
        _ = await admission.cancel(parentSessionID: sessionA, startID: start.startID)
        await admission.advanceClock(by: LiveStartLimits.clientCancelDeadlineAfterStart - 30 + 1)

        let records = await admission.startRecordCount
        XCTAssertEqual(records, 0)
    }

    func testEndedSessionsAndIdleSessionsLeaveNoState() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = FakeLiveStartAdmission(starter: starter, liveParentSessionIDs: [sessionA, sessionB])
        _ = await admission.start(key(sessionA), request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        _ = await admission.cancel(parentSessionID: sessionA, startID: UUID())

        await admission.endSession(sessionA, reason: .sessionEnded)

        let stream = await starter.launched[0]
        let stopCalls = await stream.stopCalls
        XCTAssertEqual(stopCalls, [.sessionEnded])
        let afterEnd = await admission.sessionsWithState
        XCTAssertEqual(afterEnd, [])

        _ = await admission.start(key(sessionB), request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        await admission.release(reason: .sharingStopped)
        await admission.advanceClock(by: LiveStartLimits.tombstoneLifetime + 1)
        let afterIdle = await admission.sessionsWithState
        XCTAssertEqual(afterIdle, [])
    }

    func testRetryAfterStarterFailureReturnsTheOriginalOutcome() async throws {
        struct StartFailed: Error {}
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        await starter.failNextStart(with: StartFailed())
        let admission = FakeLiveStartAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)
        let original = await admission.start(start, request: makeFakeLiveStreamRequest())
        try await waitUntil {
            if case .empty = await admission.slot { return true }
            return false
        }

        let retry = await admission.start(start, request: makeFakeLiveStreamRequest())
        let conflict = await admission.start(withChangedParameters(start), request: makeFakeLiveStreamRequest())

        XCTAssertEqual(retry, original)
        XCTAssertEqual(conflict, .conflict)
        let calls = await starter.calls.count
        XCTAssertEqual(calls, 1)
    }

    func testCancelBeforeStartLeavesATombstoneThatExpires() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = FakeLiveStartAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)

        let cancel = await admission.cancel(parentSessionID: sessionA, startID: start.startID)
        let refused = await admission.start(start, request: makeFakeLiveStreamRequest())
        XCTAssertEqual(cancel, .acknowledged)
        XCTAssertEqual(refused, .cancelled)
        let callsWhileTombstoned = await starter.calls.count
        XCTAssertEqual(callsWhileTombstoned, 0)

        await admission.advanceClock(by: LiveStartLimits.tombstoneLifetime - 1)
        let stillRefused = await admission.start(start, request: makeFakeLiveStreamRequest())
        XCTAssertEqual(stillRefused, .cancelled)
        await admission.advanceClock(by: 1)
        guard case .started = await admission.start(start, request: makeFakeLiveStreamRequest()) else {
            return XCTFail("Expected the start after the tombstone expired")
        }
    }

    func testNewTombstoneCancelsBeyondTheWindowRetryLaterAndRecordNothing() async throws {
        let admission = FakeLiveStartAdmission(starter: FakeLiveRelayStreamStarter(), liveParentSessionIDs: [sessionA])
        var tombstoned: [UUID] = []
        for _ in 0 ..< LiveStartLimits.tombstoneCancelsPerSessionPerMinute {
            let startID = UUID()
            let outcome = await admission.cancel(parentSessionID: sessionA, startID: startID)
            XCTAssertEqual(outcome, .acknowledged)
            tombstoned.append(startID)
        }
        let running = key(sessionA)
        _ = await admission.start(running, request: makeFakeLiveStreamRequest())

        let refusedStartID = UUID()
        let refused = await admission.cancel(parentSessionID: sessionA, startID: refusedStartID)
        XCTAssertEqual(refused, .retryLater)
        XCTAssertEqual(refused.statusCode, 429)
        let repeatedTombstone = await admission.cancel(parentSessionID: sessionA, startID: tombstoned[0])
        let matchedStart = await admission.cancel(parentSessionID: sessionA, startID: running.startID)
        XCTAssertEqual(repeatedTombstone, .acknowledged)
        XCTAssertEqual(matchedStart, .acknowledged)

        // Nothing was recorded for the refused cancel, and no tombstone was evicted to make room.
        let count = await admission.tombstoneCount
        XCTAssertEqual(count, LiveStartLimits.tombstoneCancelsPerSessionPerMinute)
        let refusedKey = LiveStartKey(parentSessionID: sessionA, startID: refusedStartID, titleID: "t", revision: "r", audioPID: 4352)
        guard case .started = await admission.start(refusedKey, request: makeFakeLiveStreamRequest()) else {
            return XCTFail("A refused cancel must not block its start")
        }
    }

    func testTombstoneTableCapacityRefusesInsteadOfEvicting() async throws {
        let sessions = try (0 ..< 6).map {
            try RelaySessionIdentifier(rawValue: String(format: "00000000-0000-4000-8000-%012d", $0 + 10))
        }
        let admission = FakeLiveStartAdmission(starter: FakeLiveRelayStreamStarter(), liveParentSessionIDs: Set(sessions))
        var outcomes: [LiveCancelOutcome] = []
        for session in sessions {
            for _ in 0 ..< LiveStartLimits.tombstoneCancelsPerSessionPerMinute {
                outcomes.append(await admission.cancel(parentSessionID: session, startID: UUID()))
            }
        }

        XCTAssertEqual(outcomes.filter { $0 == .acknowledged }.count, LiveStartLimits.tombstoneCapacity)
        let count = await admission.tombstoneCount
        XCTAssertEqual(count, LiveStartLimits.tombstoneCapacity)
    }

    private func withChangedParameters(_ key: LiveStartKey) -> LiveStartKey {
        LiveStartKey(
            parentSessionID: key.parentSessionID,
            startID: key.startID,
            titleID: key.titleID,
            revision: key.revision,
            audioPID: key.audioPID + 1
        )
    }

    private func waitForRunning(_ admission: FakeLiveStartAdmission) async throws {
        try await waitUntil {
            if case .running = await admission.slot { return true }
            return false
        }
    }

    /// The start's outcome, or a thrown timeout if `start` is still waiting, for example on a
    /// drain, after five seconds.
    private func outcome(of admission: FakeLiveStartAdmission, _ key: LiveStartKey) async throws -> LiveStartOutcome {
        let box = OutcomeBox()
        Task { await box.set(await admission.start(key, request: makeFakeLiveStreamRequest())) }
        try await waitUntil { await box.value != nil }
        return await box.value!
    }

    private func key(_ session: RelaySessionIdentifier) -> LiveStartKey {
        LiveStartKey(parentSessionID: session, startID: UUID(), titleID: "title", revision: "revision", audioPID: 4352)
    }
}

private struct WaitTimedOut: Error {}

private actor OutcomeBox {
    private(set) var value: LiveStartOutcome?

    func set(_ outcome: LiveStartOutcome) {
        value = outcome
    }
}

/// Polls `condition` until it holds, failing instead of hanging when a contract is broken.
private func waitUntil(timeout: TimeInterval = 5, _ condition: () async -> Bool) async throws {
    let deadline = Date().addingTimeInterval(timeout)
    while await !condition() {
        guard Date() < deadline else { throw WaitTimedOut() }
        try await Task.sleep(nanoseconds: 1_000_000)
    }
}

private func XCTAssertThrowsAsync<T>(
    _ expression: @autoclosure () async throws -> T,
    file: StaticString = #filePath,
    line: UInt = #line
) async {
    do {
        _ = try await expression()
        XCTFail("Expected an error", file: file, line: line)
    } catch {}
}

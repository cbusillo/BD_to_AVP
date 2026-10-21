import Foundation
import XCTest
@testable import BluRayToVisionPro

/// The PR 1 live contracts, exercised through the shared fakes that later host and player tests
/// build on: the starter's cancellation contract, the slot's drain, and cancel admission.
class LiveRelayAdmissionContractTests: XCTestCase {
    /// The admission under test. The reference model here; the production admission in
    /// `LiveStartAdmissionProductionTests`, which inherits every test.
    func makeAdmission(
        starter: any LiveRelayStreamStarting,
        liveParentSessionIDs: Set<RelaySessionIdentifier>
    ) -> any LiveStartAdmissionUnderTest {
        FakeLiveStartAdmission(starter: starter, liveParentSessionIDs: liveParentSessionIDs)
    }

    private let sessionA = try! RelaySessionIdentifier(rawValue: "11111111-1111-4111-8111-111111111111")
    private let sessionB = try! RelaySessionIdentifier(rawValue: "22222222-2222-4222-8222-222222222222")
    /// Every test key is issued at the fake admission's starting time.
    private let issuedAt = Date(timeIntervalSince1970: 1_700_000_000)
    private var issuedAtMs: Int64 { Int64(issuedAt.timeIntervalSince1970 * 1_000) }
    /// Seconds from issue until no request for the start can verify under the default policy.
    private var lastVerifiableOffset: TimeInterval {
        let policy = try! RelayRequestValidationPolicy()
        let last = LiveStartLimits.lastVerifiableUnixMilliseconds(issuedAtUnixMilliseconds: issuedAtMs, policy: policy)!
        return TimeInterval(last - issuedAtMs) / 1_000
    }

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
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA])
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
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA, sessionB])
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
        let admission = makeAdmission(starter: FakeLiveRelayStreamStarter(), liveParentSessionIDs: [sessionA])
        let original = key(sessionA)
        let changed = LiveStartKey(
            parentSessionID: original.parentSessionID,
            startID: original.startID,
            issuedAtUnixMilliseconds: original.issuedAtUnixMilliseconds,
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
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)
        _ = await admission.start(start, request: makeFakeLiveStreamRequest())
        await starter.waitForCalls(1)

        let cancel = await admission.cancel(parentSessionID: sessionA, startID: start.startID, issuedAtUnixMilliseconds: issuedAtMs)
        let retry = await admission.start(start, request: makeFakeLiveStreamRequest())

        XCTAssertEqual(cancel, .acknowledged)
        XCTAssertEqual(retry, .cancelled)
        let calls = await starter.calls.count
        XCTAssertEqual(calls, 1)
    }

    func testRetryOfAReplacedStartReturnsItsStreamWithoutDisturbingTheCurrentOne() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA])
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
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let original = key(sessionA)
        _ = await admission.start(original, request: makeFakeLiveStreamRequest())
        await admission.release(reason: .sessionEnded)
        let changed = LiveStartKey(
            parentSessionID: original.parentSessionID,
            startID: original.startID,
            issuedAtUnixMilliseconds: original.issuedAtUnixMilliseconds,
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
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)
        _ = await admission.start(start, request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        let stream = await starter.launched[0]
        await stream.holdStop()
        let cancel = Task { await admission.cancel(parentSessionID: self.sessionA, startID: start.startID, issuedAtUnixMilliseconds: self.issuedAtMs) }
        try await waitUntil { await !stream.stopCalls.isEmpty }

        let retry = try await outcome(of: admission, start)

        XCTAssertEqual(retry, .cancelled)
        await stream.releaseStop()
        let acknowledged = await cancel.value
        XCTAssertEqual(acknowledged, .acknowledged)
    }

    func testRetrySignedInsideTheWindowIsRefusedAfterTheSlotIsGone() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)
        _ = await admission.start(start, request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        _ = await admission.cancel(parentSessionID: sessionA, startID: start.startID, issuedAtUnixMilliseconds: issuedAtMs)

        await admission.advanceClock(by: 41)
        let retry = await admission.start(start, signedAt: issuedAt.addingTimeInterval(39), request: makeFakeLiveStreamRequest())

        XCTAssertEqual(retry, .cancelled)
        let calls = await starter.calls.count
        XCTAssertEqual(calls, 1)
        await admission.advanceClock(by: lastVerifiableOffset - 41 + 1)
        let records = await admission.startRecordCount
        XCTAssertEqual(records, 0)
    }

    func testRequestsSignedOutsideTheWindowAreExpiredAndRecordNothing() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)
        let early = await admission.start(start, signedAt: issuedAt.addingTimeInterval(-6), request: makeFakeLiveStreamRequest())
        XCTAssertEqual(early, .expired)

        await admission.advanceClock(by: LiveStartLimits.clientCancelDeadlineAfterStart + 1)
        let late = await admission.start(start, request: makeFakeLiveStreamRequest())
        let lateCancel = await admission.cancel(
            parentSessionID: sessionA,
            startID: start.startID,
            issuedAtUnixMilliseconds: issuedAtMs
        )

        XCTAssertEqual(late, .expired)
        XCTAssertEqual(lateCancel, .expired)
        XCTAssertNotEqual(lateCancel?.statusCode, LiveCancelOutcome.retryLater.statusCode)
        let calls = await starter.calls.count
        let tombstones = await admission.tombstoneCount
        XCTAssertEqual(calls, 0)
        XCTAssertEqual(tombstones, 0)
    }

    func testAfterLongSlotOwnershipEveryRetryIsRefused() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)
        _ = await admission.start(start, request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        await admission.advanceClock(by: 100)
        await admission.release(reason: .sharingStopped)

        let retry = await admission.start(start, request: makeFakeLiveStreamRequest())
        let changed = await admission.start(withChangedParameters(start), request: makeFakeLiveStreamRequest())

        XCTAssertEqual(retry, .expired)
        XCTAssertEqual(changed, .expired)
        let calls = await starter.calls.count
        XCTAssertEqual(calls, 1)
    }

    func testRecordIsKeptWhileItsDrainLasts() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)
        _ = await admission.start(start, request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        let stream = await starter.launched[0]
        await stream.holdStop()
        let cancel = Task {
            await admission.cancel(parentSessionID: self.sessionA, startID: start.startID, issuedAtUnixMilliseconds: self.issuedAtMs)
        }
        try await waitUntil { await !stream.stopCalls.isEmpty }

        await admission.advanceClock(by: lastVerifiableOffset + 10)
        let duringDrain = await admission.startRecordCount
        XCTAssertEqual(duringDrain, 1)

        await stream.releaseStop()
        _ = await cancel.value
        await admission.advanceClock(by: 1)
        let afterDrain = await admission.startRecordCount
        XCTAssertEqual(afterDrain, 0)
    }

    func testEndedSessionsAndIdleSessionsLeaveNoState() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA, sessionB])
        _ = await admission.start(key(sessionA), request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        _ = await admission.cancel(parentSessionID: sessionA, startID: UUID(), issuedAtUnixMilliseconds: issuedAtMs)

        await admission.endSession(sessionA, reason: .sessionEnded)

        let stream = await starter.launched[0]
        let stopCalls = await stream.stopCalls
        XCTAssertEqual(stopCalls, [.sessionEnded])
        let afterEnd = await admission.sessionsWithState
        XCTAssertEqual(afterEnd, [])

        _ = await admission.start(key(sessionB), request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        await admission.release(reason: .sharingStopped)
        await admission.advanceClock(by: lastVerifiableOffset + 1)
        let afterIdle = await admission.sessionsWithState
        XCTAssertEqual(afterIdle, [])
    }

    func testStartAndCancelRacingSessionEndAdmitNothingAndLeaveNoState() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        _ = await admission.start(key(sessionA), request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        let running = await starter.launched[0]
        await running.holdStop()

        let replacement = Task { await admission.start(self.key(self.sessionA), request: makeFakeLiveStreamRequest()) }
        try await waitUntil { await !running.stopCalls.isEmpty }
        let end = Task { await admission.endSession(self.sessionA, reason: .sessionEnded) }
        try await waitUntil { await admission.isEnded(self.sessionA) }
        let cancel = await admission.cancel(parentSessionID: sessionA, startID: UUID(), issuedAtUnixMilliseconds: issuedAtMs)
        await running.releaseStop()
        await end.value
        let replacementOutcome = await replacement.value

        XCTAssertNil(cancel)
        XCTAssertNil(replacementOutcome)
        let calls = await starter.calls.count
        XCTAssertEqual(calls, 1)
        let state = await admission.sessionsWithState
        XCTAssertEqual(state, [])
    }

    func testRetryAfterStarterFailureReturnsTheOriginalOutcome() async throws {
        struct StartFailed: Error {}
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        await starter.failNextStart(with: StartFailed())
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA])
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

    func testStartWaitingOnADrainPastItsWindowIsRefused() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        _ = await admission.start(key(sessionA), request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        let running = await starter.launched[0]
        await running.holdStop()
        let waiting = key(sessionA)
        let replacement = Task { await admission.start(waiting, request: makeFakeLiveStreamRequest()) }
        try await waitUntil { await !running.stopCalls.isEmpty }
        let cancel = await admission.cancel(parentSessionID: sessionA, startID: waiting.startID, issuedAtUnixMilliseconds: issuedAtMs)

        await admission.advanceClock(by: lastVerifiableOffset + 1)
        await running.releaseStop()
        let outcome = await replacement.value

        XCTAssertEqual(cancel, .acknowledged)
        XCTAssertEqual(outcome, .expired)
        let calls = await starter.calls.count
        XCTAssertEqual(calls, 1)
    }

    func testStartWaitingOnAnotherSessionsDrainPastItsWindowIsRefused() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA, sessionB])
        _ = await admission.start(key(sessionA), request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        let running = await starter.launched[0]
        await running.holdStop()
        let release = Task { await admission.release(reason: .sharingStopped) }
        try await waitUntil { await !running.stopCalls.isEmpty }
        let waiting = key(sessionB)
        let other = Task { await admission.start(waiting, request: makeFakeLiveStreamRequest()) }
        try await waitUntil { await admission.isWaitingOnDrain }
        _ = await admission.cancel(parentSessionID: sessionB, startID: waiting.startID, issuedAtUnixMilliseconds: issuedAtMs)

        await admission.advanceClock(by: lastVerifiableOffset + 1)
        await running.releaseStop()
        await release.value
        let outcome = await other.value

        XCTAssertEqual(outcome, .expired)
        let calls = await starter.calls.count
        XCTAssertEqual(calls, 1)
    }

    func testTombstoneExpiryUsesTheVerificationMillisecondClock() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)
        _ = await admission.cancel(parentSessionID: sessionA, startID: start.startID, issuedAtUnixMilliseconds: issuedAtMs)

        // Half a millisecond past the last verifiable time still truncates to it.
        await admission.advanceClock(by: lastVerifiableOffset + 0.0005)
        let lastFreshStart = await admission.start(
            start,
            signedAt: issuedAt.addingTimeInterval(LiveStartLimits.clientCancelDeadlineAfterStart),
            request: makeFakeLiveStreamRequest()
        )

        XCTAssertEqual(lastFreshStart, .cancelled)
        let calls = await starter.calls.count
        XCTAssertEqual(calls, 0)
    }

    func testRecordExpiryUsesTheVerificationMillisecondClock() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)
        let original = await admission.start(start, request: makeFakeLiveStreamRequest())
        try await waitForRunning(admission)
        await admission.release(reason: .sharingStopped)

        await admission.advanceClock(by: lastVerifiableOffset + 0.0005)
        let lastFreshRetry = await admission.start(
            start,
            signedAt: issuedAt.addingTimeInterval(LiveStartLimits.clientCancelDeadlineAfterStart),
            request: makeFakeLiveStreamRequest()
        )

        XCTAssertEqual(lastFreshRetry, original)
        let calls = await starter.calls.count
        XCTAssertEqual(calls, 1)
    }

    func testCancelBeforeStartLeavesATombstoneUntilNoRequestCanVerify() async throws {
        let starter = FakeLiveRelayStreamStarter(holdsStarts: false)
        let admission = makeAdmission(starter: starter, liveParentSessionIDs: [sessionA])
        let start = key(sessionA)

        let cancel = await admission.cancel(parentSessionID: sessionA, startID: start.startID, issuedAtUnixMilliseconds: issuedAtMs)
        let refused = await admission.start(start, request: makeFakeLiveStreamRequest())
        XCTAssertEqual(cancel, .acknowledged)
        XCTAssertEqual(refused, .cancelled)

        await admission.advanceClock(by: lastVerifiableOffset)
        let lastFreshStart = await admission.start(
            start,
            signedAt: issuedAt.addingTimeInterval(LiveStartLimits.clientCancelDeadlineAfterStart),
            request: makeFakeLiveStreamRequest()
        )
        XCTAssertEqual(lastFreshStart, .cancelled)
        await admission.advanceClock(by: 1)
        let tombstones = await admission.tombstoneCount
        XCTAssertEqual(tombstones, 0)
        let calls = await starter.calls.count
        XCTAssertEqual(calls, 0)
    }

    func testNewTombstoneCancelsBeyondTheWindowRetryLaterAndRecordNothing() async throws {
        let admission = makeAdmission(starter: FakeLiveRelayStreamStarter(), liveParentSessionIDs: [sessionA])
        var tombstoned: [UUID] = []
        for _ in 0 ..< LiveStartLimits.tombstonesPerSession {
            let startID = UUID()
            let outcome = await admission.cancel(parentSessionID: sessionA, startID: startID, issuedAtUnixMilliseconds: issuedAtMs)
            XCTAssertEqual(outcome, .acknowledged)
            tombstoned.append(startID)
        }
        let running = key(sessionA)
        _ = await admission.start(running, request: makeFakeLiveStreamRequest())

        let refusedStartID = UUID()
        let refused = await admission.cancel(parentSessionID: sessionA, startID: refusedStartID, issuedAtUnixMilliseconds: issuedAtMs)
        XCTAssertEqual(refused, .retryLater)
        XCTAssertEqual(refused?.statusCode, 429)
        let repeatedTombstone = await admission.cancel(parentSessionID: sessionA, startID: tombstoned[0], issuedAtUnixMilliseconds: issuedAtMs)
        let matchedStart = await admission.cancel(parentSessionID: sessionA, startID: running.startID, issuedAtUnixMilliseconds: issuedAtMs)
        XCTAssertEqual(repeatedTombstone, .acknowledged)
        XCTAssertEqual(matchedStart, .acknowledged)

        // Nothing was recorded for the refused cancel, and no tombstone was evicted to make room.
        let count = await admission.tombstoneCount
        XCTAssertEqual(count, LiveStartLimits.tombstonesPerSession)
        let refusedKey = LiveStartKey(parentSessionID: sessionA, startID: refusedStartID, issuedAtUnixMilliseconds: issuedAtMs, titleID: "t", revision: "r", audioPID: 4352)
        guard case .started = await admission.start(refusedKey, request: makeFakeLiveStreamRequest()) else {
            return XCTFail("A refused cancel must not block its start")
        }
    }

    func testTombstoneTableCapacityRefusesInsteadOfEvicting() async throws {
        let sessions = try (0 ..< 6).map {
            try RelaySessionIdentifier(rawValue: String(format: "00000000-0000-4000-8000-%012d", $0 + 10))
        }
        let admission = makeAdmission(starter: FakeLiveRelayStreamStarter(), liveParentSessionIDs: Set(sessions))
        var outcomes: [LiveCancelOutcome?] = []
        for session in sessions {
            for _ in 0 ..< LiveStartLimits.tombstonesPerSession {
                outcomes.append(await admission.cancel(parentSessionID: session, startID: UUID(), issuedAtUnixMilliseconds: issuedAtMs))
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
            issuedAtUnixMilliseconds: key.issuedAtUnixMilliseconds,
            titleID: key.titleID,
            revision: key.revision,
            audioPID: key.audioPID + 1
        )
    }

    private func waitForRunning(_ admission: any LiveStartAdmissionUnderTest) async throws {
        try await waitUntil {
            if case .running = await admission.slot { return true }
            return false
        }
    }

    /// The start's outcome, or a thrown timeout if `start` is still waiting, for example on a
    /// drain, after five seconds.
    private func outcome(of admission: any LiveStartAdmissionUnderTest, _ key: LiveStartKey) async throws -> LiveStartOutcome? {
        let box = OutcomeBox()
        Task { await box.set(await admission.start(key, request: makeFakeLiveStreamRequest())) }
        try await waitUntil { await box.isSet }
        return await box.value
    }

    private func key(_ session: RelaySessionIdentifier) -> LiveStartKey {
        LiveStartKey(
            parentSessionID: session,
            startID: UUID(),
            issuedAtUnixMilliseconds: issuedAtMs,
            titleID: "title",
            revision: "revision",
            audioPID: 4352
        )
    }
}

private struct WaitTimedOut: Error {}

private actor OutcomeBox {
    private(set) var value: LiveStartOutcome?
    private(set) var isSet = false

    func set(_ outcome: LiveStartOutcome?) {
        value = outcome
        isSet = true
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

/// Every PR 1 admission contract test, run against the production `LiveStartAdmission`.
final class LiveStartAdmissionProductionTests: LiveRelayAdmissionContractTests {
    override func makeAdmission(
        starter: any LiveRelayStreamStarting,
        liveParentSessionIDs: Set<RelaySessionIdentifier>
    ) -> any LiveStartAdmissionUnderTest {
        ProductionLiveStartAdmission(starter: starter, liveParentSessionIDs: liveParentSessionIDs)
    }
}

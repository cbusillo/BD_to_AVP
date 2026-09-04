import XCTest
@testable import BluRayToVisionPro

final class RelaySessionCoreTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_700_000_000)

    func testCommittedNonceProducesSameSixDigitSASAndRequiresBothConfirmations() async throws {
        let server = try makeServer()
        let attempt = try RelayClientPairingAttempt(challenge: server.challenge, now: now)
        let offered = try await server.accept(attempt.request, now: now)
        let provisional = try attempt.complete(with: offered.candidate, now: now)
        let pendingSummary = await server.pendingCandidateSummary()
        let summary = try XCTUnwrap(pendingSummary)
        XCTAssertEqual(provisional.shortAuthenticationString, summary.shortAuthenticationString)
        XCTAssertEqual(provisional.shortAuthenticationString.digits.count, 6)

        let waiting = try await server.confirm(try provisional.confirmation(decision: .codesMatch), now: now)
        XCTAssertEqual(waiting.response.state, .waitingForMac)
        try await server.approve(candidateID: offered.candidate.candidateID, now: now)
        let established = try await server.confirm(try provisional.confirmation(decision: .codesMatch), now: now)
        XCTAssertEqual(established.response.state, .established)
        XCTAssertNotNil(established.session)
        _ = try provisional.complete(with: try XCTUnwrap(established.response.acceptance), now: now)
    }

    func testCommitmentRejectsNonceGrindingAndTranscriptChanges() async throws {
        let server = try makeServer()
        let attempt = try RelayClientPairingAttempt(challenge: server.challenge, now: now)
        let offered = try await server.accept(attempt.request, now: now)
        let tampered = try RelayPairingCandidate(
            candidateID: offered.candidate.candidateID,
            sessionID: offered.candidate.sessionID,
            serverNonce: Data(repeating: 0x99, count: 32),
            expiresAtUnixMilliseconds: offered.candidate.expiresAtUnixMilliseconds,
            serverProof: offered.candidate.serverProof
        )
        XCTAssertThrowsError(try attempt.complete(with: tampered, now: now)) {
            XCTAssertEqual($0 as? RelaySessionError, .nonceCommitmentMismatch)
        }
        let alternate = try RelayPairingRequest(
            sessionID: attempt.request.sessionID,
            clientPublicKey: attempt.request.clientPublicKey,
            clientNonce: Data(repeating: 0x77, count: 32)
        )
        XCTAssertNotEqual(
            RelayCanonical.pairingTranscript(challenge: server.challenge, request: attempt.request, serverNonce: offered.candidate.serverNonce),
            RelayCanonical.pairingTranscript(challenge: server.challenge, request: alternate, serverNonce: offered.candidate.serverNonce)
        )
    }

    func testCandidateIsExclusiveAndExhaustionFailsClosed() async throws {
        let server = try makeServer()
        let first = try RelayClientPairingAttempt(challenge: server.challenge, now: now)
        let offered = try await server.accept(first.request, now: now)
        let second = try RelayClientPairingAttempt(challenge: server.challenge, now: now)
        await XCTAssertThrowsErrorAsync(try await server.accept(second.request, now: now), .pairingCandidateInProgress)
        try await server.reject(candidateID: offered.candidate.candidateID, now: now)
        for _ in 0 ..< 2 {
            let next = try RelayClientPairingAttempt(challenge: server.challenge, now: now)
            let candidate = try await server.accept(next.request, now: now)
            try await server.reject(candidateID: candidate.candidate.candidateID, now: now)
        }
        let exhausted = try RelayClientPairingAttempt(challenge: server.challenge, now: now)
        await XCTAssertThrowsErrorAsync(try await server.accept(exhausted.request, now: now), .pairingAttemptsExhausted)
    }

    func testReplayProtectionSurvivesEstablishedSession() async throws {
        let (client, serverSession) = try await establishedSessions()
        let request = try client.signRequest(
            method: "GET", requestTarget: "/relay/v1/playlist.m3u8", timestamp: now, nonce: "replay-nonce-0001", body: Data()
        )
        let store = try RelayReplayNonceStore()
        try await serverSession.verify(request, actualMethod: "GET", actualRequestTarget: "/relay/v1/playlist.m3u8", body: Data(), now: now, replayStore: store)
        await XCTAssertThrowsErrorAsync(
            try await serverSession.verify(request, actualMethod: "GET", actualRequestTarget: "/relay/v1/playlist.m3u8", body: Data(), now: now, replayStore: store),
            .replayDetected
        )
    }

    func testPendingWindowExpiresAfterSixtySeconds() async throws {
        let server = try RelayServerPairingContext(now: now, challengeTTL: 60)
        let attempt = try RelayClientPairingAttempt(challenge: server.challenge, now: now)
        await XCTAssertThrowsErrorAsync(
            try await server.accept(attempt.request, now: now.addingTimeInterval(60.001)),
            .expiredChallenge
        )
    }

    private func makeServer() throws -> RelayServerPairingContext {
        try RelayServerPairingContext(
            serverPrivateKeyData: Data(repeating: 0x11, count: 32),
            serverNonce: Data(repeating: 0x22, count: 32),
            now: now
        )
    }

    private func establishedSessions() async throws -> (RelayEstablishedSession, RelayEstablishedSession) {
        let server = try makeServer()
        let attempt = try RelayClientPairingAttempt(challenge: server.challenge, now: now)
        let candidate = try await server.accept(attempt.request, now: now)
        let provisional = try attempt.complete(with: candidate.candidate, now: now)
        try await server.approve(candidateID: candidate.candidate.candidateID, now: now)
        let confirmed = try await server.confirm(try provisional.confirmation(decision: .codesMatch), now: now)
        return (try provisional.complete(with: try XCTUnwrap(confirmed.response.acceptance), now: now), try XCTUnwrap(confirmed.session))
    }
}

private func XCTAssertThrowsErrorAsync<T>(
    _ expression: @autoclosure () async throws -> T,
    _ expected: RelaySessionError,
    file: StaticString = #filePath,
    line: UInt = #line
) async {
    do {
        _ = try await expression()
        XCTFail("Expected \(expected)", file: file, line: line)
    } catch {
        XCTAssertEqual(error as? RelaySessionError, expected, file: file, line: line)
    }
}

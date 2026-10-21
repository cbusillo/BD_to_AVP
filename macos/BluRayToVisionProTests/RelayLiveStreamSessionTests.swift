import Foundation
import XCTest
#if os(visionOS)
@testable import BDToAVPPlayer
#else
@testable import BluRayToVisionPro
#endif

/// Shared by the Mac and player test targets, because both compile `RelaySessionCore`.
final class RelayLiveStreamSessionTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_700_000_000)
    private let parentSessionID = try! RelaySessionIdentifier(rawValue: "A9B8C7D6-E5F4-4321-ABCD-1234567890AB")
    private let streamA = UUID(uuidString: "00000000-0000-4000-8000-000000000002")!
    private let streamB = UUID(uuidString: "00000000-0000-4000-8000-000000000003")!

    /// Expected values were computed once, independently of this code, with Python's standard
    /// library. The HKDF below reproduces RFC 5869 test case 1 (OKM 3cb25f25…5865) before use:
    ///
    ///     def hkdf(ikm, salt, info, n):
    ///         prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    ///         okm, t, i = b"", b"", 1
    ///         while len(okm) < n:
    ///             t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest(); okm += t; i += 1
    ///         return okm[:n]
    ///     def transcript(domain, fields):
    ///         data = domain.encode() + b"\0"
    ///         for name, value in fields:
    ///             data += struct.pack(">Q", len(name)) + name.encode() + struct.pack(">Q", len(value)) + value
    ///         return data
    ///     stream = uuid.UUID("00000000-0000-4000-8000-000000000002").bytes
    ///     info = transcript("bd-to-avp.relay.live-stream-key.v1", [
    ///         ("parentSessionID", b"00000000-0000-4000-8000-000000000001"),
    ///         ("streamID", stream), ("label", label.encode())])
    ///     hkdf(bytes(range(32)), stream, info, 32).hex()
    func testDerivationMatchesIndependentRFC5869Vector() throws {
        let parentValue = Data(0 ..< 32)
        let parent = try RelaySessionIdentifier(rawValue: "00000000-0000-4000-8000-000000000001")
        func derived(_ label: String) -> String {
            RelayLiveStreamDerivation.value(
                parentValue: parentValue,
                parentSessionID: parent,
                streamID: streamA,
                label: label
            ).hexadecimalString
        }

        XCTAssertEqual(
            derived(RelayLiveStreamDerivation.Label.clientToServerRequest),
            "e01ca9d8b7dd0a97f5f4a8d8b9a78c34e9e0ca99eb162b3919f0c00049807063"
        )
        XCTAssertEqual(
            derived(RelayLiveStreamDerivation.Label.mediaCapability),
            "ddd6a2f55865a5b9c3d8576bb009a5a512bd8a107e665971716f4bc2fa2cf783"
        )
    }

    /// The parent here is the deterministic pairing from `testGoldenProtocolTranscriptVectors`,
    /// whose session identity and media capability are public in that test. The expected values
    /// apply the Python `hkdf` above to those base64url-decoded parent values, with parent session
    /// ID a9b8c7d6-e5f4-4321-abcd-1234567890ab and stream 00000000-0000-4000-8000-000000000002.
    func testStreamSessionTakesStreamIDAndDerivesEachValueFromItsSameNamedParentValue() async throws {
        let sessions = try await pairedSessions()
        let stream = try sessions.client.liveStreamSession(streamID: streamA)

        XCTAssertEqual(stream.sessionID.rawValue, streamA.uuidString.lowercased())
        XCTAssertEqual(stream.role, sessions.client.role)
        XCTAssertEqual(stream.expiresAtUnixMilliseconds, sessions.client.expiresAtUnixMilliseconds)
        XCTAssertEqual(stream.sessionIdentity.value, "D-EB8VthPVnCo26opMTIymPEdPy1L6ZOGAQEPr9TWyk")
        XCTAssertEqual(stream.mediaCapability.value, "ND21Qt9ZnV4ucY5oFOcD5oeiJvrfgOGSXeQnkMnElQI")
    }

    func testMacAndPlayerDeriveTheSameStreamSession() async throws {
        let sessions = try await pairedSessions()
        let player = try sessions.client.liveStreamSession(streamID: streamA)
        let mac = try sessions.server.liveStreamSession(streamID: streamA)

        XCTAssertEqual(player.sessionID, mac.sessionID)
        XCTAssertEqual(player.mediaCapability, mac.mediaCapability)
        XCTAssertEqual(player.sessionIdentity, mac.sessionIdentity)
        let request = try player.signRequest(
            method: "GET",
            requestTarget: RelayWireContract.playlistSnapshotPath,
            timestamp: now,
            nonce: "live-agree-000001",
            body: Data()
        )
        try await mac.verify(
            request,
            actualMethod: request.method,
            actualRequestTarget: request.requestTarget,
            body: Data(),
            now: now,
            replayStore: try RelayReplayNonceStore()
        )
        let macRequest = try mac.signRequest(
            method: "POST",
            requestTarget: "/control?side=server",
            timestamp: now,
            nonce: "live-agree-000002",
            body: Data()
        )
        try await player.verify(
            macRequest,
            actualMethod: macRequest.method,
            actualRequestTarget: macRequest.requestTarget,
            body: Data(),
            now: now,
            replayStore: try RelayReplayNonceStore()
        )
        let response = try mac.authenticateResponse(requestNonce: request.nonce, statusCode: 200, body: Data("ok".utf8))
        try player.verifyResponse(response, requestNonce: request.nonce, actualStatusCode: 200, body: Data("ok".utf8), now: now)
    }

    func testRequestsAndResponsesDoNotMoveBetweenStreamsOrToTheParent() async throws {
        let sessions = try await pairedSessions()
        let playerA = try sessions.client.liveStreamSession(streamID: streamA)
        let playerB = try sessions.client.liveStreamSession(streamID: streamB)
        let macA = try sessions.server.liveStreamSession(streamID: streamA)
        let macB = try sessions.server.liveStreamSession(streamID: streamB)
        let request = try playerA.signRequest(
            method: "GET",
            requestTarget: RelayWireContract.playlistSnapshotPath,
            timestamp: now,
            nonce: "live-move-0000001",
            body: Data()
        )

        // Under each other session ID, the signature for stream A does not verify.
        for (target, verifier) in [(macB.sessionID, macB), (sessions.server.sessionID, sessions.server)] {
            let moved = try relabel(request, sessionID: target)
            await assertRelayError(.requestSignatureMismatch) {
                try await verifier.verify(
                    moved,
                    actualMethod: moved.method,
                    actualRequestTarget: moved.requestTarget,
                    body: Data(),
                    now: self.now,
                    replayStore: try RelayReplayNonceStore()
                )
            }
        }
        // A parent-signed request does not verify as a stream request either.
        let parentRequest = try sessions.client.signRequest(
            method: "GET",
            requestTarget: RelayWireContract.playlistSnapshotPath,
            timestamp: now,
            nonce: "live-move-0000002",
            body: Data()
        )
        let movedParentRequest = try relabel(parentRequest, sessionID: macA.sessionID)
        await assertRelayError(.requestSignatureMismatch) {
            try await macA.verify(
                movedParentRequest,
                actualMethod: movedParentRequest.method,
                actualRequestTarget: movedParentRequest.requestTarget,
                body: Data(),
                now: self.now,
                replayStore: try RelayReplayNonceStore()
            )
        }

        let responseA = try macA.authenticateResponse(requestNonce: request.nonce, statusCode: 410, body: Data())
        let responseAsB = try RelayAuthenticatedResponse(
            sessionID: playerB.sessionID,
            signerRole: responseA.signerRole,
            requestNonce: responseA.requestNonce,
            statusCode: responseA.statusCode,
            bodySHA256: responseA.bodySHA256,
            signature: responseA.signature
        )
        XCTAssertThrowsError(
            try playerB.verifyResponse(responseAsB, requestNonce: request.nonce, actualStatusCode: 410, body: Data(), now: now)
        ) { XCTAssertEqual($0 as? RelaySessionError, .responseSignatureMismatch) }

        XCTAssertNotEqual(playerA.mediaCapability, playerB.mediaCapability)
        XCTAssertNotEqual(playerA.mediaCapability, sessions.client.mediaCapability)
    }

    func testReplayIsRejectedPerStreamSessionInTheSharedStore() async throws {
        let sessions = try await pairedSessions()
        let store = try RelayReplayNonceStore()
        let nonce = "live-replay-00001"
        let streamA = try sessions.server.liveStreamSession(streamID: self.streamA)
        let streamB = try sessions.server.liveStreamSession(streamID: self.streamB)
        let requests = try [
            sessions.client.liveStreamSession(streamID: self.streamA),
            sessions.client.liveStreamSession(streamID: self.streamB),
            sessions.client,
        ].map {
            try $0.signRequest(method: "GET", requestTarget: "/relay/v1/playlist.json", timestamp: now, nonce: nonce, body: Data())
        }
        let verifiers = [streamA, streamB, sessions.server]

        for (request, verifier) in zip(requests, verifiers) {
            try await verifier.verify(
                request,
                actualMethod: request.method,
                actualRequestTarget: request.requestTarget,
                body: Data(),
                now: now,
                replayStore: store
            )
        }
        await assertRelayError(.replayDetected) {
            try await streamA.verify(
                requests[0],
                actualMethod: requests[0].method,
                actualRequestTarget: requests[0].requestTarget,
                body: Data(),
                now: self.now,
                replayStore: store
            )
        }
    }

    func testStreamIDCannotBeTheParentSessionID() async throws {
        let sessions = try await pairedSessions()
        let parentAsStream = try XCTUnwrap(UUID(uuidString: sessions.client.sessionID.rawValue))

        XCTAssertThrowsError(try sessions.client.liveStreamSession(streamID: parentAsStream)) {
            XCTAssertEqual($0 as? RelaySessionError, .invalidSessionIdentifier)
        }
        var candidates = [parentAsStream, streamA]
        let issued = LiveStreamIdentifier.make(excluding: [sessions.client.sessionID]) { candidates.removeFirst() }
        XCTAssertEqual(issued, streamA)
    }

    private func relabel(
        _ request: RelayAuthenticatedRequest,
        sessionID: RelaySessionIdentifier
    ) throws -> RelayAuthenticatedRequest {
        try RelayAuthenticatedRequest(
            sessionID: sessionID,
            signerRole: request.signerRole,
            method: request.method,
            requestTarget: request.requestTarget,
            timestampUnixMilliseconds: request.timestampUnixMilliseconds,
            nonce: request.nonce,
            bodySHA256: request.bodySHA256,
            signature: request.signature
        )
    }

    /// The deterministic pairing used by `RelaySessionCoreTests.testGoldenProtocolTranscriptVectors`.
    private func pairedSessions() async throws -> (client: RelayEstablishedSession, server: RelayEstablishedSession) {
        let server = try RelayServerPairingContext(
            sessionID: parentSessionID,
            serverPrivateKeyData: Data(repeating: 0x11, count: 32),
            serverNonce: Data(repeating: 0x22, count: 32),
            now: now,
            challengeTTL: 120,
            candidateTTL: 60,
            sessionTTL: 7_200,
            maximumCandidates: 3
        )
        let challenge = try await server.currentChallenge(now: now)
        let client = try RelayClientPairingAttempt(
            challenge: challenge,
            clientPrivateKeyData: Data(repeating: 0x33, count: 32),
            clientNonce: Data(repeating: 0x44, count: 32),
            now: now
        )
        let offer = try await server.accept(client.request, now: now)
        let provisional = try client.complete(with: offer.candidate, now: now)
        try await server.approve(candidateID: offer.candidate.candidateID, now: now)
        let established = try await server.confirm(provisional.confirmation(decision: .codesMatch), now: now)
        return (
            try provisional.complete(with: XCTUnwrap(established.response.acceptance), now: now),
            try XCTUnwrap(established.session)
        )
    }

    private func assertRelayError(
        _ expectedError: RelaySessionError,
        operation: () async throws -> Void
    ) async {
        do {
            try await operation()
            XCTFail("Expected \(expectedError)")
        } catch {
            XCTAssertEqual(error as? RelaySessionError, expectedError)
        }
    }
}

private extension Data {
    var hexadecimalString: String {
        map { String(format: "%02x", $0) }.joined()
    }
}

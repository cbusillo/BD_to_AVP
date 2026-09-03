import Foundation
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class RelaySessionCoreTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_700_000_000)
    private let sessionID = try! RelaySessionIdentifier(rawValue: "A9B8C7D6-E5F4-4321-ABCD-1234567890AB")
    private let pairingCode = try! RelayPairingCode("2345-6789-ABCD-EFGH")
    private let wrongPairingCode = try! RelayPairingCode("QRST-UVWX-YZ23-4567")

    func testSuccessfulPairingSeparatesChallengeAndSessionExpiry() async throws {
        let server = try makeServer(challengeTTL: 30, sessionTTL: 7_200)
        let client = try makeClient(challenge: server.challenge, pairingCode: pairingCode)
        let result = try await server.accept(client.request, now: now)
        let clientSession = try client.complete(with: result.acceptance, now: now)

        XCTAssertEqual(clientSession.role, .client)
        XCTAssertEqual(result.session.role, .server)
        XCTAssertEqual(clientSession.sessionID, result.session.sessionID)
        XCTAssertEqual(clientSession.sessionIdentity, result.session.sessionIdentity)
        XCTAssertEqual(clientSession.mediaCapability, result.session.mediaCapability)
        XCTAssertEqual(result.acceptance.expiresAtUnixMilliseconds, 1_700_007_200_000)
        XCTAssertEqual(server.challenge.expiresAtUnixMilliseconds, 1_700_000_030_000)
        XCTAssertNotEqual(result.acceptance.expiresAtUnixMilliseconds, server.challenge.expiresAtUnixMilliseconds)
        XCTAssertFalse(String(describing: clientSession.sessionIdentity).contains(clientSession.sessionIdentity.value))
        XCTAssertFalse(String(reflecting: clientSession.sessionIdentity).contains(clientSession.sessionIdentity.value))
    }

    func testPairingCodeIsCanonicalHighEntropyAndRedacted() throws {
        let normalized = try RelayPairingCode("2345 6789-abcd-efgh")
        XCTAssertEqual(normalized, pairingCode)
        XCTAssertEqual(normalized.formattedValue, "2345-6789-ABCD-EFGH")
        XCTAssertEqual(RelayPairingCode.alphabet.count, 32)
        XCTAssertEqual(RelayPairingCode.random().formattedValue.count, 19)
        XCTAssertFalse(String(describing: normalized).contains(normalized.formattedValue))
        XCTAssertFalse(String(reflecting: normalized).contains(normalized.formattedValue))
    }

    func testPairingCodeRejectsAmbiguousAndWrongLengthInput() {
        XCTAssertThrowsError(try RelayPairingCode("0123-4567-89AB-CDEF"))
        XCTAssertThrowsError(try RelayPairingCode("2345-6789-ABCD-EFG"))
        XCTAssertThrowsError(try RelayPairingCode("2345-6789-ABCD-EFGH!"))
    }

    func testPairingAttemptBudgetExhaustsWithoutLiveReset() async throws {
        XCTAssertThrowsError(try makeServer(maximumFailedAttempts: 6))
        let server = try makeServer(maximumFailedAttempts: 2)
        let wrongClient = try makeClient(challenge: server.challenge, pairingCode: wrongPairingCode)

        await assertRelayError(.pairingProofMismatch) {
            _ = try await server.accept(wrongClient.request, now: self.now)
        }
        await assertRelayError(.pairingAttemptsExhausted) {
            _ = try await server.accept(wrongClient.request, now: self.now)
        }
        let remainingAttempts = await server.remainingFailedAttempts()
        XCTAssertEqual(remainingAttempts, 0)
        await assertRelayError(.pairingAttemptsExhausted) {
            _ = try await server.accept(wrongClient.request, now: self.now)
        }
    }

    func testMismatchedSessionDoesNotConsumePairingAttemptBudget() async throws {
        let server = try makeServer(maximumFailedAttempts: 2)
        let client = try makeClient(challenge: server.challenge, pairingCode: pairingCode)
        let otherSessionID = try RelaySessionIdentifier(rawValue: "11111111-2222-4333-8444-555555555555")
        let mismatchedRequest = try RelayPairingRequest(
            sessionID: otherSessionID,
            clientPublicKey: client.request.clientPublicKey,
            clientNonce: client.request.clientNonce,
            pairingProof: client.request.pairingProof
        )

        await assertRelayError(.invalidRequest) {
            _ = try await server.accept(mismatchedRequest, now: self.now)
        }
        let remainingAttempts = await server.remainingFailedAttempts()
        XCTAssertEqual(remainingAttempts, 2)
    }

    func testPairingContextRejectsSecondSuccess() async throws {
        let server = try makeServer()
        let client = try makeClient(challenge: server.challenge, pairingCode: pairingCode)

        _ = try await server.accept(client.request, now: now)
        await assertRelayError(.pairingAlreadyCompleted) {
            _ = try await server.accept(client.request, now: self.now)
        }
    }

    func testProtocolMessagesRoundTripCodable() async throws {
        let server = try makeServer()
        let client = try makeClient(challenge: server.challenge, pairingCode: pairingCode)
        let result = try await server.accept(client.request, now: now)
        let session = try client.complete(with: result.acceptance, now: now)
        let authenticatedRequest = try session.signRequest(
            method: "GET",
            requestTarget: "/playlist.m3u8?edition=main",
            timestamp: now,
            nonce: "codable-nonce-0001",
            body: Data()
        )

        XCTAssertEqual(try roundTrip(server.challenge), server.challenge)
        XCTAssertEqual(try roundTrip(client.request), client.request)
        XCTAssertEqual(try roundTrip(result.acceptance), result.acceptance)
        XCTAssertEqual(try roundTrip(authenticatedRequest), authenticatedRequest)
    }

    func testMalformedCodablePayloadsAreRejected() async throws {
        let server = try makeServer()
        let client = try makeClient(challenge: server.challenge, pairingCode: pairingCode)
        let sessions = try await pairedSessions()
        let request = try sessions.client.signRequest(
            method: "GET",
            requestTarget: "/playlist.m3u8",
            timestamp: now,
            nonce: "malformed-nonce-01",
            body: Data()
        )

        XCTAssertThrowsError(try JSONDecoder().decode(
            RelaySessionChallenge.self,
            from: replacingEncodedSubstring(
                in: server.challenge,
                original: "\"expiresAtUnixMilliseconds\":1700000120000",
                replacement: "\"expiresAtUnixMilliseconds\":9223372036854775807"
            )
        ))
        XCTAssertThrowsError(try JSONDecoder().decode(
            RelayPairingRequest.self,
            from: replacingEncodedSubstring(
                in: client.request,
                original: Data(repeating: 0x44, count: 32).base64EncodedString(),
                replacement: Data(repeating: 1, count: 31).base64EncodedString()
            )
        ))
        XCTAssertThrowsError(try JSONDecoder().decode(
            RelayAuthenticatedRequest.self,
            from: replacingEncodedSubstring(
                in: request,
                original: "\"method\":\"GET\"",
                replacement: "\"method\":\"\(String(repeating: "A", count: 33))\""
            )
        ))
        XCTAssertThrowsError(try JSONDecoder().decode(
            RelayAuthenticatedRequest.self,
            from: replacingEncodedSubstring(
                in: request,
                original: "\"signerRole\":\"client\"",
                replacement: "\"signerRole\":\"observer\""
            )
        ))
    }

    func testAcceptanceProofAndExpiryTamperingAreRejected() async throws {
        let server = try makeServer()
        let client = try makeClient(challenge: server.challenge, pairingCode: pairingCode)
        let result = try await server.accept(client.request, now: now)
        var proof = result.acceptance.serverProof
        proof[0] ^= 0xFF
        let proofTampered = try RelayPairingAcceptance(
            sessionID: result.acceptance.sessionID,
            expiresAtUnixMilliseconds: result.acceptance.expiresAtUnixMilliseconds,
            serverProof: proof
        )
        let expiryTampered = try RelayPairingAcceptance(
            sessionID: result.acceptance.sessionID,
            expiresAtUnixMilliseconds: result.acceptance.expiresAtUnixMilliseconds + 1,
            serverProof: result.acceptance.serverProof
        )

        XCTAssertThrowsError(try client.complete(with: proofTampered, now: now)) { error in
            XCTAssertEqual(error as? RelaySessionError, .acceptanceProofMismatch)
        }
        XCTAssertThrowsError(try client.complete(with: expiryTampered, now: now)) { error in
            XCTAssertEqual(error as? RelaySessionError, .acceptanceProofMismatch)
        }
    }

    func testClientRejectsAuthenticatedSessionLifetimeAboveMaximum() async throws {
        let server = try makeServer()
        let client = try makeClient(challenge: server.challenge, pairingCode: pairingCode)
        let keyMaterial = try RelayCrypto.derivedKeyMaterial(
            sessionID: sessionID,
            ownPrivateKeyData: Data(repeating: 0x11, count: 32),
            peerPublicKeyData: client.request.clientPublicKey,
            transcript: RelayCanonical.pairingTranscript(challenge: server.challenge, request: client.request)
        )
        let nowMilliseconds = try XCTUnwrap(RelayTime.unixMilliseconds(for: now))
        let excessiveExpiration = nowMilliseconds + RelayLimits.maximumSessionTTLMilliseconds + 1
        let acceptance = try RelayPairingAcceptance(
            sessionID: sessionID,
            expiresAtUnixMilliseconds: excessiveExpiration,
            serverProof: RelayCrypto.acceptanceProof(
                keyMaterial: keyMaterial,
                challenge: server.challenge,
                request: client.request,
                sessionExpirationUnixMilliseconds: excessiveExpiration
            )
        )

        XCTAssertThrowsError(try client.complete(with: acceptance, now: now)) { error in
            XCTAssertEqual(error as? RelaySessionError, .invalidRequest)
        }
    }

    func testExpiredChallengeIsRejectedByBothSides() async throws {
        let server = try makeServer(challengeTTL: 1)
        let client = try makeClient(challenge: server.challenge, pairingCode: pairingCode)

        await assertRelayError(.expiredChallenge) {
            _ = try await server.accept(client.request, now: self.now.addingTimeInterval(2))
        }
        XCTAssertThrowsError(
            try RelayClientPairingAttempt(
                challenge: server.challenge,
                pairingCode: pairingCode,
                clientPrivateKeyData: Data(repeating: 0x33, count: 32),
                clientNonce: Data(repeating: 0x44, count: 32),
                now: now.addingTimeInterval(2)
            )
        ) { error in
            XCTAssertEqual(error as? RelaySessionError, .expiredChallenge)
        }
    }

    func testEstablishedSessionOutlivesPairingChallengeButStillExpires() async throws {
        let sessions = try await pairedSessions(challengeTTL: 1, sessionTTL: 60)
        let request = try sessions.client.signRequest(
            method: "GET",
            requestTarget: "/playlist.m3u8",
            timestamp: now.addingTimeInterval(2),
            nonce: "session-expiry-001",
            body: Data()
        )
        try await sessions.server.verify(
            request,
            actualMethod: request.method,
            actualRequestTarget: request.requestTarget,
            body: Data(),
            now: now.addingTimeInterval(2),
            replayStore: try RelayReplayNonceStore()
        )

        await assertRelayError(.requestExpired) {
            try await sessions.server.verify(
                request,
                actualMethod: request.method,
                actualRequestTarget: request.requestTarget,
                body: Data(),
                now: self.now.addingTimeInterval(61),
                replayStore: try RelayReplayNonceStore()
            )
        }
        XCTAssertThrowsError(try sessions.client.signRequest(
            method: "GET",
            requestTarget: "/playlist.m3u8",
            timestamp: now.addingTimeInterval(61),
            nonce: "session-expiry-002",
            body: Data()
        )) { error in
            XCTAssertEqual(error as? RelaySessionError, .requestExpired)
        }
    }

    func testAuthenticatedRequestTimestampCannotCrossSessionExpiry() async throws {
        let server = try makeServer(sessionTTL: 60)
        let client = try makeClient(challenge: server.challenge, pairingCode: pairingCode)
        let keyMaterial = try RelayCrypto.derivedKeyMaterial(
            sessionID: sessionID,
            ownPrivateKeyData: Data(repeating: 0x11, count: 32),
            peerPublicKeyData: client.request.clientPublicKey,
            transcript: RelayCanonical.pairingTranscript(challenge: server.challenge, request: client.request)
        )
        let result = try await server.accept(client.request, now: now)
        let timestamp = result.acceptance.expiresAtUnixMilliseconds + 1
        let bodyHash = RelayCrypto.sha256(Data())
        let unsignedRequest = try RelayAuthenticatedRequest(
            sessionID: sessionID,
            signerRole: .client,
            method: "GET",
            requestTarget: "/playlist.m3u8",
            timestampUnixMilliseconds: timestamp,
            nonce: "expiry-boundary-001",
            bodySHA256: bodyHash,
            signature: Data(repeating: 0, count: 32)
        )
        let signedRequest = try RelayAuthenticatedRequest(
            sessionID: sessionID,
            signerRole: .client,
            method: unsignedRequest.method,
            requestTarget: unsignedRequest.requestTarget,
            timestampUnixMilliseconds: timestamp,
            nonce: unsignedRequest.nonce,
            bodySHA256: bodyHash,
            signature: RelayCrypto.hmac(
                keyMaterial: keyMaterial.clientToServerRequestKey,
                message: RelayCanonical.authenticatedRequestTranscript(unsignedRequest)
            )
        )

        await assertRelayError(.requestExpired) {
            try await result.session.verify(
                signedRequest,
                actualMethod: signedRequest.method,
                actualRequestTarget: signedRequest.requestTarget,
                body: Data(),
                now: RelayTime.date(fromUnixMilliseconds: result.acceptance.expiresAtUnixMilliseconds - 1),
                replayStore: try RelayReplayNonceStore()
            )
        }
    }

    func testDirectionalSigningWorksBothWays() async throws {
        let sessions = try await pairedSessions()
        let clientRequest = try sessions.client.signRequest(
            method: "GET",
            requestTarget: "/playlist.m3u8?side=client",
            timestamp: now,
            nonce: "direction-client-01",
            body: Data()
        )
        let serverRequest = try sessions.server.signRequest(
            method: "POST",
            requestTarget: "/control?side=server",
            timestamp: now,
            nonce: "direction-server-01",
            body: Data("pause".utf8)
        )

        XCTAssertEqual(clientRequest.signerRole, .client)
        XCTAssertEqual(serverRequest.signerRole, .server)
        try await sessions.server.verify(
            clientRequest,
            actualMethod: clientRequest.method,
            actualRequestTarget: clientRequest.requestTarget,
            body: Data(),
            now: now,
            replayStore: try RelayReplayNonceStore()
        )
        try await sessions.client.verify(
            serverRequest,
            actualMethod: serverRequest.method,
            actualRequestTarget: serverRequest.requestTarget,
            body: Data("pause".utf8),
            now: now,
            replayStore: try RelayReplayNonceStore()
        )
    }

    func testReflectedRequestsAreRejectedByBothRoles() async throws {
        let sessions = try await pairedSessions()
        let clientRequest = try sessions.client.signRequest(
            method: "GET",
            requestTarget: "/playlist.m3u8",
            timestamp: now,
            nonce: "reflection-client-1",
            body: Data()
        )
        let serverRequest = try sessions.server.signRequest(
            method: "GET",
            requestTarget: "/playlist.m3u8",
            timestamp: now,
            nonce: "reflection-server-1",
            body: Data()
        )

        await assertRelayError(.invalidRequest) {
            try await sessions.client.verify(
                clientRequest,
                actualMethod: clientRequest.method,
                actualRequestTarget: clientRequest.requestTarget,
                body: Data(),
                now: self.now,
                replayStore: try RelayReplayNonceStore()
            )
        }
        await assertRelayError(.invalidRequest) {
            try await sessions.server.verify(
                serverRequest,
                actualMethod: serverRequest.method,
                actualRequestTarget: serverRequest.requestTarget,
                body: Data(),
                now: self.now,
                replayStore: try RelayReplayNonceStore()
            )
        }
    }

    func testSignerRoleAndExactQueryTargetAreBoundToSignature() async throws {
        let sessions = try await pairedSessions()
        let request = try sessions.client.signRequest(
            method: "GET",
            requestTarget: "/segments/1.m4s?track=left&token=one",
            timestamp: now,
            nonce: "target-binding-001",
            body: Data()
        )
        let roleTampered = try RelayAuthenticatedRequest(
            sessionID: request.sessionID,
            signerRole: .server,
            method: request.method,
            requestTarget: request.requestTarget,
            timestampUnixMilliseconds: request.timestampUnixMilliseconds,
            nonce: request.nonce,
            bodySHA256: request.bodySHA256,
            signature: request.signature
        )
        let queryTampered = try RelayAuthenticatedRequest(
            sessionID: request.sessionID,
            signerRole: request.signerRole,
            method: request.method,
            requestTarget: "/segments/1.m4s?track=right&token=one",
            timestampUnixMilliseconds: request.timestampUnixMilliseconds,
            nonce: request.nonce,
            bodySHA256: request.bodySHA256,
            signature: request.signature
        )

        await assertRelayError(.invalidRequest) {
            try await sessions.server.verify(
                roleTampered,
                actualMethod: roleTampered.method,
                actualRequestTarget: roleTampered.requestTarget,
                body: Data(),
                now: self.now,
                replayStore: try RelayReplayNonceStore()
            )
        }
        await assertRelayError(.requestSignatureMismatch) {
            try await sessions.server.verify(
                queryTampered,
                actualMethod: queryTampered.method,
                actualRequestTarget: queryTampered.requestTarget,
                body: Data(),
                now: self.now,
                replayStore: try RelayReplayNonceStore()
            )
        }
    }

    func testVerificationBindsActualTransportMethodAndTarget() async throws {
        let sessions = try await pairedSessions()
        let request = try sessions.client.signRequest(
            method: "GET",
            requestTarget: "/segments/1.m4s?track=left",
            timestamp: now,
            nonce: "transport-binding-01",
            body: Data()
        )

        await assertRelayError(.invalidRequest) {
            try await sessions.server.verify(
                request,
                actualMethod: "POST",
                actualRequestTarget: request.requestTarget,
                body: Data(),
                now: self.now,
                replayStore: try RelayReplayNonceStore()
            )
        }
        await assertRelayError(.invalidRequest) {
            try await sessions.server.verify(
                request,
                actualMethod: request.method,
                actualRequestTarget: "/control",
                body: Data(),
                now: self.now,
                replayStore: try RelayReplayNonceStore()
            )
        }
    }

    func testRequestInputLengthsAreCapped() async throws {
        let sessions = try await pairedSessions()

        XCTAssertThrowsError(try sessions.client.signRequest(
            method: String(repeating: "A", count: 33),
            requestTarget: "/ok",
            timestamp: now,
            nonce: "length-cap-nonce01",
            body: Data()
        ))
        XCTAssertThrowsError(try sessions.client.signRequest(
            method: "GET",
            requestTarget: "/" + String(repeating: "a", count: 8_192),
            timestamp: now,
            nonce: "length-cap-nonce02",
            body: Data()
        ))
        XCTAssertThrowsError(try sessions.client.signRequest(
            method: "GET",
            requestTarget: "/ok",
            timestamp: now,
            nonce: String(repeating: "a", count: 129),
            body: Data()
        ))
        XCTAssertThrowsError(try sessions.client.signRequest(
            method: "GET",
            requestTarget: "/bad fragment#value",
            timestamp: now,
            nonce: "length-cap-nonce03",
            body: Data()
        ))
    }

    func testBodyAndSignatureTamperingAreRejected() async throws {
        let sessions = try await pairedSessions()
        let request = try sessions.client.signRequest(
            method: "POST",
            requestTarget: "/control",
            timestamp: now,
            nonce: "tamper-request-001",
            body: Data("play".utf8)
        )
        var signature = request.signature
        signature[0] ^= 0x01
        let signatureTampered = try RelayAuthenticatedRequest(
            sessionID: request.sessionID,
            signerRole: request.signerRole,
            method: request.method,
            requestTarget: request.requestTarget,
            timestampUnixMilliseconds: request.timestampUnixMilliseconds,
            nonce: request.nonce,
            bodySHA256: request.bodySHA256,
            signature: signature
        )

        await assertRelayError(.requestBodyMismatch) {
            try await sessions.server.verify(
                request,
                actualMethod: request.method,
                actualRequestTarget: request.requestTarget,
                body: Data("pause".utf8),
                now: self.now,
                replayStore: try RelayReplayNonceStore()
            )
        }
        await assertRelayError(.requestSignatureMismatch) {
            try await sessions.server.verify(
                signatureTampered,
                actualMethod: signatureTampered.method,
                actualRequestTarget: signatureTampered.requestTarget,
                body: Data("play".utf8),
                now: self.now,
                replayStore: try RelayReplayNonceStore()
            )
        }
    }

    func testReplayDetectionIsSessionScoped() async throws {
        let otherSessionID = try RelaySessionIdentifier(rawValue: "11111111-2222-4333-8444-555555555555")
        let store = try RelayReplayNonceStore()
        let nonce = "session-scoped-001"
        let validThrough = try XCTUnwrap(RelayTime.unixMilliseconds(for: now.addingTimeInterval(60)))

        try await store.checkAndInsert(
            sessionID: sessionID,
            nonce: nonce,
            validThroughUnixMilliseconds: validThrough,
            now: now
        )
        try await store.checkAndInsert(
            sessionID: otherSessionID,
            nonce: nonce,
            validThroughUnixMilliseconds: validThrough,
            now: now
        )
        let containsFirstSession = try await store.contains(sessionID: sessionID, nonce: nonce, now: now)
        let containsOtherSession = try await store.contains(sessionID: otherSessionID, nonce: nonce, now: now)
        XCTAssertTrue(containsFirstSession)
        XCTAssertTrue(containsOtherSession)
        await assertRelayError(.replayDetected) {
            try await store.checkAndInsert(
                sessionID: self.sessionID,
                nonce: nonce,
                validThroughUnixMilliseconds: validThrough,
                now: self.now
            )
        }
    }

    func testReplayStoreFailsClosedAtCapacityWithoutEvictingLiveNonces() async throws {
        let store = try RelayReplayNonceStore(capacity: 2)
        let validThrough = try XCTUnwrap(RelayTime.unixMilliseconds(for: now.addingTimeInterval(60)))

        try await store.checkAndInsert(
            sessionID: sessionID,
            nonce: "capacity-nonce-001",
            validThroughUnixMilliseconds: validThrough,
            now: now
        )
        try await store.checkAndInsert(
            sessionID: sessionID,
            nonce: "capacity-nonce-002",
            validThroughUnixMilliseconds: validThrough,
            now: now
        )
        await assertRelayError(.replayCapacityExceeded) {
            try await store.checkAndInsert(
                sessionID: self.sessionID,
                nonce: "capacity-nonce-003",
                validThroughUnixMilliseconds: validThrough,
                now: self.now
            )
        }
        let count = try await store.count(now: now)
        let containsFirst = try await store.contains(
            sessionID: sessionID,
            nonce: "capacity-nonce-001",
            now: now
        )
        let containsSecond = try await store.contains(
            sessionID: sessionID,
            nonce: "capacity-nonce-002",
            now: now
        )
        XCTAssertEqual(count, 2)
        XCTAssertTrue(containsFirst)
        XCTAssertTrue(containsSecond)
    }

    func testReplayValidityRemovesExpiredEntriesBeforeCapacityCheck() async throws {
        let store = try RelayReplayNonceStore(capacity: 1)
        let firstExpiration = try XCTUnwrap(RelayTime.unixMilliseconds(for: now.addingTimeInterval(1)))
        let replacementNow = now.addingTimeInterval(1.001)
        let replacementExpiration = try XCTUnwrap(RelayTime.unixMilliseconds(for: replacementNow.addingTimeInterval(60)))
        try await store.checkAndInsert(
            sessionID: sessionID,
            nonce: "ttl-expiring-0001",
            validThroughUnixMilliseconds: firstExpiration,
            now: now
        )

        try await store.checkAndInsert(
            sessionID: sessionID,
            nonce: "ttl-replacement-01",
            validThroughUnixMilliseconds: replacementExpiration,
            now: replacementNow
        )
        let containsExpired = try await store.contains(
            sessionID: sessionID,
            nonce: "ttl-expiring-0001",
            now: replacementNow
        )
        let containsReplacement = try await store.contains(
            sessionID: sessionID,
            nonce: "ttl-replacement-01",
            now: replacementNow
        )
        XCTAssertFalse(containsExpired)
        XCTAssertTrue(containsReplacement)
    }

    func testReplayRetentionTracksAuthenticatedRequestValidity() async throws {
        let sessions = try await pairedSessions()
        let request = try sessions.client.signRequest(
            method: "GET",
            requestTarget: "/playlist.m3u8",
            timestamp: now,
            nonce: "policy-retention-001",
            body: Data()
        )
        let policy = try RelayRequestValidationPolicy(maximumAge: 300, allowedFutureSkew: 60)
        let store = try RelayReplayNonceStore()

        try await sessions.server.verify(
            request,
            actualMethod: request.method,
            actualRequestTarget: request.requestTarget,
            body: Data(),
            now: now,
            policy: policy,
            replayStore: store
        )
        await assertRelayError(.replayDetected) {
            try await sessions.server.verify(
                request,
                actualMethod: request.method,
                actualRequestTarget: request.requestTarget,
                body: Data(),
                now: self.now.addingTimeInterval(120),
                policy: policy,
                replayStore: store
            )
        }
    }

    func testConcurrentReplayInsertionAllowsExactlyOneWinner() async throws {
        let store = try RelayReplayNonceStore(capacity: 100)
        let capturedSessionID = sessionID
        let capturedNow = now
        let validThrough = try XCTUnwrap(RelayTime.unixMilliseconds(for: now.addingTimeInterval(60)))
        let outcomes = await withTaskGroup(of: RelaySessionError?.self, returning: [RelaySessionError?].self) { group in
            for _ in 0 ..< 32 {
                group.addTask {
                    do {
                        try await store.checkAndInsert(
                            sessionID: capturedSessionID,
                            nonce: "concurrent-nonce-1",
                            validThroughUnixMilliseconds: validThrough,
                            now: capturedNow
                        )
                        return nil
                    } catch {
                        return error as? RelaySessionError
                    }
                }
            }
            var results: [RelaySessionError?] = []
            for await result in group {
                results.append(result)
            }
            return results
        }

        XCTAssertEqual(outcomes.filter { $0 == nil }.count, 1)
        XCTAssertEqual(outcomes.filter { $0 == .replayDetected }.count, 31)
    }

    func testNonceExpiryAndPublicTimeIntervalsRejectOverflowBoundaries() async throws {
        XCTAssertThrowsError(try RelayReplayNonceStore(capacity: Int.max))
        XCTAssertThrowsError(try RelayRequestValidationPolicy(maximumAge: .greatestFiniteMagnitude))
        XCTAssertThrowsError(try RelayServerPairingContext(now: Date(timeIntervalSince1970: .greatestFiniteMagnitude)))

        let store = try RelayReplayNonceStore()
        let nearMaximumDate = RelayTime.date(fromUnixMilliseconds: RelayLimits.maximumUnixMilliseconds - 500)
        await assertRelayError(.invalidTimestamp) {
            try await store.checkAndInsert(
                sessionID: self.sessionID,
                nonce: "overflow-nonce-001",
                validThroughUnixMilliseconds: RelayLimits.maximumUnixMilliseconds - 501,
                now: nearMaximumDate
            )
        }
    }

    func testKeyScheduleSeparatesDirectionsCapabilityAndAcceptance() async throws {
        let server = try makeServer()
        let client = try makeClient(challenge: server.challenge, pairingCode: pairingCode)
        let serverMaterial = try RelayCrypto.derivedKeyMaterial(
            sessionID: sessionID,
            ownPrivateKeyData: Data(repeating: 0x11, count: 32),
            peerPublicKeyData: client.request.clientPublicKey,
            transcript: RelayCanonical.pairingTranscript(challenge: server.challenge, request: client.request)
        )
        let clientMaterial = try RelayCrypto.derivedKeyMaterial(
            sessionID: sessionID,
            ownPrivateKeyData: Data(repeating: 0x33, count: 32),
            peerPublicKeyData: server.challenge.serverPublicKey,
            transcript: RelayCanonical.pairingTranscript(challenge: server.challenge, request: client.request)
        )

        XCTAssertEqual(serverMaterial.sessionIdentity, clientMaterial.sessionIdentity)
        XCTAssertEqual(serverMaterial.clientToServerRequestKey, clientMaterial.clientToServerRequestKey)
        XCTAssertEqual(serverMaterial.serverToClientRequestKey, clientMaterial.serverToClientRequestKey)
        XCTAssertEqual(serverMaterial.mediaCapability, clientMaterial.mediaCapability)
        XCTAssertEqual(serverMaterial.acceptanceProofKey, clientMaterial.acceptanceProofKey)
        XCTAssertEqual(Set([
            serverMaterial.sessionIdentity,
            serverMaterial.clientToServerRequestKey,
            serverMaterial.serverToClientRequestKey,
            serverMaterial.mediaCapability,
            serverMaterial.acceptanceProofKey,
        ]).count, 5)
    }

    func testMediaCapabilityMatchingAndDescriptionsDoNotExposeSecret() async throws {
        let sessions = try await pairedSessions()
        let otherSessions = try await pairedSessions(
            sessionID: try RelaySessionIdentifier(rawValue: "11111111-2222-4333-8444-555555555555")
        )
        let capability = sessions.client.mediaCapability

        XCTAssertTrue(capability.matches(capability.value))
        XCTAssertFalse(capability.matches(capability.value + "x"))
        XCTAssertNotEqual(capability, otherSessions.client.mediaCapability)
        XCTAssertFalse(String(describing: capability).contains(capability.value))
        XCTAssertFalse(String(reflecting: capability).contains(capability.value))
        XCTAssertFalse(String(reflecting: sessions.client).contains(capability.value))
    }

    func testGoldenProtocolTranscriptVectors() async throws {
        let server = try makeServer()
        let client = try makeClient(challenge: server.challenge, pairingCode: pairingCode)
        let result = try await server.accept(client.request, now: now)
        let clientSession = try client.complete(with: result.acceptance, now: now)
        let request = try clientSession.signRequest(
            method: "GET",
            requestTarget: "/playlist.m3u8?edition=main&track=left",
            timestamp: now,
            nonce: "golden-nonce-0001",
            body: Data("golden-body".utf8)
        )

        let pairingDigest = RelayCrypto.sha256(
            RelayCanonical.pairingTranscript(challenge: server.challenge, request: client.request)
        ).hexadecimalString
        let requestDigest = RelayCrypto.sha256(
            RelayCanonical.authenticatedRequestTranscript(request)
        ).hexadecimalString

        XCTAssertEqual(pairingDigest, "1730a92dbe43af656779d816a09b2eb130a6f517d7266c0975396f685e03600e")
        XCTAssertEqual(requestDigest, "0a5824bf58e41acc26fc4c76a39266354920408fea426503ae5f53716986592b")
        XCTAssertEqual(request.signature.hexadecimalString, "9d274aefccb8e01e884f3c1980115c1dfa5c2df10efae30b7c1aa03014966dd0")
        XCTAssertEqual(clientSession.sessionIdentity.value, "1Rb96W3e81TCMrp92nqQNiFqPh1jE7jVmAkKstGZ8Bs")
        XCTAssertEqual(clientSession.mediaCapability.value, "uEzFy9r92oCMBRcGUkBw7v3SxHihPDgtirlaTJw-Bks")
    }

    func testPlaylistGrowthUsesFixedTargetDuration() throws {
        var playlist = try RelayEventPlaylist(targetDuration: 4, retainedSegmentLimit: 3)
        let first = try playlist.append(resourceIdentifier: "segment-000", duration: 2)
        let second = try playlist.append(resourceIdentifier: "segment-001", duration: 3)

        XCTAssertEqual(playlist.targetDuration, 4)
        XCTAssertEqual(playlist.segments, [first, second])
        XCTAssertEqual(playlist.totalDuration, 5)
        XCTAssertThrowsError(try playlist.append(resourceIdentifier: "too-long", duration: 5)) { error in
            XCTAssertEqual(error as? RelayPlaylistError, .segmentExceedsTargetDuration)
        }
    }

    func testPlaylistRetainedHistorySeekValidationIsHonest() throws {
        var playlist = try RelayEventPlaylist(targetDuration: 2, retainedSegmentLimit: 2)
        _ = try playlist.append(resourceIdentifier: "segment-000", duration: 2)
        let retainedFirst = try playlist.append(resourceIdentifier: "segment-001", duration: 2)
        _ = try playlist.append(resourceIdentifier: "segment-002", duration: 2)

        XCTAssertEqual(playlist.earliestPlayableTime, 2)
        XCTAssertEqual(playlist.validateSeek(to: 3), .playable(segment: retainedFirst, offsetMilliseconds: 1_000))
        XCTAssertEqual(playlist.validateSeek(to: 1), .beforeRetainedHistory(earliestPlayableMilliseconds: 2_000))
        XCTAssertEqual(playlist.validateSeek(to: 7), .notYetAvailable(latestAvailableMilliseconds: 6_000))
        XCTAssertEqual(playlist.validateSeek(to: -1), .invalidTime)
        XCTAssertEqual(playlist.validateSeek(to: .nan), .invalidTime)
    }

    func testPlaylistFinalizationPublishesEndAndRejectsFurtherGrowth() throws {
        var playlist = try RelayEventPlaylist(targetDuration: 2, retainedSegmentLimit: 2)
        _ = try playlist.append(resourceIdentifier: "segment-000", duration: 2)
        playlist.finalize()

        XCTAssertTrue(playlist.hasEndList)
        XCTAssertEqual(playlist.validateSeek(to: 2), .ended(finalDurationMilliseconds: 2_000))
        XCTAssertThrowsError(try playlist.append(resourceIdentifier: "segment-001", duration: 2)) { error in
            XCTAssertEqual(error as? RelayPlaylistError, .finalized)
        }
    }

    func testPlaylistRejectsHostilePublicTimeIntervalsAndIdentifiers() throws {
        XCTAssertThrowsError(try RelayEventPlaylist(targetDuration: .greatestFiniteMagnitude, retainedSegmentLimit: 1))
        XCTAssertThrowsError(try RelayEventPlaylist(targetDuration: 1, retainedSegmentLimit: Int.max))

        var playlist = try RelayEventPlaylist(targetDuration: 1, retainedSegmentLimit: 1)
        XCTAssertThrowsError(try playlist.append(resourceIdentifier: "segment", duration: .greatestFiniteMagnitude))
        XCTAssertThrowsError(try playlist.append(
            resourceIdentifier: String(repeating: "a", count: RelayPlaylistLimits.maximumResourceIdentifierLength + 1),
            duration: 1
        ))
        for unsafeIdentifier in ["#EXT-X-ENDLIST", "../segment.m4s", "/segment.m4s", "segment.m4s?token=x", "dir//segment.m4s"] {
            XCTAssertThrowsError(try playlist.append(resourceIdentifier: unsafeIdentifier, duration: 1))
        }
    }

    func testPlaylistSegmentRejectsIntegerOverflow() {
        XCTAssertThrowsError(try RelayPlaylistSegment(
            sequenceNumber: 0,
            startTimeMilliseconds: Int64.max,
            durationMilliseconds: 1,
            resourceIdentifier: "segment"
        ))
        XCTAssertThrowsError(try RelayPlaylistSegment(
            sequenceNumber: 0,
            startTimeMilliseconds: RelayPlaylistLimits.maximumTimelineDurationMilliseconds,
            durationMilliseconds: 1,
            resourceIdentifier: "segment"
        ))
    }

    func testPlaylistHostileCodableIntegersAndCollectionsAreRejected() throws {
        let overflowSegment = """
        {
          "sequenceNumber": 0,
          "startTimeMilliseconds": 9223372036854775807,
          "durationMilliseconds": 1,
          "resourceIdentifier": "segment"
        }
        """
        XCTAssertThrowsError(try JSONDecoder().decode(RelayPlaylistSegment.self, from: Data(overflowSegment.utf8)))

        let overflowingSequencePlaylist = """
        {
          "targetDurationMilliseconds": 1000,
          "retainedSegmentLimit": 1,
          "segments": [{
            "sequenceNumber": 18446744073709551615,
            "startTimeMilliseconds": 0,
            "durationMilliseconds": 1000,
            "resourceIdentifier": "segment"
          }],
          "nextSequenceNumber": 0,
          "nextStartTimeMilliseconds": 1000,
          "isFinalized": false
        }
        """
        XCTAssertThrowsError(try JSONDecoder().decode(
            RelayEventPlaylist.self,
            from: Data(overflowingSequencePlaylist.utf8)
        ))

        let tooManySegments = """
        {
          "targetDurationMilliseconds": 1000,
          "retainedSegmentLimit": 1,
          "segments": [
            {"sequenceNumber": 0, "startTimeMilliseconds": 0, "durationMilliseconds": 1000, "resourceIdentifier": "a"},
            {"sequenceNumber": 1, "startTimeMilliseconds": 1000, "durationMilliseconds": 1000, "resourceIdentifier": "b"}
          ],
          "nextSequenceNumber": 2,
          "nextStartTimeMilliseconds": 2000,
          "isFinalized": false
        }
        """
        XCTAssertThrowsError(try JSONDecoder().decode(RelayEventPlaylist.self, from: Data(tooManySegments.utf8)))
    }

    func testPlaylistSnapshotRejectsHostileOrInconsistentWindows() throws {
        let negativeTimeline = """
        {
          "earliestPlayableTimeMilliseconds": -1,
          "totalDurationMilliseconds": 0,
          "isFinalized": false,
          "segments": []
        }
        """
        XCTAssertThrowsError(try JSONDecoder().decode(
            RelayPlaylistSnapshot.self,
            from: Data(negativeTimeline.utf8)
        ))

        let discontinuousWindow = """
        {
          "earliestPlayableTimeMilliseconds": 0,
          "totalDurationMilliseconds": 3000,
          "isFinalized": false,
          "segments": [
            {"sequenceNumber": 7, "startTimeMilliseconds": 0, "durationMilliseconds": 1000, "resourceIdentifier": "a.m4s"},
            {"sequenceNumber": 9, "startTimeMilliseconds": 2000, "durationMilliseconds": 1000, "resourceIdentifier": "b.m4s"}
          ]
        }
        """
        XCTAssertThrowsError(try JSONDecoder().decode(
            RelayPlaylistSnapshot.self,
            from: Data(discontinuousWindow.utf8)
        ))

        let repeatedSegment = """
        {"sequenceNumber": 0, "startTimeMilliseconds": 0, "durationMilliseconds": 1, "resourceIdentifier": "a.m4s"}
        """
        let repeatedSegments = Array(
            repeating: repeatedSegment,
            count: RelayPlaylistLimits.maximumRetainedSegmentLimit + 1
        ).joined(separator: ",")
        let oversizedWindow = """
        {
          "earliestPlayableTimeMilliseconds": 0,
          "totalDurationMilliseconds": 1,
          "isFinalized": false,
          "segments": [\(repeatedSegments)]
        }
        """
        XCTAssertThrowsError(try JSONDecoder().decode(
            RelayPlaylistSnapshot.self,
            from: Data(oversizedWindow.utf8)
        ))
    }

    func testPlaylistDecodeAtTimelineBoundaryCannotAppendPastLimit() throws {
        let start = RelayPlaylistLimits.maximumTimelineDurationMilliseconds - 1_000
        let payload = """
        {
          "targetDurationMilliseconds": 1000,
          "retainedSegmentLimit": 1,
          "segments": [{
            "sequenceNumber": 10,
            "startTimeMilliseconds": \(start),
            "durationMilliseconds": 1000,
            "resourceIdentifier": "segment"
          }],
          "nextSequenceNumber": 11,
          "nextStartTimeMilliseconds": \(RelayPlaylistLimits.maximumTimelineDurationMilliseconds),
          "isFinalized": false
        }
        """
        var playlist = try JSONDecoder().decode(RelayEventPlaylist.self, from: Data(payload.utf8))

        XCTAssertThrowsError(try playlist.append(resourceIdentifier: "next", duration: 1)) { error in
            XCTAssertEqual(error as? RelayPlaylistError, .invalidSegment)
        }
    }

    private func makeServer(
        sessionID: RelaySessionIdentifier? = nil,
        challengeTTL: TimeInterval = 120,
        sessionTTL: TimeInterval = 7_200,
        maximumFailedAttempts: Int = 5
    ) throws -> RelayServerPairingContext {
        try RelayServerPairingContext(
            sessionID: sessionID ?? self.sessionID,
            pairingCode: pairingCode,
            serverPrivateKeyData: Data(repeating: 0x11, count: 32),
            serverNonce: Data(repeating: 0x22, count: 32),
            now: now,
            challengeTTL: challengeTTL,
            sessionTTL: sessionTTL,
            maximumFailedAttempts: maximumFailedAttempts
        )
    }

    private func makeClient(
        challenge: RelaySessionChallenge,
        pairingCode: RelayPairingCode
    ) throws -> RelayClientPairingAttempt {
        try RelayClientPairingAttempt(
            challenge: challenge,
            pairingCode: pairingCode,
            clientPrivateKeyData: Data(repeating: 0x33, count: 32),
            clientNonce: Data(repeating: 0x44, count: 32),
            now: now
        )
    }

    private func pairedSessions(
        sessionID: RelaySessionIdentifier? = nil,
        challengeTTL: TimeInterval = 120,
        sessionTTL: TimeInterval = 7_200
    ) async throws -> (client: RelayEstablishedSession, server: RelayEstablishedSession) {
        let server = try makeServer(
            sessionID: sessionID,
            challengeTTL: challengeTTL,
            sessionTTL: sessionTTL
        )
        let client = try makeClient(challenge: server.challenge, pairingCode: pairingCode)
        let result = try await server.accept(client.request, now: now)
        return (try client.complete(with: result.acceptance, now: now), result.session)
    }

    private func roundTrip<Value: Codable>(_ value: Value) throws -> Value {
        try JSONDecoder().decode(Value.self, from: JSONEncoder().encode(value))
    }

    private func replacingEncodedSubstring<Value: Encodable>(
        in value: Value,
        original: String,
        replacement: String
    ) throws -> Data {
        guard let encoded = String(data: try JSONEncoder().encode(value), encoding: .utf8),
              encoded.contains(original)
        else {
            throw RelaySessionError.invalidRequest
        }
        return Data(encoded.replacingOccurrences(of: original, with: replacement).utf8)
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

import Foundation
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class RelaySessionCoreTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_700_000_000)
    private let sessionID = try! RelaySessionIdentifier(rawValue: "A9B8C7D6-E5F4-4321-ABCD-1234567890AB")

    func testSuccessfulPairingSeparatesCandidateAndSessionExpiry() async throws {
        let server = try makeServer(challengeTTL: 120, candidateTTL: 60, sessionTTL: 7_200)
        let challenge = try await server.currentChallenge(now: now)
        let client = try makeClient(challenge: challenge)
        let offered = try await server.accept(client.request, now: now)
        let provisional = try client.complete(with: offered.candidate, now: now)
        let pendingSummary = try await server.pendingCandidateSummary(now: now)
        let pending = try XCTUnwrap(pendingSummary)

        XCTAssertEqual(provisional.shortAuthenticationString, pending.shortAuthenticationString)
        XCTAssertEqual(provisional.shortAuthenticationString.digits.count, 6)
        XCTAssertTrue(provisional.shortAuthenticationString.digits.allSatisfy(\.isNumber))
        XCTAssertEqual(offered.candidate.expiresAtUnixMilliseconds, 1_700_000_060_000)
        XCTAssertEqual(challenge.expiresAtUnixMilliseconds, 1_700_000_120_000)

        try await server.approve(candidateID: offered.candidate.candidateID, now: now)
        let confirmation = try await server.confirm(
            provisional.confirmation(decision: .codesMatch),
            now: now
        )
        let acceptance = try XCTUnwrap(confirmation.response.acceptance)
        let clientSession = try provisional.complete(with: acceptance, now: now)
        let serverSession = try XCTUnwrap(confirmation.session)

        XCTAssertEqual(clientSession.role, .client)
        XCTAssertEqual(serverSession.role, .server)
        XCTAssertEqual(clientSession.sessionIdentity, serverSession.sessionIdentity)
        XCTAssertEqual(clientSession.mediaCapability, serverSession.mediaCapability)
        XCTAssertEqual(acceptance.expiresAtUnixMilliseconds, 1_700_007_200_000)
        XCTAssertFalse(String(describing: provisional.shortAuthenticationString).contains(provisional.shortAuthenticationString.digits))
    }

    func testCandidateRequiresBothConfirmationsInEitherOrder() async throws {
        let clientFirstServer = try makeServer()
        let clientFirstChallenge = try await clientFirstServer.currentChallenge(now: now)
        let clientFirstAttempt = try makeClient(challenge: clientFirstChallenge)
        let clientFirstOffer = try await clientFirstServer.accept(clientFirstAttempt.request, now: now)
        let clientFirstProvisional = try clientFirstAttempt.complete(with: clientFirstOffer.candidate, now: now)
        let waiting = try await clientFirstServer.confirm(
            clientFirstProvisional.confirmation(decision: .codesMatch),
            now: now
        )
        XCTAssertEqual(waiting.response.state, .waitingForMac)
        XCTAssertNil(waiting.session)
        try await clientFirstServer.approve(candidateID: clientFirstOffer.candidate.candidateID, now: now)
        let clientFirstEstablished = try await clientFirstServer.confirm(
            clientFirstProvisional.confirmation(decision: .codesMatch),
            now: now
        )
        XCTAssertEqual(clientFirstEstablished.response.state, .established)
        XCTAssertNotNil(clientFirstEstablished.session)

        let macFirstServer = try makeServer()
        let macFirstChallenge = try await macFirstServer.currentChallenge(now: now)
        let macFirstAttempt = try makeClient(challenge: macFirstChallenge)
        let macFirstOffer = try await macFirstServer.accept(macFirstAttempt.request, now: now)
        let macFirstProvisional = try macFirstAttempt.complete(with: macFirstOffer.candidate, now: now)
        try await macFirstServer.approve(candidateID: macFirstOffer.candidate.candidateID, now: now)
        let macFirstEstablished = try await macFirstServer.confirm(
            macFirstProvisional.confirmation(decision: .codesMatch),
            now: now
        )
        XCTAssertEqual(macFirstEstablished.response.state, .established)
        XCTAssertNotNil(macFirstEstablished.session)
    }

    func testCandidateExclusivityExpiryRotationAndAttemptBudget() async throws {
        XCTAssertThrowsError(try makeServer(maximumCandidates: 4))
        let server = try makeServer(challengeTTL: 120, candidateTTL: 1, maximumCandidates: 3)
        let firstChallenge = try await server.currentChallenge(now: now)
        let firstAttempt = try makeClient(challenge: firstChallenge)
        let firstOffer = try await server.accept(firstAttempt.request, now: now)
        let competingAttempt = try makeClient(challenge: firstChallenge)

        await assertRelayError(.pairingCandidateInProgress) {
            _ = try await server.accept(competingAttempt.request, now: self.now)
        }
        let expiredCandidate = try await server.pendingCandidateSummary(now: now.addingTimeInterval(2))
        XCTAssertNil(expiredCandidate)
        let secondChallenge = try await server.currentChallenge(now: now.addingTimeInterval(2))
        XCTAssertNotEqual(secondChallenge.serverPublicKey, firstChallenge.serverPublicKey)
        XCTAssertNotEqual(secondChallenge.serverNonceCommitment, firstChallenge.serverNonceCommitment)
        await assertRelayError(.pairingCandidateNotFound) {
            try await server.approve(candidateID: firstOffer.candidate.candidateID, now: self.now.addingTimeInterval(2))
        }

        let secondAttempt = try makeClient(challenge: secondChallenge, clientPrivateKeyByte: 0x55, clientNonceByte: 0x66)
        let secondOffer = try await server.accept(secondAttempt.request, now: now.addingTimeInterval(2))
        try await server.reject(candidateID: secondOffer.candidate.candidateID, now: now.addingTimeInterval(2))
        let thirdChallenge = try await server.currentChallenge(now: now.addingTimeInterval(2))
        let thirdAttempt = try makeClient(challenge: thirdChallenge, clientPrivateKeyByte: 0x77, clientNonceByte: 0x88)
        let thirdOffer = try await server.accept(thirdAttempt.request, now: now.addingTimeInterval(2))
        try await server.reject(candidateID: thirdOffer.candidate.candidateID, now: now.addingTimeInterval(2))

        await assertRelayError(.pairingAttemptsExhausted) {
            _ = try await server.currentChallenge(now: self.now.addingTimeInterval(2))
        }
    }

    func testMismatchedSessionAndStaleChallengeDoNotConsumeAttemptBudget() async throws {
        let server = try makeServer(maximumCandidates: 2)
        let challenge = try await server.currentChallenge(now: now)
        let client = try makeClient(challenge: challenge)
        let otherSessionID = try RelaySessionIdentifier(rawValue: "11111111-2222-4333-8444-555555555555")
        let mismatchedRequest = try RelayPairingRequest(
            sessionID: otherSessionID,
            serverNonceCommitment: challenge.serverNonceCommitment,
            clientPublicKey: client.request.clientPublicKey,
            clientNonce: client.request.clientNonce
        )
        let staleCommitmentRequest = try RelayPairingRequest(
            sessionID: challenge.sessionID,
            serverNonceCommitment: Data(repeating: 0x99, count: 32),
            clientPublicKey: client.request.clientPublicKey,
            clientNonce: client.request.clientNonce
        )

        await assertRelayError(.invalidRequest) {
            _ = try await server.accept(mismatchedRequest, now: self.now)
        }
        await assertRelayError(.invalidRequest) {
            _ = try await server.accept(staleCommitmentRequest, now: self.now)
        }
        let firstOffer = try await server.accept(client.request, now: now)
        try await server.reject(candidateID: firstOffer.candidate.candidateID, now: now)
        let nextChallenge = try await server.currentChallenge(now: now)
        let nextClient = try makeClient(challenge: nextChallenge, clientPrivateKeyByte: 0x55, clientNonceByte: 0x66)
        _ = try await server.accept(nextClient.request, now: now)
    }

    func testPairingContextRejectsSecondSuccess() async throws {
        let server = try makeServer()
        let challenge = try await server.currentChallenge(now: now)
        let client = try makeClient(challenge: challenge)
        let offer = try await server.accept(client.request, now: now)
        let provisional = try client.complete(with: offer.candidate, now: now)
        try await server.approve(candidateID: offer.candidate.candidateID, now: now)
        _ = try await server.confirm(provisional.confirmation(decision: .codesMatch), now: now)

        await assertRelayError(.pairingAlreadyCompleted) {
            _ = try await server.accept(client.request, now: self.now)
        }
    }

    func testProtocolMessagesRoundTripCodable() async throws {
        let server = try makeServer()
        let challenge = try await server.currentChallenge(now: now)
        let client = try makeClient(challenge: challenge)
        let offer = try await server.accept(client.request, now: now)
        let provisional = try client.complete(with: offer.candidate, now: now)
        let clientConfirmation = try provisional.confirmation(decision: .codesMatch)
        let waiting = try await server.confirm(clientConfirmation, now: now)
        try await server.approve(candidateID: offer.candidate.candidateID, now: now)
        let established = try await server.confirm(clientConfirmation, now: now)
        let acceptance = try XCTUnwrap(established.response.acceptance)
        let clientSession = try provisional.complete(with: acceptance, now: now)
        let serverSession = try XCTUnwrap(established.session)
        let authenticatedRequest = try clientSession.signRequest(
            method: "GET",
            requestTarget: "/playlist.m3u8?edition=main",
            timestamp: now,
            nonce: "codable-nonce-0001",
            body: Data()
        )
        let authenticatedResponse = try serverSession.authenticateResponse(
            requestNonce: authenticatedRequest.nonce,
            statusCode: 200,
            body: Data("response".utf8)
        )

        XCTAssertEqual(try roundTrip(challenge), challenge)
        XCTAssertEqual(try roundTrip(client.request), client.request)
        XCTAssertEqual(try roundTrip(offer.candidate), offer.candidate)
        XCTAssertEqual(try roundTrip(clientConfirmation), clientConfirmation)
        XCTAssertEqual(try roundTrip(waiting.response), waiting.response)
        XCTAssertEqual(try roundTrip(acceptance), acceptance)
        XCTAssertEqual(try roundTrip(established.response), established.response)
        XCTAssertEqual(try roundTrip(authenticatedRequest), authenticatedRequest)
        XCTAssertEqual(try roundTrip(authenticatedResponse), authenticatedResponse)
    }

    func testMalformedCodablePayloadsAreRejected() async throws {
        let server = try makeServer()
        let challenge = try await server.currentChallenge(now: now)
        let client = try makeClient(challenge: challenge)
        let offer = try await server.accept(client.request, now: now)
        let provisional = try client.complete(with: offer.candidate, now: now)
        let confirmation = try provisional.confirmation(decision: .codesMatch)
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
                in: challenge,
                original: challenge.serverNonceCommitment.base64EncodedString(),
                replacement: Data(repeating: 1, count: 31).base64EncodedString()
            )
        ))
        XCTAssertThrowsError(try JSONDecoder().decode(
            RelayPairingRequest.self,
            from: replacingEncodedSubstring(
                in: client.request,
                original: client.request.serverNonceCommitment.base64EncodedString(),
                replacement: Data(repeating: 1, count: 31).base64EncodedString()
            )
        ))
        XCTAssertThrowsError(try JSONDecoder().decode(
            RelayPairingCandidate.self,
            from: replacingEncodedSubstring(
                in: offer.candidate,
                original: offer.candidate.serverNonce.base64EncodedString(),
                replacement: Data(repeating: 1, count: 31).base64EncodedString()
            )
        ))
        XCTAssertThrowsError(try JSONDecoder().decode(
            RelayPairingCandidateIdentifier.self,
            from: Data("{\"rawValue\":\"not-a-uuid\"}".utf8)
        ))
        XCTAssertThrowsError(try JSONDecoder().decode(
            RelayPairingConfirmation.self,
            from: replacingEncodedSubstring(
                in: confirmation,
                original: confirmation.clientConfirmationMAC!.base64EncodedString(),
                replacement: Data(repeating: 1, count: 31).base64EncodedString()
            )
        ))
        XCTAssertThrowsError(try JSONDecoder().decode(
            RelayPairingConfirmationResponse.self,
            from: Data("{\"candidateID\":{\"rawValue\":\"\(offer.candidate.candidateID.rawValue)\"},\"state\":\"established\"}".utf8)
        ))
        XCTAssertThrowsError(try JSONDecoder().decode(
            RelayAuthenticatedRequest.self,
            from: replacingEncodedSubstring(
                in: request,
                original: "\"method\":\"GET\"",
                replacement: "\"method\":\"\(String(repeating: "A", count: 33))\""
            )
        ))
    }

    func testCommitmentCandidateAndConfirmationTamperingAreRejected() async throws {
        let server = try makeServer()
        let challenge = try await server.currentChallenge(now: now)
        let client = try makeClient(challenge: challenge)
        let offer = try await server.accept(client.request, now: now)
        let tamperedNonceCandidate = try RelayPairingCandidate(
            candidateID: offer.candidate.candidateID,
            sessionID: offer.candidate.sessionID,
            serverNonce: Data(repeating: 0x99, count: 32),
            expiresAtUnixMilliseconds: offer.candidate.expiresAtUnixMilliseconds,
            serverProof: offer.candidate.serverProof
        )
        XCTAssertThrowsError(try client.complete(with: tamperedNonceCandidate, now: now)) {
            XCTAssertEqual($0 as? RelaySessionError, .nonceCommitmentMismatch)
        }

        var tamperedProof = offer.candidate.serverProof
        tamperedProof[0] ^= 0xFF
        let proofCandidate = try RelayPairingCandidate(
            candidateID: offer.candidate.candidateID,
            sessionID: offer.candidate.sessionID,
            serverNonce: offer.candidate.serverNonce,
            expiresAtUnixMilliseconds: offer.candidate.expiresAtUnixMilliseconds,
            serverProof: tamperedProof
        )
        XCTAssertThrowsError(try client.complete(with: proofCandidate, now: now)) {
            XCTAssertEqual($0 as? RelaySessionError, .candidateProofMismatch)
        }

        let provisional = try client.complete(with: offer.candidate, now: now)
        let reflectedConfirmation = try RelayPairingConfirmation(
            candidateID: offer.candidate.candidateID,
            decision: .codesMatch,
            clientConfirmationMAC: offer.candidate.serverProof
        )
        await assertRelayError(.clientConfirmationMismatch) {
            _ = try await server.confirm(reflectedConfirmation, now: self.now)
        }
        try await server.approve(candidateID: offer.candidate.candidateID, now: now)
        let established = try await server.confirm(provisional.confirmation(decision: .codesMatch), now: now)
        let acceptance = try XCTUnwrap(established.response.acceptance)
        var tamperedServerMAC = acceptance.serverConfirmationMAC
        tamperedServerMAC[0] ^= 0x80
        let tamperedAcceptance = try RelayPairingAcceptance(
            candidateID: acceptance.candidateID,
            sessionID: acceptance.sessionID,
            expiresAtUnixMilliseconds: acceptance.expiresAtUnixMilliseconds,
            serverConfirmationMAC: tamperedServerMAC
        )
        XCTAssertThrowsError(try provisional.complete(with: tamperedAcceptance, now: now)) {
            XCTAssertEqual($0 as? RelaySessionError, .serverConfirmationMismatch)
        }
    }

    func testExpiredChallengeAndCandidateFailClosed() async throws {
        let server = try makeServer(challengeTTL: 1, candidateTTL: 1)
        let challenge = try await server.currentChallenge(now: now)
        XCTAssertThrowsError(
            try RelayClientPairingAttempt(
                challenge: challenge,
                clientPrivateKeyData: Data(repeating: 0x33, count: 32),
                clientNonce: Data(repeating: 0x44, count: 32),
                now: now.addingTimeInterval(2)
            )
        ) {
            XCTAssertEqual($0 as? RelaySessionError, .expiredChallenge)
        }
        let client = try makeClient(challenge: challenge)
        await assertRelayError(.invalidRequest) {
            _ = try await server.accept(client.request, now: self.now.addingTimeInterval(2))
        }

        let freshChallenge = try await server.currentChallenge(now: now.addingTimeInterval(2))
        let freshClient = try makeClient(challenge: freshChallenge)
        let offer = try await server.accept(freshClient.request, now: now.addingTimeInterval(2))
        let provisional = try freshClient.complete(with: offer.candidate, now: now.addingTimeInterval(2))
        await assertRelayError(.confirmationExpired) {
            _ = try await server.confirm(
                provisional.confirmation(decision: .codesMatch),
                now: self.now.addingTimeInterval(4)
            )
        }
    }

    func testEstablishedSessionOutlivesPairingChallengeButStillExpires() async throws {
        let sessions = try await pairedSessions(challengeTTL: 1, candidateTTL: 1, sessionTTL: 60)
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
        )) {
            XCTAssertEqual($0 as? RelaySessionError, .requestExpired)
        }
    }

    func testAuthenticatedRequestTimestampCannotCrossSessionExpiry() async throws {
        let server = try makeServer(sessionTTL: 60)
        let challenge = try await server.currentChallenge(now: now)
        let client = try makeClient(challenge: challenge)
        let keyMaterial = try RelayCrypto.derivedKeyMaterial(
            sessionID: sessionID,
            ownPrivateKeyData: Data(repeating: 0x33, count: 32),
            peerPublicKeyData: challenge.serverPublicKey,
            transcript: RelayCanonical.pairingTranscript(
                challenge: challenge,
                request: client.request,
                serverNonce: Data(repeating: 0x22, count: 32)
            )
        )
        let offer = try await server.accept(client.request, now: now)
        let provisional = try client.complete(with: offer.candidate, now: now)
        try await server.approve(candidateID: offer.candidate.candidateID, now: now)
        let established = try await server.confirm(provisional.confirmation(decision: .codesMatch), now: now)
        let acceptance = try XCTUnwrap(established.response.acceptance)
        let serverSession = try XCTUnwrap(established.session)
        let timestamp = acceptance.expiresAtUnixMilliseconds + 1
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
            try await serverSession.verify(
                signedRequest,
                actualMethod: signedRequest.method,
                actualRequestTarget: signedRequest.requestTarget,
                body: Data(),
                now: RelayTime.date(fromUnixMilliseconds: acceptance.expiresAtUnixMilliseconds - 1),
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

    func testKeyScheduleSeparatesDirectionsCapabilitiesAndConfirmation() async throws {
        let server = try makeServer()
        let challenge = try await server.currentChallenge(now: now)
        let client = try makeClient(challenge: challenge)
        let transcript = RelayCanonical.pairingTranscript(
            challenge: challenge,
            request: client.request,
            serverNonce: Data(repeating: 0x22, count: 32)
        )
        let serverMaterial = try RelayCrypto.derivedKeyMaterial(
            sessionID: sessionID,
            ownPrivateKeyData: Data(repeating: 0x11, count: 32),
            peerPublicKeyData: client.request.clientPublicKey,
            transcript: transcript
        )
        let clientMaterial = try RelayCrypto.derivedKeyMaterial(
            sessionID: sessionID,
            ownPrivateKeyData: Data(repeating: 0x33, count: 32),
            peerPublicKeyData: challenge.serverPublicKey,
            transcript: transcript
        )

        XCTAssertEqual(serverMaterial.sessionIdentity, clientMaterial.sessionIdentity)
        XCTAssertEqual(serverMaterial.clientToServerRequestKey, clientMaterial.clientToServerRequestKey)
        XCTAssertEqual(serverMaterial.serverToClientRequestKey, clientMaterial.serverToClientRequestKey)
        XCTAssertEqual(serverMaterial.serverToClientResponseKey, clientMaterial.serverToClientResponseKey)
        XCTAssertEqual(serverMaterial.mediaCapability, clientMaterial.mediaCapability)
        XCTAssertEqual(serverMaterial.candidateProofKey, clientMaterial.candidateProofKey)
        XCTAssertEqual(serverMaterial.shortAuthenticationStringKey, clientMaterial.shortAuthenticationStringKey)
        XCTAssertEqual(serverMaterial.clientConfirmationKey, clientMaterial.clientConfirmationKey)
        XCTAssertEqual(serverMaterial.serverConfirmationKey, clientMaterial.serverConfirmationKey)
        XCTAssertEqual(Set([
            serverMaterial.sessionIdentity,
            serverMaterial.clientToServerRequestKey,
            serverMaterial.serverToClientRequestKey,
            serverMaterial.serverToClientResponseKey,
            serverMaterial.mediaCapability,
            serverMaterial.candidateProofKey,
            serverMaterial.shortAuthenticationStringKey,
            serverMaterial.clientConfirmationKey,
            serverMaterial.serverConfirmationKey,
        ]).count, 9)
    }

    func testAuthenticatedResponseBindsNonceStatusAndBody() async throws {
        let sessions = try await pairedSessions()
        let body = Data("authenticated response".utf8)
        let response = try sessions.server.authenticateResponse(
            requestNonce: "response-nonce-0001",
            statusCode: 200,
            body: body
        )

        try sessions.client.verifyResponse(
            response,
            requestNonce: "response-nonce-0001",
            actualStatusCode: 200,
            body: body,
            now: now
        )
        XCTAssertThrowsError(try sessions.client.verifyResponse(
            response,
            requestNonce: "response-nonce-0002",
            actualStatusCode: 200,
            body: body,
            now: now
        )) { error in
            XCTAssertEqual(error as? RelaySessionError, .invalidResponse)
        }
        XCTAssertThrowsError(try sessions.client.verifyResponse(
            response,
            requestNonce: "response-nonce-0001",
            actualStatusCode: 206,
            body: body,
            now: now
        )) { error in
            XCTAssertEqual(error as? RelaySessionError, .invalidResponse)
        }
        XCTAssertThrowsError(try sessions.client.verifyResponse(
            response,
            requestNonce: "response-nonce-0001",
            actualStatusCode: 200,
            body: Data("tampered response".utf8),
            now: now
        )) { error in
            XCTAssertEqual(error as? RelaySessionError, .responseBodyMismatch)
        }
    }

    func testAuthenticatedResponseRejectsTamperedReflectedAndWrongSessionProofs() async throws {
        let sessions = try await pairedSessions()
        let otherSessions = try await pairedSessions(
            sessionID: try RelaySessionIdentifier(rawValue: "11111111-2222-4333-8444-555555555555")
        )
        let body = Data("response".utf8)
        let valid = try sessions.server.authenticateResponse(
            requestNonce: "response-proof-0001",
            statusCode: 200,
            body: body
        )
        var tamperedSignature = valid.signature
        tamperedSignature[0] ^= 0x80
        let tampered = try RelayAuthenticatedResponse(
            sessionID: valid.sessionID,
            signerRole: valid.signerRole,
            requestNonce: valid.requestNonce,
            statusCode: valid.statusCode,
            bodySHA256: valid.bodySHA256,
            signature: tamperedSignature
        )
        let reflectedRequest = try sessions.server.signRequest(
            method: "GET",
            requestTarget: "/relay/v1/playlist.json",
            timestamp: now,
            nonce: valid.requestNonce,
            body: Data()
        )
        let reflected = try RelayAuthenticatedResponse(
            sessionID: valid.sessionID,
            signerRole: .server,
            requestNonce: valid.requestNonce,
            statusCode: valid.statusCode,
            bodySHA256: valid.bodySHA256,
            signature: reflectedRequest.signature
        )
        let wrongSession = try otherSessions.server.authenticateResponse(
            requestNonce: valid.requestNonce,
            statusCode: valid.statusCode,
            body: body
        )

        for rejected in [tampered, reflected] {
            XCTAssertThrowsError(try sessions.client.verifyResponse(
                rejected,
                requestNonce: valid.requestNonce,
                actualStatusCode: valid.statusCode,
                body: body,
                now: now
            )) { error in
                XCTAssertEqual(error as? RelaySessionError, .responseSignatureMismatch)
            }
        }
        XCTAssertThrowsError(try sessions.client.verifyResponse(
            wrongSession,
            requestNonce: valid.requestNonce,
            actualStatusCode: valid.statusCode,
            body: body,
            now: now
        )) { error in
            XCTAssertEqual(error as? RelaySessionError, .invalidResponse)
        }
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
        let challenge = try await server.currentChallenge(now: now)
        let client = try makeClient(challenge: challenge)
        let offer = try await server.accept(client.request, now: now)
        let provisional = try client.complete(with: offer.candidate, now: now)
        try await server.approve(candidateID: offer.candidate.candidateID, now: now)
        let established = try await server.confirm(provisional.confirmation(decision: .codesMatch), now: now)
        let clientSession = try provisional.complete(with: XCTUnwrap(established.response.acceptance), now: now)
        let serverSession = try XCTUnwrap(established.session)
        let request = try clientSession.signRequest(
            method: "GET",
            requestTarget: "/playlist.m3u8?edition=main&track=left",
            timestamp: now,
            nonce: "golden-nonce-0001",
            body: Data("golden-body".utf8)
        )
        let response = try serverSession.authenticateResponse(
            requestNonce: request.nonce,
            statusCode: 200,
            body: Data("golden-response".utf8)
        )

        let pairingDigest = RelayCrypto.sha256(
            RelayCanonical.pairingTranscript(
                challenge: challenge,
                request: client.request,
                serverNonce: offer.candidate.serverNonce
            )
        ).hexadecimalString
        let requestDigest = RelayCrypto.sha256(
            RelayCanonical.authenticatedRequestTranscript(request)
        ).hexadecimalString
        let responseDigest = RelayCrypto.sha256(
            RelayCanonical.authenticatedResponseTranscript(response)
        ).hexadecimalString

        XCTAssertEqual(pairingDigest, "5751e052a54c759660802d3c1aa82095b341ebf876e0ea47b58e4265907b9eff")
        XCTAssertEqual(requestDigest, "0a5824bf58e41acc26fc4c76a39266354920408fea426503ae5f53716986592b")
        XCTAssertEqual(responseDigest, "dadedddef62021aca21dd5d86ba64bbedd1dd12532131d05ff2204e63a75a408")
        XCTAssertEqual(response.signature.hexadecimalString, "5d7d52f3c63385a28185afab8e82818af4f6f25a4f5d86882e8195fa18fb9725")
        XCTAssertEqual(request.signature.hexadecimalString, "739dc7b99c7568a94f0a56b6cd1acfac78d5760e83e1a6445687915d93ad3b90")
        XCTAssertEqual(clientSession.sessionIdentity.value, "aKMZUQwUT0lGZyJmIOs8PWAir0mnqKBmmLfrEGxVu6I")
        XCTAssertEqual(clientSession.mediaCapability.value, "eoSTE-PkbmoZzpx8TTHs2FX9k8F-gcO2pQzTxo1_SBU")
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
        candidateTTL: TimeInterval = 60,
        sessionTTL: TimeInterval = 7_200,
        maximumCandidates: Int = 3
    ) throws -> RelayServerPairingContext {
        try RelayServerPairingContext(
            sessionID: sessionID ?? self.sessionID,
            serverPrivateKeyData: Data(repeating: 0x11, count: 32),
            serverNonce: Data(repeating: 0x22, count: 32),
            now: now,
            challengeTTL: challengeTTL,
            candidateTTL: candidateTTL,
            sessionTTL: sessionTTL,
            maximumCandidates: maximumCandidates
        )
    }

    private func makeClient(
        challenge: RelaySessionChallenge,
        clientPrivateKeyByte: UInt8 = 0x33,
        clientNonceByte: UInt8 = 0x44
    ) throws -> RelayClientPairingAttempt {
        try RelayClientPairingAttempt(
            challenge: challenge,
            clientPrivateKeyData: Data(repeating: clientPrivateKeyByte, count: 32),
            clientNonce: Data(repeating: clientNonceByte, count: 32),
            now: now
        )
    }

    private func pairedSessions(
        sessionID: RelaySessionIdentifier? = nil,
        challengeTTL: TimeInterval = 120,
        candidateTTL: TimeInterval = 60,
        sessionTTL: TimeInterval = 7_200
    ) async throws -> (client: RelayEstablishedSession, server: RelayEstablishedSession) {
        let server = try makeServer(
            sessionID: sessionID,
            challengeTTL: challengeTTL,
            candidateTTL: candidateTTL,
            sessionTTL: sessionTTL
        )
        let challenge = try await server.currentChallenge(now: now)
        let client = try makeClient(challenge: challenge)
        let offer = try await server.accept(client.request, now: now)
        let provisional = try client.complete(with: offer.candidate, now: now)
        try await server.approve(candidateID: offer.candidate.candidateID, now: now)
        let established = try await server.confirm(provisional.confirmation(decision: .codesMatch), now: now)
        return (
            try provisional.complete(with: XCTUnwrap(established.response.acceptance), now: now),
            try XCTUnwrap(established.session)
        )
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

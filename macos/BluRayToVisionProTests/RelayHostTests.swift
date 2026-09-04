import CryptoKit
import Foundation
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class RelayHostTests: XCTestCase {
    private let initialDate = Date(timeIntervalSince1970: 1_700_000_000)

    func testChallengeAndPairingUseExplicitEphemeralContext() async throws {
        let fixture = try await makeFixture()
        defer { removeFixture(fixture) }

        let challengeResponse = await fixture.connection.exchange(
            request(method: "GET", target: "/relay/v1/challenge")
        )
        XCTAssertEqual(challengeResponse.statusCode, 200)
        let challenge = try JSONDecoder().decode(ChallengeEnvelope.self, from: challengeResponse.body).challenge

        let client = try await pair(fixture)
        XCTAssertEqual(client.sessionID, challenge.sessionID)
        let lifecycle = await fixture.host.currentLifecycle()
        let advertisement = await fixture.host.advertisedBonjourService()
        XCTAssertEqual(lifecycle, .paired)
        XCTAssertNil(advertisement)
    }

    func testAuthenticatedRoutesBindRawMethodAndTargetAndRejectReplay() async throws {
        let fixture = try await makeFixture()
        defer { removeFixture(fixture) }
        let client = try await pair(fixture)

        let signedForSnapshot = try authenticatedRequest(
            session: client,
            method: "GET",
            target: "/relay/v1/playlist.json",
            nonce: "target-mismatch-0001"
        )
        let targetMismatch = await fixture.connection.exchange(
            request(method: "GET", target: "/relay/v1/playlist.m3u8", headers: signedForSnapshot.headers)
        )
        XCTAssertEqual(targetMismatch.statusCode, 401)

        let replayable = try authenticatedRequest(
            session: client,
            method: "GET",
            target: "/relay/v1/playlist.json",
            nonce: "replay-proof-000001"
        )
        let first = await fixture.connection.exchange(
            request(method: "GET", target: "/relay/v1/playlist.json", headers: replayable.headers)
        )
        let replay = await fixture.connection.exchange(
            request(method: "GET", target: "/relay/v1/playlist.json", headers: replayable.headers)
        )
        XCTAssertEqual(first.statusCode, 200)
        XCTAssertEqual(replay.statusCode, 409)
        try verifyResponse(first, for: replayable, using: client)
    }

    func testPairedHTTPResponsesAuthenticatePlaylistSnapshotBinaryMediaAndControls() async throws {
        let fixture = try await makeFixture()
        defer { removeFixture(fixture) }
        let mediaBody = Data([0x00, 0xFF, 0x10, 0x80, 0x42])
        try mediaBody.write(to: fixture.root.appendingPathComponent("segment.m4s"))
        let client = try await pair(fixture)

        let playlistRequest = try authenticatedRequest(
            session: client,
            method: "GET",
            target: RelayWireContract.playlistPath,
            nonce: "response-playlist-01"
        )
        let playlistResponse = await fixture.connection.exchange(
            request(method: "GET", target: RelayWireContract.playlistPath, headers: playlistRequest.headers)
        )
        try verifyResponse(playlistResponse, for: playlistRequest, using: client)

        let snapshotRequest = try authenticatedRequest(
            session: client,
            method: "GET",
            target: RelayWireContract.playlistSnapshotPath,
            nonce: "response-snapshot-01"
        )
        let snapshotResponse = await fixture.connection.exchange(
            request(method: "GET", target: RelayWireContract.playlistSnapshotPath, headers: snapshotRequest.headers)
        )
        try verifyResponse(snapshotResponse, for: snapshotRequest, using: client)

        let mediaRequest = try authenticatedRequest(
            session: client,
            method: "GET",
            target: "\(RelayWireContract.mediaPathPrefix)segment.m4s",
            nonce: "response-media-00001",
            mediaCapability: client.mediaCapability.value
        )
        let mediaResponse = await fixture.connection.exchange(
            request(
                method: "GET",
                target: "\(RelayWireContract.mediaPathPrefix)segment.m4s",
                headers: mediaRequest.headers
            )
        )
        XCTAssertEqual(mediaResponse.body, mediaBody)
        try verifyResponse(mediaResponse, for: mediaRequest, using: client)

        let finishRequest = try authenticatedRequest(
            session: client,
            method: "POST",
            target: RelayWireContract.finishPath,
            nonce: "response-finish-0001"
        )
        let finishResponse = await fixture.connection.exchange(
            request(method: "POST", target: RelayWireContract.finishPath, headers: finishRequest.headers)
        )
        XCTAssertEqual(finishResponse.statusCode, 204)
        try verifyResponse(finishResponse, for: finishRequest, using: client)

        let cancelRequest = try authenticatedRequest(
            session: client,
            method: "POST",
            target: RelayWireContract.cancelPath,
            nonce: "response-cancel-0001"
        )
        let cancelResponse = await fixture.connection.exchange(
            request(method: "POST", target: RelayWireContract.cancelPath, headers: cancelRequest.headers)
        )
        XCTAssertEqual(cancelResponse.statusCode, 204)
        try verifyResponse(cancelResponse, for: cancelRequest, using: client)
    }

    func testMediaRequiresCapabilityAndPreventsTraversalAndSymlinkEscape() async throws {
        let fixture = try await makeFixture()
        defer { removeFixture(fixture) }
        try Data("not exposed".utf8).write(to: fixture.root.deletingLastPathComponent().appendingPathComponent("escape.m4s"))
        try FileManager.default.createSymbolicLink(
            at: fixture.root.appendingPathComponent("safe/escaped.m4s"),
            withDestinationURL: fixture.root.deletingLastPathComponent().appendingPathComponent("escape.m4s")
        )
        let client = try await pair(fixture)

        let noCapability = try authenticatedRequest(
            session: client,
            method: "GET",
            target: "/relay/v1/media/safe/segment.m4s",
            nonce: "media-without-cap-1"
        )
        let denied = await fixture.connection.exchange(
            request(method: "GET", target: "/relay/v1/media/safe/segment.m4s", headers: noCapability.headers)
        )
        XCTAssertEqual(denied.statusCode, 403)
        try verifyResponse(denied, for: noCapability, using: client)

        let traversal = try authenticatedRequest(
            session: client,
            method: "GET",
            target: "/relay/v1/media/../escape.m4s",
            nonce: "media-traversal-001",
            mediaCapability: client.mediaCapability.value
        )
        let traversalResponse = await fixture.connection.exchange(
            request(method: "GET", target: "/relay/v1/media/../escape.m4s", headers: traversal.headers)
        )
        XCTAssertEqual(traversalResponse.statusCode, 400)
        try verifyResponse(traversalResponse, for: traversal, using: client)

        let symlink = try authenticatedRequest(
            session: client,
            method: "GET",
            target: "/relay/v1/media/safe/escaped.m4s",
            nonce: "media-symlink-escape",
            mediaCapability: client.mediaCapability.value
        )
        let symlinkResponse = await fixture.connection.exchange(
            request(method: "GET", target: "/relay/v1/media/safe/escaped.m4s", headers: symlink.headers)
        )
        XCTAssertEqual(symlinkResponse.statusCode, 404)
        try verifyResponse(symlinkResponse, for: symlink, using: client)

        let valid = try authenticatedRequest(
            session: client,
            method: "GET",
            target: "/relay/v1/media/safe/segment.m4s",
            nonce: "media-valid-resource-1",
            mediaCapability: client.mediaCapability.value
        )
        let served = await fixture.connection.exchange(
            request(method: "GET", target: "/relay/v1/media/safe/segment.m4s", headers: valid.headers)
        )
        XCTAssertEqual(served.statusCode, 200)
        XCTAssertEqual(served.body, Data("media fixture".utf8))
        try verifyResponse(served, for: valid, using: client)
    }

    func testMediaServingRejectsUnlistedRegularFixtureFile() async throws {
        let fixture = try await makeFixture()
        defer { removeFixture(fixture) }
        try Data("not in playlist".utf8).write(to: fixture.root.appendingPathComponent("unlisted.m4s"))
        let client = try await pair(fixture)

        let signedRequest = try authenticatedRequest(
            session: client,
            method: "GET",
            target: "\(RelayWireContract.mediaPathPrefix)unlisted.m4s",
            nonce: "unlisted-media-0001",
            mediaCapability: client.mediaCapability.value
        )
        let response = await fixture.connection.exchange(
            request(
                method: "GET",
                target: "\(RelayWireContract.mediaPathPrefix)unlisted.m4s",
                headers: signedRequest.headers
            )
        )

        XCTAssertEqual(response.statusCode, 404)
        try verifyResponse(response, for: signedRequest, using: client)
    }

    func testAppendedSegmentBecomesAnAllowedMediaResource() async throws {
        let fixture = try await makeFixture()
        defer { removeFixture(fixture) }
        try Data("live segment".utf8).write(to: fixture.root.appendingPathComponent("live.m4s"))
        let client = try await pair(fixture)
        _ = try await fixture.host.appendSegment(resourceIdentifier: "live.m4s", duration: 2)
        let signedRequest = try authenticatedRequest(
            session: client,
            method: "GET",
            target: "\(RelayWireContract.mediaPathPrefix)live.m4s",
            nonce: "appended-media-0001",
            mediaCapability: client.mediaCapability.value
        )

        let response = await fixture.connection.exchange(
            request(
                method: "GET",
                target: "\(RelayWireContract.mediaPathPrefix)live.m4s",
                headers: signedRequest.headers
            )
        )

        XCTAssertEqual(response.statusCode, 200)
        XCTAssertEqual(response.body, Data("live segment".utf8))
        try verifyResponse(response, for: signedRequest, using: client)
    }

    func testParserEnforcesHeaderAndBodyLimitsBeforeRoutes() async throws {
        let fixture = try await makeFixture()
        defer { removeFixture(fixture) }

        let oversizedHeader = request(
            method: "GET",
            target: "/relay/v1/challenge",
            headers: ["x-padding": String(repeating: "x", count: 17 * 1_024)]
        )
        let oversizedHeaderResponse = await fixture.connection.exchange(oversizedHeader)
        XCTAssertEqual(oversizedHeaderResponse.statusCode, 431)

        let oversizedBody = Data("POST /relay/v1/pairing HTTP/1.1\r\ncontent-length: 1048577\r\n\r\n".utf8)
        let oversizedBodyResponse = await fixture.connection.exchange(oversizedBody)
        XCTAssertEqual(oversizedBodyResponse.statusCode, 413)
    }

    func testSessionExpiryFinalizesAndRemovesAccess() async throws {
        let fixture = try await makeFixture(sessionTTL: 1)
        defer { removeFixture(fixture) }
        let client = try await pair(fixture)
        let signed = try authenticatedRequest(
            session: client,
            method: "GET",
            target: "/relay/v1/playlist.json",
            nonce: "expiry-check-000001"
        )
        fixture.clock.set(initialDate.addingTimeInterval(2))

        let response = await fixture.connection.exchange(
            request(method: "GET", target: "/relay/v1/playlist.json", headers: signed.headers)
        )
        XCTAssertEqual(response.statusCode, 410)
        let lifecycle = await fixture.host.currentLifecycle()
        XCTAssertEqual(lifecycle, .expired)
    }

    func testLifecyclePollingExpiresPendingCandidateAndRotatesChallenge() async throws {
        let fixture = try await makeFixture(challengeTTL: 30, candidateTTL: 1)
        defer { removeFixture(fixture) }
        let pending = try await beginPairing(fixture)
        fixture.clock.set(initialDate.addingTimeInterval(2))

        let lifecycle = await fixture.host.currentLifecycle()
        let currentCandidate = await fixture.host.currentPairingCandidate()
        let challengeResponse = await fixture.connection.exchange(
            request(method: "GET", target: RelayWireContract.challengePath)
        )
        let challenge = try JSONDecoder().decode(ChallengeEnvelope.self, from: challengeResponse.body).challenge

        XCTAssertEqual(lifecycle, .pairing)
        XCTAssertNil(currentCandidate)
        XCTAssertNotEqual(challenge.serverNonceCommitment, pending.challenge.serverNonceCommitment)
    }

    func testLifecyclePollingExpiresQuietPairingSession() async throws {
        let fixture = try await makeFixture(pairingSessionTTL: 1)
        defer { removeFixture(fixture) }
        fixture.clock.set(initialDate.addingTimeInterval(2))

        let lifecycle = await fixture.host.currentLifecycle()
        let advertisement = await fixture.host.advertisedBonjourService()

        XCTAssertEqual(lifecycle, .expired)
        XCTAssertNil(advertisement)
    }

    func testFinalRejectedPairingCandidateExpiresHostImmediately() async throws {
        let fixture = try await makeFixture(maximumCandidates: 2)
        defer { removeFixture(fixture) }
        let first = try await beginPairing(fixture)
        try await fixture.host.rejectPairingCandidate(first.candidate.candidateID)
        let second = try await beginPairing(fixture)
        try await fixture.host.rejectPairingCandidate(second.candidate.candidateID)
        let exhaustedResponse = await fixture.connection.exchange(
            request(method: "POST", target: RelayWireContract.pairingPath, body: try JSONEncoder().encode(second.attempt.request))
        )

        XCTAssertEqual(exhaustedResponse.statusCode, 409)
        let lifecycle = await fixture.host.currentLifecycle()
        XCTAssertEqual(lifecycle, .expired)
    }

    func testConfirmationRouteRejectsNonlocalTamperedAndReplayRequests() async throws {
        let fixture = try await makeFixture()
        defer { removeFixture(fixture) }
        let pending = try await beginPairing(fixture)
        let confirmation = try pending.provisional.confirmation(decision: .codesMatch)
        let body = try JSONEncoder().encode(confirmation)
        let signed = try authenticatedRequest(
            session: pending.provisional.authenticationSession,
            method: "POST",
            target: RelayWireContract.pairingConfirmPath,
            nonce: "confirm-route-0001",
            body: body
        )
        let encodedRequest = request(
            method: "POST",
            target: RelayWireContract.pairingConfirmPath,
            headers: signed.headers,
            body: body
        )

        let nonlocal = await fixture.host.handle(encodedRequest, peer: .nonLocal)
        XCTAssertEqual(nonlocal.statusCode, 403)
        let tampered = await fixture.connection.exchange(request(
            method: "POST",
            target: RelayWireContract.pairingConfirmPath,
            headers: signed.headers,
            body: Data("tampered".utf8)
        ))
        XCTAssertEqual(tampered.statusCode, 401)
        let accepted = await fixture.connection.exchange(encodedRequest)
        XCTAssertEqual(accepted.statusCode, 202)
        try verifyResponse(accepted, for: signed, using: pending.provisional.authenticationSession)
        let replay = await fixture.connection.exchange(encodedRequest)
        XCTAssertEqual(replay.statusCode, 409)
    }

    func testProtectedRoutesRemainUnavailableUntilBothDevicesConfirm() async throws {
        let fixture = try await makeFixture()
        defer { removeFixture(fixture) }
        let pending = try await beginPairing(fixture)
        let playlistRequest = try authenticatedRequest(
            session: pending.provisional.authenticationSession,
            method: "GET",
            target: RelayWireContract.playlistPath,
            nonce: "provisional-media-01",
            mediaCapability: pending.provisional.authenticationSession.mediaCapability.value
        )
        let beforeConfirmation = await fixture.connection.exchange(request(
            method: "GET",
            target: RelayWireContract.playlistPath,
            headers: playlistRequest.headers
        ))
        XCTAssertEqual(beforeConfirmation.statusCode, 503)

        let firstConfirmation = try await confirm(pending, fixture: fixture, nonce: "confirm-media-0001")
        XCTAssertEqual(firstConfirmation.response.statusCode, 202)
        try await fixture.host.approvePairingCandidate(pending.candidate.candidateID)
        let established = try await confirm(pending, fixture: fixture, nonce: "confirm-media-0002")
        XCTAssertEqual(established.response.statusCode, 200)
        XCTAssertNotNil(established.session)
    }

    func testRetainedEventPlaylistSnapshotKeepsOnlyWindow() async throws {
        let fixture = try await makeFixture(retainedSegmentLimit: 2)
        defer { removeFixture(fixture) }
        let client = try await pair(fixture)

        let signedSnapshot = try authenticatedRequest(
            session: client,
            method: "GET",
            target: "/relay/v1/playlist.json",
            nonce: "playlist-snapshot-01"
        )
        let snapshotResponse = await fixture.connection.exchange(
            request(method: "GET", target: "/relay/v1/playlist.json", headers: signedSnapshot.headers)
        )
        XCTAssertEqual(snapshotResponse.statusCode, 200)
        let snapshot = try JSONDecoder().decode(RelayPlaylistSnapshot.self, from: snapshotResponse.body)
        XCTAssertEqual(snapshot.earliestPlayableTimeMilliseconds, 6_000)
        XCTAssertEqual(snapshot.totalDurationMilliseconds, 10_000)
        XCTAssertEqual(snapshot.segments.map(\.resourceIdentifier), ["second.m4s", "third.m4s"])

        let signedPlaylist = try authenticatedRequest(
            session: client,
            method: "GET",
            target: "/relay/v1/playlist.m3u8",
            nonce: "playlist-rendering-01"
        )
        let playlistResponse = await fixture.connection.exchange(
            request(method: "GET", target: "/relay/v1/playlist.m3u8", headers: signedPlaylist.headers)
        )
        let playlist = String(decoding: playlistResponse.body, as: UTF8.self)
        XCTAssertTrue(playlist.contains("#EXT-X-PLAYLIST-TYPE:EVENT"))
        XCTAssertTrue(playlist.contains("#EXT-X-MEDIA-SEQUENCE:3"))
        XCTAssertTrue(playlist.contains("#EXT-X-MAP:URI=\"/relay/v1/media/init.mp4\""))
        XCTAssertFalse(playlist.contains("first.m4s"))
    }

    func testBonjourAdvertisementUsesDNSServiceDiscoveryTXTEncoding() throws {
        let sessionID = try RelaySessionIdentifier(rawValue: "A9B8C7D6-E5F4-4321-ABCD-1234567890AB")
        let advertisement = RelayBonjourAdvertisement(sessionID: sessionID)
        let values = NetService.dictionary(fromTXTRecord: advertisement.txtRecord)

        XCTAssertEqual(advertisement.serviceType, RelayWireContract.bonjourServiceType)
        XCTAssertEqual(values["v"].map { String(decoding: $0, as: UTF8.self) }, "3")
        XCTAssertEqual(values["sid"].map { String(decoding: $0, as: UTF8.self) }, String(sessionID.rawValue.prefix(8)))
    }

    func testCleanupIsIdempotentForCancelNetworkLossAndAppQuit() async throws {
        let fixture = try await makeFixture()
        defer { removeFixture(fixture) }
        _ = try await pair(fixture)

        await fixture.host.cancel()
        await fixture.host.cancel()
        let cancelledLifecycle = await fixture.host.currentLifecycle()
        XCTAssertEqual(cancelledLifecycle, .cancelled)

        let secondFixture = try await makeFixture()
        defer { removeFixture(secondFixture) }
        await secondFixture.host.networkLost()
        await secondFixture.host.stopForAppQuit()
        await secondFixture.host.stopForAppQuit()
        let stoppedLifecycle = await secondFixture.host.currentLifecycle()
        XCTAssertEqual(stoppedLifecycle, .stopped)
    }

    func testRejectsLoopbackAndNonlocalInMemoryConnections() async throws {
        let fixture = try await makeFixture()
        defer { removeFixture(fixture) }
        let raw = request(method: "GET", target: "/relay/v1/challenge")
        let loopbackResponse = await fixture.host.handle(raw, peer: .loopback)
        let nonlocalResponse = await fixture.host.handle(raw, peer: .nonLocal)
        XCTAssertEqual(loopbackResponse.statusCode, 403)
        XCTAssertEqual(nonlocalResponse.statusCode, 403)
        XCTAssertEqual(RelayNetworkPeerClassifier.classify(address: "127.0.0.1"), .loopback)
        XCTAssertEqual(RelayNetworkPeerClassifier.classify(address: "8.8.8.8"), .nonLocal)
    }

    private func makeFixture(
        challengeTTL: TimeInterval = 30,
        candidateTTL: TimeInterval = 60,
        pairingSessionTTL: TimeInterval = 600,
        sessionTTL: TimeInterval = 120,
        retainedSegmentLimit: Int = 3,
        maximumCandidates: Int = 3
    ) async throws -> Fixture {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let mediaDirectory = root.appendingPathComponent("safe", isDirectory: true)
        try FileManager.default.createDirectory(at: mediaDirectory, withIntermediateDirectories: true)
        try Data("init".utf8).write(to: root.appendingPathComponent("init.mp4"))
        try Data("segment".utf8).write(to: root.appendingPathComponent("segment.m4s"))
        try Data("media fixture".utf8).write(to: mediaDirectory.appendingPathComponent("segment.m4s"))
        for name in ["first.m4s", "second.m4s", "third.m4s"] {
            try Data(name.utf8).write(to: root.appendingPathComponent(name))
        }
        try """
        #EXTM3U
        #EXT-X-VERSION:7
        #EXT-X-PLAYLIST-TYPE:EVENT
        #EXT-X-TARGETDURATION:2
        #EXT-X-MAP:URI="init.mp4"
        #EXTINF:2,
        segment.m4s
        #EXTINF:2,
        safe/segment.m4s
        #EXTINF:2,
        first.m4s
        #EXTINF:2,
        second.m4s
        #EXTINF:2,
        third.m4s
        """.write(to: root.appendingPathComponent("media.m3u8"), atomically: true, encoding: .utf8)
        let eventFixture = try RelayEventHLSFixture.load(directory: root)
        let sessionID = try RelaySessionIdentifier(rawValue: "A9B8C7D6-E5F4-4321-ABCD-1234567890AB")
        let clock = RelayTestClock(initialDate)
        let context = try RelayServerPairingContext(
            sessionID: sessionID,
            now: initialDate,
            challengeTTL: challengeTTL,
            candidateTTL: candidateTTL,
            sessionTTL: sessionTTL,
            maximumCandidates: maximumCandidates
        )
        let host = try RelayHost(
            pairingContext: context,
            configuration: try RelayHostConfiguration(
                fixtureDirectory: root,
                retainedSegmentLimit: retainedSegmentLimit
            ),
            fixture: eventFixture,
            pairingSessionTTL: pairingSessionTTL,
            now: { clock.now() }
        )
        return Fixture(
            host: host,
            connection: RelayInMemoryConnection(host: host),
            root: root,
            clock: clock
        )
    }

    private func pair(_ fixture: Fixture) async throws -> RelayEstablishedSession {
        let pending = try await beginPairing(fixture)
        try await fixture.host.approvePairingCandidate(pending.candidate.candidateID)
        let established = try await confirm(pending, fixture: fixture, nonce: "pairing-confirm-0001")
        return try XCTUnwrap(established.session)
    }

    private func beginPairing(_ fixture: Fixture) async throws -> PendingPairing {
        let challengeResponse = await fixture.connection.exchange(
            request(method: "GET", target: RelayWireContract.challengePath)
        )
        XCTAssertEqual(challengeResponse.statusCode, 200)
        let challenge = try JSONDecoder().decode(ChallengeEnvelope.self, from: challengeResponse.body).challenge
        let privateKey = Curve25519.KeyAgreement.PrivateKey().rawRepresentation
        let attempt = try RelayClientPairingAttempt(
            challenge: challenge,
            clientPrivateKeyData: privateKey,
            clientNonce: Data(repeating: 7, count: 32),
            now: fixture.clock.now()
        )
        let body = try JSONEncoder().encode(attempt.request)
        let response = await fixture.connection.exchange(request(method: "POST", target: "/relay/v1/pairing", body: body))
        XCTAssertEqual(response.statusCode, 201)
        let candidate = try JSONDecoder().decode(RelayPairingCandidateEnvelope.self, from: response.body).candidate
        return PendingPairing(
            challenge: challenge,
            attempt: attempt,
            candidate: candidate,
            provisional: try attempt.complete(with: candidate, now: fixture.clock.now())
        )
    }

    private func confirm(
        _ pending: PendingPairing,
        fixture: Fixture,
        nonce: String
    ) async throws -> ConfirmationExchange {
        let confirmation = try pending.provisional.confirmation(decision: .codesMatch)
        let body = try JSONEncoder().encode(confirmation)
        let signed = try authenticatedRequest(
            session: pending.provisional.authenticationSession,
            method: "POST",
            target: RelayWireContract.pairingConfirmPath,
            nonce: nonce,
            body: body
        )
        let response = await fixture.connection.exchange(request(
            method: "POST",
            target: RelayWireContract.pairingConfirmPath,
            headers: signed.headers,
            body: body
        ))
        try verifyResponse(response, for: signed, using: pending.provisional.authenticationSession)
        let result = try JSONDecoder().decode(RelayPairingConfirmationEnvelope.self, from: response.body).confirmation
        let session = try result.acceptance.map {
            try pending.provisional.complete(with: $0, now: fixture.clock.now())
        }
        return ConfirmationExchange(response: response, session: session)
    }

    private func authenticatedRequest(
        session: RelayEstablishedSession,
        method: String,
        target: String,
        nonce: String,
        body: Data = Data(),
        mediaCapability: String? = nil
    ) throws -> AuthenticatedRequest {
        let authentication = try session.signRequest(
            method: method,
            requestTarget: target,
            timestamp: initialDate,
            nonce: nonce,
            body: body
        )
        var headers = [
            "x-bdtoavp-relay-auth": try JSONEncoder().encode(authentication).base64EncodedString(),
        ]
        if let mediaCapability {
            headers["x-bdtoavp-relay-media-capability"] = mediaCapability
        }
        return AuthenticatedRequest(headers: headers, authentication: authentication)
    }

    private func verifyResponse(
        _ response: RelayHTTPResponse,
        for request: AuthenticatedRequest,
        using client: RelayEstablishedSession
    ) throws {
        let encoded = try XCTUnwrap(response.headers[RelayWireContract.responseAuthenticationHeader])
        XCTAssertLessThanOrEqual(encoded.utf8.count, RelayWireContract.maximumAuthenticationHeaderBytes)
        let authenticationData = try XCTUnwrap(Data(base64Encoded: encoded))
        let authentication = try JSONDecoder().decode(RelayAuthenticatedResponse.self, from: authenticationData)
        try client.verifyResponse(
            authentication,
            requestNonce: request.authentication.nonce,
            actualStatusCode: response.statusCode,
            body: response.body,
            now: initialDate
        )
    }

    private func request(method: String, target: String, headers: [String: String] = [:], body: Data = Data()) -> Data {
        var renderedHeaders = headers
        if !body.isEmpty {
            renderedHeaders["content-length"] = String(body.count)
        }
        var requestData = Data("\(method) \(target) HTTP/1.1\r\n".utf8)
        for (name, value) in renderedHeaders.sorted(by: { $0.key < $1.key }) {
            requestData.append(Data("\(name): \(value)\r\n".utf8))
        }
        requestData.append(Data("\r\n".utf8))
        requestData.append(body)
        return requestData
    }

    private func removeFixture(_ fixture: Fixture) {
        try? FileManager.default.removeItem(at: fixture.root)
    }
}

private struct ChallengeEnvelope: Decodable {
    let challenge: RelaySessionChallenge
}

private struct AuthenticatedRequest {
    let headers: [String: String]
    let authentication: RelayAuthenticatedRequest
}

private struct Fixture {
    let host: RelayHost
    let connection: RelayInMemoryConnection
    let root: URL
    let clock: RelayTestClock
}

private struct PendingPairing {
    let challenge: RelaySessionChallenge
    let attempt: RelayClientPairingAttempt
    let candidate: RelayPairingCandidate
    let provisional: RelayProvisionalSession
}

private struct ConfirmationExchange {
    let response: RelayHTTPResponse
    let session: RelayEstablishedSession?
}

private final class RelayInMemoryConnection: @unchecked Sendable {
    private let host: RelayHost

    init(host: RelayHost) {
        self.host = host
    }

    func exchange(_ request: Data) async -> RelayHTTPResponse {
        await host.handle(request, peer: .localNetwork)
    }
}

private final class RelayTestClock: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Date

    init(_ value: Date) {
        self.value = value
    }

    func now() -> Date {
        lock.withLock { value }
    }

    func set(_ value: Date) {
        lock.withLock {
            self.value = value
        }
    }
}

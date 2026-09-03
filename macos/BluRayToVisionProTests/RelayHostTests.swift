import CryptoKit
import Foundation
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class RelayHostTests: XCTestCase {
    private let initialDate = Date(timeIntervalSince1970: 1_700_000_000)

    func testChallengeAndPairingUseExplicitEphemeralContext() async throws {
        let fixture = try makeFixture()
        defer { removeFixture(fixture) }

        let challengeResponse = await fixture.connection.exchange(
            request(method: "GET", target: "/relay/v1/challenge")
        )
        XCTAssertEqual(challengeResponse.statusCode, 200)
        let challenge = try JSONDecoder().decode(ChallengeEnvelope.self, from: challengeResponse.body).challenge
        XCTAssertEqual(challenge, fixture.challenge)

        let client = try await pair(fixture)
        XCTAssertEqual(client.sessionID, fixture.challenge.sessionID)
        let lifecycle = await fixture.host.currentLifecycle()
        let advertisement = await fixture.host.advertisedBonjourService()
        XCTAssertEqual(lifecycle, .paired)
        XCTAssertNil(advertisement)
    }

    func testAuthenticatedRoutesBindRawMethodAndTargetAndRejectReplay() async throws {
        let fixture = try makeFixture()
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
        let fixture = try makeFixture()
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
        let fixture = try makeFixture()
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
        let fixture = try makeFixture()
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

    func testParserEnforcesHeaderAndBodyLimitsBeforeRoutes() async throws {
        let fixture = try makeFixture()
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
        let fixture = try makeFixture(sessionTTL: 1)
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

    func testLifecyclePollingExpiresQuietPairingSession() async throws {
        let fixture = try makeFixture(challengeTTL: 1)
        defer { removeFixture(fixture) }
        fixture.clock.set(initialDate.addingTimeInterval(2))

        let lifecycle = await fixture.host.currentLifecycle()

        XCTAssertEqual(lifecycle, .expired)
        let pairingCode = await fixture.host.pairingCode()
        XCTAssertNil(pairingCode)
    }

    func testFinalWrongPairingAttemptExpiresHostImmediately() async throws {
        let fixture = try makeFixture(maximumFailedAttempts: 2)
        defer { removeFixture(fixture) }
        let wrongCode = try RelayPairingCode("RSTU-VWXY-2345-6789")
        let firstAttempt = try RelayClientPairingAttempt(
            challenge: fixture.challenge,
            pairingCode: wrongCode,
            now: fixture.clock.now()
        )
        let firstResponse = await fixture.connection.exchange(
            request(
                method: "POST",
                target: RelayWireContract.pairingPath,
                body: try JSONEncoder().encode(firstAttempt.request)
            )
        )
        let finalAttempt = try RelayClientPairingAttempt(
            challenge: fixture.challenge,
            pairingCode: wrongCode,
            now: fixture.clock.now()
        )
        let finalResponse = await fixture.connection.exchange(
            request(
                method: "POST",
                target: RelayWireContract.pairingPath,
                body: try JSONEncoder().encode(finalAttempt.request)
            )
        )

        XCTAssertEqual(firstResponse.statusCode, 401)
        XCTAssertEqual(finalResponse.statusCode, 409)
        let lifecycle = await fixture.host.currentLifecycle()
        XCTAssertEqual(lifecycle, .expired)
    }

    func testRetainedEventPlaylistSnapshotKeepsOnlyWindow() async throws {
        let fixture = try makeFixture(retainedSegmentLimit: 2)
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
        XCTAssertEqual(values["v"].map { String(decoding: $0, as: UTF8.self) }, "2")
        XCTAssertEqual(values["sid"].map { String(decoding: $0, as: UTF8.self) }, String(sessionID.rawValue.prefix(8)))
    }

    func testCleanupIsIdempotentForCancelNetworkLossAndAppQuit() async throws {
        let fixture = try makeFixture()
        defer { removeFixture(fixture) }
        _ = try await pair(fixture)

        await fixture.host.cancel()
        await fixture.host.cancel()
        let cancelledLifecycle = await fixture.host.currentLifecycle()
        XCTAssertEqual(cancelledLifecycle, .cancelled)

        let secondFixture = try makeFixture()
        defer { removeFixture(secondFixture) }
        await secondFixture.host.networkLost()
        await secondFixture.host.stopForAppQuit()
        await secondFixture.host.stopForAppQuit()
        let stoppedLifecycle = await secondFixture.host.currentLifecycle()
        XCTAssertEqual(stoppedLifecycle, .stopped)
    }

    func testRejectsLoopbackAndNonlocalInMemoryConnections() async throws {
        let fixture = try makeFixture()
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
        sessionTTL: TimeInterval = 120,
        retainedSegmentLimit: Int = 3,
        maximumFailedAttempts: Int = 5
    ) throws -> Fixture {
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
        let pairingCode = try RelayPairingCode("2345-6789-ABCD-EFGH")
        let sessionID = try RelaySessionIdentifier(rawValue: "A9B8C7D6-E5F4-4321-ABCD-1234567890AB")
        let clock = RelayTestClock(initialDate)
        let context = try RelayServerPairingContext(
            sessionID: sessionID,
            pairingCode: pairingCode,
            now: initialDate,
            challengeTTL: challengeTTL,
            sessionTTL: sessionTTL,
            maximumFailedAttempts: maximumFailedAttempts
        )
        let host = try RelayHost(
            pairingContext: context,
            configuration: try RelayHostConfiguration(
                fixtureDirectory: root,
                retainedSegmentLimit: retainedSegmentLimit
            ),
            fixture: eventFixture,
            now: { clock.now() }
        )
        return Fixture(
            host: host,
            connection: RelayInMemoryConnection(host: host),
            root: root,
            clock: clock,
            challenge: context.challenge,
            pairingCode: pairingCode
        )
    }

    private func pair(_ fixture: Fixture) async throws -> RelayEstablishedSession {
        let privateKey = Curve25519.KeyAgreement.PrivateKey().rawRepresentation
        let attempt = try RelayClientPairingAttempt(
            challenge: fixture.challenge,
            pairingCode: fixture.pairingCode,
            clientPrivateKeyData: privateKey,
            clientNonce: Data(repeating: 7, count: 32),
            now: fixture.clock.now()
        )
        let body = try JSONEncoder().encode(attempt.request)
        let response = await fixture.connection.exchange(request(method: "POST", target: "/relay/v1/pairing", body: body))
        XCTAssertEqual(response.statusCode, 201)
        let acceptance = try JSONDecoder().decode(PairingEnvelope.self, from: response.body).acceptance
        return try attempt.complete(with: acceptance, now: fixture.clock.now())
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
        try? FileManager.default.removeItem(at: fixture.root.deletingLastPathComponent())
    }
}

private struct ChallengeEnvelope: Decodable {
    let challenge: RelaySessionChallenge
}

private struct PairingEnvelope: Decodable {
    let acceptance: RelayPairingAcceptance
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
    let challenge: RelaySessionChallenge
    let pairingCode: RelayPairingCode
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

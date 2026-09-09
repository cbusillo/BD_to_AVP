import Foundation
import XCTest
@testable import BDToAVPPlayer

func makePairedSessions(now: Date) async throws -> (client: RelayEstablishedSession, server: RelayEstablishedSession) {
    let server = try RelayServerPairingContext(now: now)
    let challenge = try await server.currentChallenge(now: now)
    let attempt = try RelayClientPairingAttempt(challenge: challenge, now: now)
    let offer = try await server.accept(attempt.request, now: now)
    let provisional = try attempt.complete(with: offer.candidate, now: now)
    try await server.approve(candidateID: offer.candidate.candidateID, now: now)
    let established = try await server.confirm(provisional.confirmation(decision: .codesMatch), now: now)
    return (
        try provisional.complete(with: XCTUnwrap(established.response.acceptance), now: now),
        try XCTUnwrap(established.session)
    )
}

func makePairedClientSession(now: Date) async throws -> RelayEstablishedSession {
    try await makePairedSessions(now: now).client
}

final class RelayHLSResourceLoaderTests: XCTestCase {
    private let baseURL = URL(string: "http://relay.local:7431")!
    private let now = Date(timeIntervalSince1970: 1_700_000_000)

    func testResourceResolverEnforcesSameOriginAndHostResourceIdentifiers() {
        let valid = URL(string: "bdtoavprelay://relay.local:7431/relay/v1/media/init/init.mp4")!
        XCTAssertEqual(
            RelayHLSResourceLoader.resolveURL(valid, serverBaseURL: baseURL)?.absoluteString,
            "http://relay.local:7431/relay/v1/media/init/init.mp4"
        )
        XCTAssertNotNil(RelayHLSResourceLoader.resolveURL(
            URL(string: "bdtoavprelay://relay.local:7431/relay/v1/playlist.m3u8")!,
            serverBaseURL: baseURL
        ))
        XCTAssertNil(RelayHLSResourceLoader.resolveURL(
            URL(string: "bdtoavprelay://attacker.local:7431/relay/v1/media/init.mp4")!,
            serverBaseURL: baseURL
        ))
        XCTAssertNil(RelayHLSResourceLoader.resolveURL(
            URL(string: "bdtoavprelay://relay.local:7431/relay/v1/media/../secret.mp4")!,
            serverBaseURL: baseURL
        ))
        XCTAssertNil(RelayHLSResourceLoader.resolveURL(
            URL(string: "bdtoavprelay://relay.local:7431/relay/v1/control/cancel")!,
            serverBaseURL: baseURL
        ))
    }

    func testRequestedDataHonorsCurrentOffsetAndRemainingRequestedLength() throws {
        let data = Data("0123456789".utf8)

        let result = try RelayHLSResourceLoader.requestedData(
            from: data,
            requestedOffset: 2,
            currentOffset: 4,
            requestedLength: 5
        )

        XCTAssertEqual(result, Data("456".utf8))
    }

    func testRequestedDataClampsAtResourceEndAndRejectsInvalidRanges() throws {
        let data = Data("0123456789".utf8)

        XCTAssertEqual(
            try RelayHLSResourceLoader.requestedData(
                from: data,
                requestedOffset: 8,
                currentOffset: 8,
                requestedLength: 3
            ),
            Data("89".utf8)
        )
        XCTAssertEqual(
            try RelayHLSResourceLoader.requestedData(
                from: data,
                requestedOffset: 3,
                currentOffset: 5,
                requestedLength: 1,
                requestsAllDataToEndOfResource: true
            ),
            Data("56789".utf8)
        )
        XCTAssertThrowsError(
            try RelayHLSResourceLoader.requestedData(
                from: data,
                requestedOffset: 4,
                currentOffset: 3,
                requestedLength: 1
            )
        ) {
            XCTAssertEqual($0 as? RelayResourceLoadingError, .invalidDataRequest)
        }
        XCTAssertThrowsError(
            try RelayHLSResourceLoader.requestedData(
                from: data,
                requestedOffset: 11,
                currentOffset: 11,
                requestedLength: 1
            )
        ) {
            XCTAssertEqual($0 as? RelayResourceLoadingError, .invalidDataRequest)
        }
    }

    func testActiveTaskRegistryDoesNotLeakWhenCompletionWinsRegistrationRace() {
        let registry = RelayActiveTaskRegistry()
        let request = NSObject()
        let key = ObjectIdentifier(request)
        let registration = registry.register(key)

        registry.complete(key, registration: registration)
        registry.install(Task {}, for: key, registration: registration)

        XCTAssertEqual(registry.activeTaskCount, 0)
    }

    func testEachTransientRetryUsesFreshSignedHostHeaders() async throws {
        let fixedNow = now
        let sessions = try await makePairedSessions(now: fixedNow)
        let session = sessions.client
        let transport = FakeRelayTransport()
        await transport.setHandler { request in
            let requests = await transport.allRequests()
            if requests.count == 1 { throw URLError(.timedOut) }
            let body = Data("segment".utf8)
            return (
                body,
                try makeAuthenticatedHTTPResponse(
                    request,
                    body: body,
                    serverSession: sessions.server,
                    contentType: "video/iso.segment"
                )
            )
        }
        let nonceSequence = TestNonceSequence()
        let client = RelayAuthenticatedResourceClient(
            signer: session,
            transport: transport,
            serverBaseURL: baseURL,
            clock: { fixedNow },
            nonce: { nonceSequence.next() }
        )

        let (data, _) = try await client.load(
            URL(string: "bdtoavprelay://relay.local:7431/relay/v1/media/segment-1.m4s")!
        )
        XCTAssertEqual(data, Data("segment".utf8))
        let requests = await transport.allRequests()
        XCTAssertEqual(requests.count, 2)
        let authentications = try requests.map { request -> RelayAuthenticatedRequest in
            let encoded = try XCTUnwrap(request.value(forHTTPHeaderField: RelayWireContract.authenticationHeader))
            return try JSONDecoder().decode(RelayAuthenticatedRequest.self, from: XCTUnwrap(Data(base64Encoded: encoded)))
        }
        XCTAssertEqual(authentications.map(\.requestTarget), ["/relay/v1/media/segment-1.m4s", "/relay/v1/media/segment-1.m4s"])
        XCTAssertNotEqual(authentications[0].nonce, authentications[1].nonce)
        XCTAssertTrue(requests.allSatisfy {
            $0.value(forHTTPHeaderField: RelayWireContract.mediaCapabilityHeader) == session.mediaCapability.value
        })
    }

    func testExpiredAndUnpairedResourceResponsesAreTyped() async throws {
        let fixedNow = now
        let sessions = try await makePairedSessions(now: fixedNow)
        let session = sessions.client
        let resourceURL = URL(string: "bdtoavprelay://relay.local:7431/relay/v1/media/init.mp4")!
        let expiredTransport = FakeRelayTransport()
        await expiredTransport.setHandler { request in
            (
                Data(),
                try makeAuthenticatedHTTPResponse(
                    request,
                    body: Data(),
                    serverSession: sessions.server,
                    statusCode: 410
                )
            )
        }
        let expiredClient = RelayAuthenticatedResourceClient(signer: session, transport: expiredTransport, serverBaseURL: baseURL, clock: { fixedNow })
        do {
            _ = try await expiredClient.load(resourceURL)
            XCTFail("Expected expired session")
        } catch {
            XCTAssertEqual(error as? RelayTransportError, .sessionExpired)
        }

        let unpairedTransport = FakeRelayTransport()
        await unpairedTransport.setHandler { request in
            (
                Data(),
                try makeAuthenticatedHTTPResponse(
                    request,
                    body: Data(),
                    serverSession: sessions.server,
                    statusCode: 503
                )
            )
        }
        let unpairedClient = RelayAuthenticatedResourceClient(signer: session, transport: unpairedTransport, serverBaseURL: baseURL, clock: { fixedNow })
        do {
            _ = try await unpairedClient.load(resourceURL)
            XCTFail("Expected unpaired host")
        } catch {
            XCTAssertEqual(error as? RelayTransportError, .unpaired)
        }
    }

    func testResourceClientRejectsTamperedResponseBindingsBeforeAcceptingData() async throws {
        let fixedNow = now
        let sessions = try await makePairedSessions(now: fixedNow)
        let resourceURL = URL(string: "bdtoavprelay://relay.local:7431/relay/v1/media/init.mp4")!

        let tamperedBodyTransport = FakeRelayTransport()
        await tamperedBodyTransport.setHandler { request in
            let authenticatedBody = Data("authentic".utf8)
            let deliveredBody = Data("tampered".utf8)
            return (
                deliveredBody,
                try makeAuthenticatedHTTPResponse(
                    request,
                    body: deliveredBody,
                    serverSession: sessions.server,
                    authenticatedBody: authenticatedBody
                )
            )
        }
        let tamperedBodyClient = RelayAuthenticatedResourceClient(
            signer: sessions.client,
            transport: tamperedBodyTransport,
            serverBaseURL: baseURL,
            clock: { fixedNow },
            maximumTransientRetries: 0
        )
        do {
            _ = try await tamperedBodyClient.load(resourceURL)
            XCTFail("Expected tampered body rejection")
        } catch {
            XCTAssertEqual(error as? RelaySessionError, .responseBodyMismatch)
        }

        let statusMismatchTransport = FakeRelayTransport()
        await statusMismatchTransport.setHandler { request in
            (
                Data(),
                try makeAuthenticatedHTTPResponse(
                    request,
                    body: Data(),
                    serverSession: sessions.server,
                    statusCode: 503,
                    authenticatedStatusCode: 200
                )
            )
        }
        let statusMismatchClient = RelayAuthenticatedResourceClient(
            signer: sessions.client,
            transport: statusMismatchTransport,
            serverBaseURL: baseURL,
            clock: { fixedNow },
            maximumTransientRetries: 0
        )
        do {
            _ = try await statusMismatchClient.load(resourceURL)
            XCTFail("Expected status mismatch rejection")
        } catch {
            XCTAssertEqual(error as? RelaySessionError, .invalidResponse)
        }

        let nonceMismatchTransport = FakeRelayTransport()
        await nonceMismatchTransport.setHandler { request in
            (
                Data(),
                try makeAuthenticatedHTTPResponse(
                    request,
                    body: Data(),
                    serverSession: sessions.server,
                    authenticatedRequestNonce: "wrong-response-nonce"
                )
            )
        }
        let nonceMismatchClient = RelayAuthenticatedResourceClient(
            signer: sessions.client,
            transport: nonceMismatchTransport,
            serverBaseURL: baseURL,
            clock: { fixedNow },
            maximumTransientRetries: 0
        )
        do {
            _ = try await nonceMismatchClient.load(resourceURL)
            XCTFail("Expected request nonce mismatch rejection")
        } catch {
            XCTAssertEqual(error as? RelaySessionError, .invalidResponse)
        }
    }
}

private final class TestNonceSequence: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0

    func next() -> String {
        lock.withLock {
            value += 1
            return String(repeating: "a", count: 31) + String(value)
        }
    }
}

final class RelayLoopbackHTTPServerTests: XCTestCase {
    private let playlist = "#EXTM3U\n#EXT-X-MAP:URI=\"/relay/v1/media/init.mp4\"\n#EXTINF:2.000,\n/relay/v1/media/segment-000.m4s\n#EXT-X-ENDLIST\n"

    func testPlaylistRewritingKeepsEveryMediaURLBehindLocalCapability() throws {
        let data = try RelayLoopbackHTTPServer.rewritePlaylist(Data(playlist.utf8), prefix: "/capability")
        let text = String(decoding: data, as: UTF8.self)
        XCTAssertTrue(text.contains("URI=\"/capability/relay/v1/media/init.mp4\""))
        XCTAssertTrue(text.contains("\n/capability/relay/v1/media/segment-000.m4s\n"))
        for replacement in ["https://other.test/movie.m4s", "/relay/v1/control/cancel", "/relay/v1/media/%2e%2e/private"] {
            let invalid = playlist.replacingOccurrences(of: "/relay/v1/media/segment-000.m4s", with: replacement)
            XCTAssertThrowsError(try RelayLoopbackHTTPServer.rewritePlaylist(Data(invalid.utf8), prefix: "/capability"))
        }
        XCTAssertThrowsError(try RelayLoopbackHTTPServer.rewritePlaylist(Data((playlist + "#EXT-X-KEY:METHOD=AES-128,URI=\"https://other.test/key\"\n").utf8), prefix: "/capability"))
    }

    func testRangesIncludeClosedOpenAndSuffixAndRejectInvalidRequests() throws {
        let data = Data("0123456789".utf8)
        for (range, expected) in [("bytes=2-4", "234"), ("bytes=8-", "89"), ("bytes=-3", "789"), ("bytes=8-99", "89")] {
            let response = try RelayLoopbackHTTPServer.mediaResponse(data: data, contentType: "video/mp4", range: range)
            XCTAssertEqual(response.statusCode, 206)
            XCTAssertEqual(response.body, Data(expected.utf8))
            XCTAssertNotNil(response.headers["content-range"])
        }
        for range in ["bytes=10-", "bytes=5-2", "bytes=0-1,3-4", "bytes=-0", "bytes=+1-2", "bytes=0-999999999999999999999"] {
            let response = try RelayLoopbackHTTPServer.mediaResponse(data: data, contentType: "video/mp4", range: range)
            XCTAssertEqual(response.statusCode, 416, range)
            XCTAssertTrue(response.body.isEmpty)
        }
    }

    func testLoopbackServesSignedMediaAndRejectsMissingCapabilityAndControlRoutes() async throws {
        let now = Date()
        let sessions = try await makePairedSessions(now: now)
        let transport = FakeRelayTransport()
        let playlistData = Data(playlist.utf8)
        await transport.setHandler { request in
            let body = request.url?.path == RelayWireContract.playlistPath ? playlistData : Data("0123456789".utf8)
            return (body, try makeAuthenticatedHTTPResponse(request, body: body, serverSession: sessions.server))
        }
        var source = try RelayRemotePlaybackSource(session: sessions.client, serverBaseURL: URL(string: "http://relay.local:7431")!)
        let (asset, _) = try await source.makeAssetAndLoader(transport: transport)
        defer { source.cancelLoader() }
        let urlSession = URLSession(configuration: .ephemeral)
        defer { urlSession.invalidateAndCancel() }
        let (data, response) = try await urlSession.data(from: asset.url)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        let localPrefix = "/" + asset.url.pathComponents[1]
        XCTAssertTrue(String(decoding: data, as: UTF8.self).contains(localPrefix + RelayWireContract.mediaPathPrefix))
        let segmentURL = asset.url.deletingLastPathComponent().appendingPathComponent("media/segment-000.m4s")
        var request = URLRequest(url: segmentURL)
        request.setValue("bytes=2-4", forHTTPHeaderField: "Range")
        let (segment, segmentResponse) = try await urlSession.data(for: request)
        XCTAssertEqual(segment, Data("234".utf8))
        XCTAssertEqual((segmentResponse as? HTTPURLResponse)?.statusCode, 206)
        let requests = await transport.allRequests()
        XCTAssertEqual(requests.count, 2)
        XCTAssertTrue(requests.allSatisfy { $0.value(forHTTPHeaderField: RelayWireContract.authenticationHeader) != nil })
        XCTAssertTrue(requests.allSatisfy { $0.value(forHTTPHeaderField: RelayWireContract.mediaCapabilityHeader) != nil })
        for path in [RelayWireContract.playlistPath, localPrefix + RelayWireContract.cancelPath, localPrefix + RelayWireContract.playlistSnapshotPath] {
            var components = URLComponents(url: asset.url, resolvingAgainstBaseURL: false)!
            components.path = path
            let (_, denied) = try await urlSession.data(from: components.url!)
            XCTAssertEqual((denied as? HTTPURLResponse)?.statusCode, 404)
        }
        let finalRequests = await transport.allRequests()
        XCTAssertEqual(finalRequests.count, 2)
    }

    func testLoopbackNeverServesTamperedUpstreamBytes() async throws {
        let sessions = try await makePairedSessions(now: Date())
        let transport = FakeRelayTransport()
        await transport.setHandler { request in
            let data = Data("tampered".utf8)
            return (data, try makeAuthenticatedHTTPResponse(request, body: data, serverSession: sessions.server, authenticatedBody: Data("original".utf8)))
        }
        var source = try RelayRemotePlaybackSource(session: sessions.client, serverBaseURL: URL(string: "http://relay.local:7431")!)
        let (asset, _) = try await source.makeAssetAndLoader(transport: transport)
        defer { source.cancelLoader() }
        let (data, response) = try await URLSession.shared.data(from: asset.url)
        XCTAssertTrue(data.isEmpty)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 503)
    }

    func testCancellationClosesListener() async throws {
        let (server, url) = try await RelayLoopbackHTTPServer.start(serverBaseURL: URL(string: "http://relay.local")!) { _ in
            throw URLError(.cancelled)
        }
        server.cancelAllRequests()
        // Cancellation is queued before any subsequent accepted connection.
        do {
            _ = try await URLSession.shared.data(from: url)
            XCTFail("A cancelled listener must not serve requests")
        } catch { }
    }
}

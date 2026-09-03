import Foundation
import XCTest
@testable import BDToAVPPlayer

func makePairedSessions(now: Date) async throws -> (client: RelayEstablishedSession, server: RelayEstablishedSession) {
    let code = RelayPairingCode.random()
    let server = try RelayServerPairingContext(pairingCode: code, now: now)
    let attempt = try RelayClientPairingAttempt(challenge: server.challenge, pairingCode: code, now: now)
    let result = try await server.accept(attempt.request, now: now)
    return (try attempt.complete(with: result.acceptance, now: now), result.session)
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

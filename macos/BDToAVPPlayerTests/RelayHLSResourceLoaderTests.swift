import Foundation
import XCTest
@testable import BDToAVPPlayer

func makePairedClientSession(now: Date) async throws -> RelayEstablishedSession {
    let code = RelayPairingCode.random()
    let server = try RelayServerPairingContext(pairingCode: code, now: now)
    let attempt = try RelayClientPairingAttempt(challenge: server.challenge, pairingCode: code, now: now)
    let result = try await server.accept(attempt.request, now: now)
    return try attempt.complete(with: result.acceptance, now: now)
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

    func testEachTransientRetryUsesFreshSignedHostHeaders() async throws {
        let fixedNow = now
        let session = try await makePairedClientSession(now: fixedNow)
        let transport = FakeRelayTransport()
        await transport.setHandler { request in
            let requests = await transport.allRequests()
            if requests.count == 1 { throw URLError(.timedOut) }
            return (Data("segment".utf8), makeHTTPResponse(request, contentType: "video/iso.segment"))
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
        let session = try await makePairedClientSession(now: fixedNow)
        let resourceURL = URL(string: "bdtoavprelay://relay.local:7431/relay/v1/media/init.mp4")!
        let expiredTransport = FakeRelayTransport()
        await expiredTransport.setHandler { request in (Data(), makeHTTPResponse(request, statusCode: 410)) }
        let expiredClient = RelayAuthenticatedResourceClient(signer: session, transport: expiredTransport, serverBaseURL: baseURL, clock: { fixedNow })
        do {
            _ = try await expiredClient.load(resourceURL)
            XCTFail("Expected expired session")
        } catch {
            XCTAssertEqual(error as? RelayTransportError, .sessionExpired)
        }

        let unpairedTransport = FakeRelayTransport()
        await unpairedTransport.setHandler { request in (Data(), makeHTTPResponse(request, statusCode: 503)) }
        let unpairedClient = RelayAuthenticatedResourceClient(signer: session, transport: unpairedTransport, serverBaseURL: baseURL, clock: { fixedNow })
        do {
            _ = try await unpairedClient.load(resourceURL)
            XCTFail("Expected unpaired host")
        } catch {
            XCTAssertEqual(error as? RelayTransportError, .unpaired)
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

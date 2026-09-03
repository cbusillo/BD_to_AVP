import Foundation
import XCTest
@testable import BDToAVPPlayer

actor FakeRelayTransport: RelayTransport {
    typealias Handler = @Sendable (URLRequest) async throws -> (Data, HTTPURLResponse)

    private var handler: Handler?
    private var requests: [URLRequest] = []

    func setHandler(_ handler: @escaping Handler) {
        self.handler = handler
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        requests.append(request)
        guard let handler else { throw URLError(.badServerResponse) }
        return try await handler(request)
    }

    func allRequests() -> [URLRequest] { requests }
}

final class FakeRelayBrowser: RelayEndpointBrowsing, @unchecked Sendable {
    let discoveryStream: AsyncStream<[RelayDiscoveredEndpoint]>
    private let continuation: AsyncStream<[RelayDiscoveredEndpoint]>.Continuation
    private(set) var startCount = 0
    private(set) var stopCount = 0

    init() {
        var streamContinuation: AsyncStream<[RelayDiscoveredEndpoint]>.Continuation!
        discoveryStream = AsyncStream { streamContinuation = $0 }
        continuation = streamContinuation
    }

    func startBrowsing() { startCount += 1 }
    func stopBrowsing() { stopCount += 1; continuation.finish() }
    func emit(_ endpoints: [RelayDiscoveredEndpoint]) { continuation.yield(endpoints) }
}

func makeTestEndpoint(
    id: String = "Vision-Pro",
    baseURL: URL = URL(string: "http://relay.local:7431")!
) -> RelayDiscoveredEndpoint {
    RelayDiscoveredEndpoint(id: id, displayName: id, baseURL: baseURL)
}

func makeHTTPResponse(_ request: URLRequest, statusCode: Int = 200, contentType: String = "application/json") -> HTTPURLResponse {
    HTTPURLResponse(
        url: request.url!,
        statusCode: statusCode,
        httpVersion: "HTTP/1.1",
        headerFields: ["content-type": contentType]
    )!
}

private struct ChallengeEnvelope: Encodable { let challenge: RelaySessionChallenge }
private struct PairingEnvelope: Encodable { let acceptance: RelayPairingAcceptance }

@MainActor
final class RelaySessionCoordinatorTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_700_000_000)
    private let pairingCode = try! RelayPairingCode("2345-6789-ABCD-EFGH")

    private func makeCoordinator(
        browser: FakeRelayBrowser,
        transport: FakeRelayTransport,
        now: @escaping @Sendable () -> Date
    ) -> RelaySessionCoordinator {
        RelaySessionCoordinator(browserFactory: { browser }, transport: transport, clock: now)
    }

    func testDiscoveryPublishesResolvedEndpoints() async {
        let browser = FakeRelayBrowser()
        let coordinator = makeCoordinator(browser: browser, transport: FakeRelayTransport(), now: { self.now })
        coordinator.startDiscovery()
        browser.emit([makeTestEndpoint()])
        for _ in 0 ..< 5 { await Task.yield() }

        XCTAssertEqual(coordinator.state, .discovery)
        XCTAssertEqual(coordinator.discoveredServers, [makeTestEndpoint()])
        XCTAssertEqual(browser.startCount, 1)
    }

    func testPairingUsesHostWireRoutesEnvelopeAndCreatedResponse() async throws {
        let browser = FakeRelayBrowser()
        let transport = FakeRelayTransport()
        let server = try RelayServerPairingContext(pairingCode: pairingCode, now: now)
        await transport.setHandler { [server, now] request in
            switch request.url?.path {
            case RelayWireContract.challengePath:
                XCTAssertEqual(request.httpMethod, "GET")
                return (try JSONEncoder().encode(ChallengeEnvelope(challenge: server.challenge)), makeHTTPResponse(request))
            case RelayWireContract.pairingPath:
                XCTAssertEqual(request.httpMethod, "POST")
                XCTAssertEqual(request.value(forHTTPHeaderField: "content-type"), RelayWireContract.jsonContentType)
                guard let body = request.httpBody else { throw URLError(.badServerResponse) }
                let pairingRequest = try JSONDecoder().decode(RelayPairingRequest.self, from: body)
                let accepted = try await server.accept(pairingRequest, now: now)
                return (try JSONEncoder().encode(PairingEnvelope(acceptance: accepted.acceptance)), makeHTTPResponse(request, statusCode: 201))
            default:
                throw URLError(.badURL)
            }
        }
        let coordinator = makeCoordinator(browser: browser, transport: transport, now: { self.now })
        coordinator.startDiscovery()
        await coordinator.connect(to: makeTestEndpoint())
        await coordinator.submitPairingCode(pairingCode.formattedValue)

        guard case .connected = coordinator.state else {
            return XCTFail("Expected a paired relay session, got \(coordinator.state)")
        }
        let paths = await transport.allRequests().compactMap(\.url?.path)
        XCTAssertEqual(paths, [RelayWireContract.challengePath, RelayWireContract.pairingPath])
    }

    func testWrongPairingCodeKeepsUnexpiredChallengeRetryable() async throws {
        let browser = FakeRelayBrowser()
        let transport = FakeRelayTransport()
        let server = try RelayServerPairingContext(pairingCode: pairingCode, now: now)
        await transport.setHandler { [server, now] request in
            switch request.url?.path {
            case RelayWireContract.challengePath:
                return (try JSONEncoder().encode(ChallengeEnvelope(challenge: server.challenge)), makeHTTPResponse(request))
            case RelayWireContract.pairingPath:
                let requestBody = try JSONDecoder().decode(RelayPairingRequest.self, from: request.httpBody!)
                do {
                    let accepted = try await server.accept(requestBody, now: now)
                    return (try JSONEncoder().encode(PairingEnvelope(acceptance: accepted.acceptance)), makeHTTPResponse(request, statusCode: 201))
                } catch RelaySessionError.pairingProofMismatch {
                    return (Data(), makeHTTPResponse(request, statusCode: 401))
                }
            default:
                throw URLError(.badURL)
            }
        }
        let coordinator = makeCoordinator(browser: browser, transport: transport, now: { self.now })
        coordinator.startDiscovery()
        await coordinator.connect(to: makeTestEndpoint())
        await coordinator.submitPairingCode("2345-6789-ABCD-EFGK")

        XCTAssertEqual(coordinator.state, .pairing(serverID: "Vision-Pro", expiresAt: server.challenge.expirationDate))
        XCTAssertEqual(coordinator.pairingErrorMessage, "That pairing code did not match. Try again.")

        await coordinator.submitPairingCode(pairingCode.formattedValue)

        guard case .connected = coordinator.state else {
            return XCTFail("Expected retry with the existing challenge to pair, got \(coordinator.state)")
        }
        XCTAssertNil(coordinator.pairingErrorMessage)
        let requestPaths = await transport.allRequests().compactMap(\.url?.path)
        XCTAssertEqual(
            requestPaths,
            [RelayWireContract.challengePath, RelayWireContract.pairingPath, RelayWireContract.pairingPath]
        )
    }

    func testPairingConflictEndsTheCurrentChallenge() async {
        let browser = FakeRelayBrowser()
        let transport = FakeRelayTransport()
        let server = try! RelayServerPairingContext(pairingCode: pairingCode, now: now)
        await transport.setHandler { request in
            switch request.url?.path {
            case RelayWireContract.challengePath:
                return (try! JSONEncoder().encode(ChallengeEnvelope(challenge: server.challenge)), makeHTTPResponse(request))
            case RelayWireContract.pairingPath:
                return (Data(), makeHTTPResponse(request, statusCode: 409))
            default:
                throw URLError(.badURL)
            }
        }
        let coordinator = makeCoordinator(browser: browser, transport: transport, now: { self.now })
        coordinator.startDiscovery()
        await coordinator.connect(to: makeTestEndpoint())
        await coordinator.submitPairingCode(pairingCode.formattedValue)

        XCTAssertEqual(coordinator.state, .sessionExpired)
        XCTAssertNil(coordinator.pairingErrorMessage)
    }

    func testExpiredChallengeAndUnpairedHostAreMappedWithoutRetainingSession() async throws {
        let browser = FakeRelayBrowser()
        let expiredTransport = FakeRelayTransport()
        await expiredTransport.setHandler { request in
            (Data(), makeHTTPResponse(request, statusCode: 410))
        }
        let expired = makeCoordinator(browser: browser, transport: expiredTransport, now: { self.now })
        expired.startDiscovery()
        await expired.connect(to: makeTestEndpoint())
        XCTAssertEqual(expired.state, .sessionExpired)
        XCTAssertNil(expired.session)

        let unpairedTransport = FakeRelayTransport()
        await unpairedTransport.setHandler { request in
            (Data(), makeHTTPResponse(request, statusCode: 503))
        }
        let unpaired = makeCoordinator(browser: FakeRelayBrowser(), transport: unpairedTransport, now: { self.now })
        unpaired.startDiscovery()
        await unpaired.connect(to: makeTestEndpoint())
        if case .failed = unpaired.state {} else {
            XCTFail("An unpaired relay must not transition to connected")
        }
        XCTAssertNil(unpaired.session)
    }

    func testReconnectUsesSameSessionAndFreshSignedProbe() async throws {
        let browser = FakeRelayBrowser()
        let transport = FakeRelayTransport()
        let server = try RelayServerPairingContext(pairingCode: pairingCode, now: now)
        await transport.setHandler { [server, now] request in
            switch request.url?.path {
            case RelayWireContract.challengePath:
                return (try JSONEncoder().encode(ChallengeEnvelope(challenge: server.challenge)), makeHTTPResponse(request))
            case RelayWireContract.pairingPath:
                let accepted = try await server.accept(try JSONDecoder().decode(RelayPairingRequest.self, from: request.httpBody!), now: now)
                return (try JSONEncoder().encode(PairingEnvelope(acceptance: accepted.acceptance)), makeHTTPResponse(request, statusCode: 201))
            case RelayWireContract.playlistSnapshotPath:
                XCTAssertNotNil(request.value(forHTTPHeaderField: RelayWireContract.authenticationHeader))
                XCTAssertNotNil(request.value(forHTTPHeaderField: RelayWireContract.mediaCapabilityHeader))
                return (Data("{}".utf8), makeHTTPResponse(request))
            default:
                throw URLError(.badURL)
            }
        }
        let coordinator = makeCoordinator(browser: browser, transport: transport, now: { self.now })
        coordinator.startDiscovery()
        await coordinator.connect(to: makeTestEndpoint())
        await coordinator.submitPairingCode(pairingCode.formattedValue)
        let sessionID = coordinator.session?.sessionID

        coordinator.handleNetworkAvailability(.unavailable)
        XCTAssertEqual(coordinator.state, .networkUnavailable)
        coordinator.handleNetworkAvailability(.available)
        try await Task.sleep(nanoseconds: 100_000_000)

        guard case let .connected(reconnectedID, _) = coordinator.state else {
            return XCTFail("Expected reconnect, got \(coordinator.state)")
        }
        XCTAssertEqual(reconnectedID, sessionID?.rawValue)
    }

    func testCleanupIsIdempotent() {
        let browser = FakeRelayBrowser()
        let coordinator = makeCoordinator(browser: browser, transport: FakeRelayTransport(), now: { self.now })
        coordinator.startDiscovery()
        coordinator.disconnect()
        coordinator.disconnect()
        XCTAssertEqual(coordinator.state, .idle)
        XCTAssertNil(coordinator.session)
        XCTAssertEqual(browser.stopCount, 1)
    }
}

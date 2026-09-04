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

func makeAuthenticatedHTTPResponse(
    _ request: URLRequest,
    body: Data,
    serverSession: RelayEstablishedSession,
    statusCode: Int = 200,
    contentType: String = "application/json",
    authenticatedRequestNonce: String? = nil,
    authenticatedStatusCode: Int? = nil,
    authenticatedBody: Data? = nil
) throws -> HTTPURLResponse {
    let requestNonce: String
    if let authenticatedRequestNonce {
        requestNonce = authenticatedRequestNonce
    } else {
        let encodedRequest = try XCTUnwrap(
            request.value(forHTTPHeaderField: RelayWireContract.authenticationHeader)
        )
        let requestData = try XCTUnwrap(Data(base64Encoded: encodedRequest))
        requestNonce = try JSONDecoder().decode(RelayAuthenticatedRequest.self, from: requestData).nonce
    }
    let authentication = try serverSession.authenticateResponse(
        requestNonce: requestNonce,
        statusCode: authenticatedStatusCode ?? statusCode,
        body: authenticatedBody ?? body
    )
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    return HTTPURLResponse(
        url: request.url!,
        statusCode: statusCode,
        httpVersion: "HTTP/1.1",
        headerFields: [
            "content-type": contentType,
            RelayWireContract.responseAuthenticationHeader: try encoder.encode(authentication).base64EncodedString(),
        ]
    )!
}

private actor RelayCoordinatorTestServer {
    private let pairingContext: RelayServerPairingContext
    private let clock: @Sendable () -> Date
    private var provisionalSession: RelayEstablishedSession?
    private var establishedSession: RelayEstablishedSession?

    init(now: Date, clock: @escaping @Sendable () -> Date) throws {
        pairingContext = try RelayServerPairingContext(now: now)
        self.clock = clock
    }

    func handle(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        switch request.url?.path {
        case RelayWireContract.challengePath:
            let challenge = try await pairingContext.currentChallenge(now: clock())
            let body = try JSONEncoder().encode(RelayChallengeEnvelope(challenge: challenge))
            return (body, makeHTTPResponse(request))
        case RelayWireContract.pairingPath:
            let pairingRequest = try JSONDecoder().decode(
                RelayPairingRequest.self,
                from: try XCTUnwrap(request.httpBody)
            )
            let offered = try await pairingContext.accept(pairingRequest, now: clock())
            provisionalSession = offered.provisionalSession
            let body = try JSONEncoder().encode(RelayPairingCandidateEnvelope(candidate: offered.candidate))
            return (body, makeHTTPResponse(request, statusCode: 201))
        case RelayWireContract.pairingConfirmPath:
            let session = try XCTUnwrap(provisionalSession)
            let confirmation = try JSONDecoder().decode(
                RelayPairingConfirmation.self,
                from: try XCTUnwrap(request.httpBody)
            )
            let result = try await pairingContext.confirm(confirmation, now: clock())
            let body = try JSONEncoder().encode(RelayPairingConfirmationEnvelope(confirmation: result.response))
            let statusCode = result.response.state == .waitingForMac ? 202 : 200
            let response = try makeAuthenticatedHTTPResponse(
                request,
                body: body,
                serverSession: session,
                statusCode: statusCode
            )
            if let established = result.session {
                establishedSession = established
            }
            if result.response.state == .rejected {
                provisionalSession = nil
            }
            return (body, response)
        case RelayWireContract.playlistSnapshotPath:
            let session = try XCTUnwrap(establishedSession)
            let body = Data("{}".utf8)
            return (
                body,
                try makeAuthenticatedHTTPResponse(request, body: body, serverSession: session)
            )
        default:
            throw URLError(.badURL)
        }
    }

    func approvePending() async throws {
        let pendingCandidate = try await pairingContext.pendingCandidateSummary(now: clock())
        let candidate = try XCTUnwrap(pendingCandidate)
        try await pairingContext.approve(candidateID: candidate.candidateID, now: clock())
    }
}

private final class RelayCoordinatorTestClock: @unchecked Sendable {
    private let lock = NSLock()
    private var date: Date

    init(_ date: Date) {
        self.date = date
    }

    func now() -> Date {
        lock.withLock { date }
    }

    func set(_ date: Date) {
        lock.withLock {
            self.date = date
        }
    }
}

@MainActor
final class RelaySessionCoordinatorTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_700_000_000)

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

    func testConnectAndMacFirstConfirmationUseProtocolV3Routes() async throws {
        let browser = FakeRelayBrowser()
        let transport = FakeRelayTransport()
        let server = try RelayCoordinatorTestServer(now: now, clock: { self.now })
        await transport.setHandler { request in try await server.handle(request) }
        let coordinator = makeCoordinator(browser: browser, transport: transport, now: { self.now })
        coordinator.startDiscovery()

        await coordinator.connect(to: makeTestEndpoint())

        guard case .confirming = coordinator.state else {
            return XCTFail("Expected numeric comparison, got \(coordinator.state)")
        }
        let sas = try XCTUnwrap(coordinator.shortAuthenticationString)
        XCTAssertEqual(sas.digits.count, 6)
        try await server.approvePending()
        await coordinator.confirmCodesMatch()

        guard case .connected = coordinator.state else {
            return XCTFail("Expected a paired relay session, got \(coordinator.state)")
        }
        let paths = await transport.allRequests().compactMap(\.url?.path)
        XCTAssertEqual(paths, [
            RelayWireContract.challengePath,
            RelayWireContract.pairingPath,
            RelayWireContract.pairingConfirmPath,
        ])
    }

    func testClientFirstConfirmationWaitsForMacThenPollingConnects() async throws {
        let browser = FakeRelayBrowser()
        let transport = FakeRelayTransport()
        let server = try RelayCoordinatorTestServer(now: now, clock: { self.now })
        await transport.setHandler { request in try await server.handle(request) }
        let coordinator = makeCoordinator(browser: browser, transport: transport, now: { self.now })
        coordinator.startDiscovery()
        await coordinator.connect(to: makeTestEndpoint())

        await coordinator.confirmCodesMatch()
        guard case .confirming = coordinator.state else {
            return XCTFail("Expected to wait for Mac confirmation")
        }
        XCTAssertTrue(coordinator.isWaitingForMacConfirmation)
        try await server.approvePending()
        let didConnect = await waitUntil {
            if case .connected = coordinator.state { return true }
            return false
        }
        XCTAssertTrue(didConnect)
        XCTAssertFalse(coordinator.isWaitingForMacConfirmation)
    }

    func testRejectReturnsToDiscoveryAndClearsPendingSelection() async throws {
        let browser = FakeRelayBrowser()
        let transport = FakeRelayTransport()
        let server = try RelayCoordinatorTestServer(now: now, clock: { self.now })
        await transport.setHandler { request in try await server.handle(request) }
        let coordinator = makeCoordinator(browser: browser, transport: transport, now: { self.now })
        coordinator.startDiscovery()
        await coordinator.connect(to: makeTestEndpoint())

        await coordinator.rejectCandidate()

        XCTAssertEqual(coordinator.state, .discovery)
        XCTAssertNil(coordinator.shortAuthenticationString)
        XCTAssertNil(coordinator.connectedServer)
    }

    func testExpiredCandidateFailsClosedWithoutSendingConfirmation() async throws {
        let clock = RelayCoordinatorTestClock(now)
        let browser = FakeRelayBrowser()
        let transport = FakeRelayTransport()
        let server = try RelayCoordinatorTestServer(now: now, clock: { clock.now() })
        await transport.setHandler { request in try await server.handle(request) }
        let coordinator = makeCoordinator(browser: browser, transport: transport, now: { clock.now() })
        coordinator.startDiscovery()
        await coordinator.connect(to: makeTestEndpoint())
        clock.set(now.addingTimeInterval(61))

        await coordinator.confirmCodesMatch()

        XCTAssertEqual(coordinator.state, .sessionExpired)
        XCTAssertNil(coordinator.session)
        let paths = await transport.allRequests().compactMap(\.url?.path)
        XCTAssertEqual(paths, [RelayWireContract.challengePath, RelayWireContract.pairingPath])
    }

    func testConflictExpiredAndUnpairedHostsDoNotRetainSession() async throws {
        let pairingContext = try RelayServerPairingContext(now: now)
        let challenge = try await pairingContext.currentChallenge(now: now)
        let conflictTransport = FakeRelayTransport()
        await conflictTransport.setHandler { request in
            switch request.url?.path {
            case RelayWireContract.challengePath:
                return (
                    try JSONEncoder().encode(RelayChallengeEnvelope(challenge: challenge)),
                    makeHTTPResponse(request)
                )
            case RelayWireContract.pairingPath:
                return (Data(), makeHTTPResponse(request, statusCode: 409))
            default:
                throw URLError(.badURL)
            }
        }
        let conflict = makeCoordinator(
            browser: FakeRelayBrowser(),
            transport: conflictTransport,
            now: { self.now }
        )
        conflict.startDiscovery()
        await conflict.connect(to: makeTestEndpoint())
        if case .failed = conflict.state {} else {
            XCTFail("A competing candidate must fail closed")
        }
        XCTAssertNil(conflict.session)

        let expiredTransport = FakeRelayTransport()
        await expiredTransport.setHandler { request in
            (Data(), makeHTTPResponse(request, statusCode: 410))
        }
        let expired = makeCoordinator(
            browser: FakeRelayBrowser(),
            transport: expiredTransport,
            now: { self.now }
        )
        expired.startDiscovery()
        await expired.connect(to: makeTestEndpoint())
        XCTAssertEqual(expired.state, .sessionExpired)
        XCTAssertNil(expired.session)

        let unpairedTransport = FakeRelayTransport()
        await unpairedTransport.setHandler { request in
            (Data(), makeHTTPResponse(request, statusCode: 503))
        }
        let unpaired = makeCoordinator(
            browser: FakeRelayBrowser(),
            transport: unpairedTransport,
            now: { self.now }
        )
        unpaired.startDiscovery()
        await unpaired.connect(to: makeTestEndpoint())
        if case .failed = unpaired.state {} else {
            XCTFail("An unavailable relay must not transition to connected")
        }
        XCTAssertNil(unpaired.session)
    }

    func testReconnectUsesSameSessionAndFreshSignedProbe() async throws {
        let browser = FakeRelayBrowser()
        let transport = FakeRelayTransport()
        let server = try RelayCoordinatorTestServer(now: now, clock: { self.now })
        await transport.setHandler { request in try await server.handle(request) }
        let coordinator = makeCoordinator(browser: browser, transport: transport, now: { self.now })
        coordinator.startDiscovery()
        await coordinator.connect(to: makeTestEndpoint())
        try await server.approvePending()
        await coordinator.confirmCodesMatch()
        let sessionID = coordinator.session?.sessionID

        coordinator.handleNetworkAvailability(.unavailable)
        XCTAssertEqual(coordinator.state, .networkUnavailable)
        let requestsDuringOutage = await transport.allRequests().filter {
            $0.url?.path == RelayWireContract.playlistSnapshotPath
        }
        XCTAssertTrue(requestsDuringOutage.isEmpty)
        coordinator.handleNetworkAvailability(.available)
        let didReconnect = await waitUntil {
            if case .connected = coordinator.state { return true }
            return false
        }
        XCTAssertTrue(didReconnect)

        guard case let .connected(reconnectedID, _) = coordinator.state else {
            return XCTFail("Expected reconnect, got \(coordinator.state)")
        }
        XCTAssertEqual(reconnectedID, sessionID?.rawValue)
        let snapshotRequests = await transport.allRequests().filter {
            $0.url?.path == RelayWireContract.playlistSnapshotPath
        }
        XCTAssertEqual(snapshotRequests.count, 1)
        XCTAssertNotNil(snapshotRequests.first?.value(forHTTPHeaderField: RelayWireContract.authenticationHeader))
        XCTAssertNotNil(snapshotRequests.first?.value(forHTTPHeaderField: RelayWireContract.mediaCapabilityHeader))
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

    private func waitUntil(
        timeoutIterations: Int = 40,
        condition: @escaping @MainActor () async -> Bool
    ) async -> Bool {
        for _ in 0 ..< timeoutIterations {
            if await condition() { return true }
            try? await Task.sleep(for: .milliseconds(50))
        }
        return false
    }
}

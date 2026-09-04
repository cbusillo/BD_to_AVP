import XCTest
@testable import BDToAVPPlayer

@MainActor
final class RelaySessionCoordinatorTests: XCTestCase {
    func testCoordinatorShowsNumericComparisonThenConnectsAfterMacApproval() async throws {
        let now = Date()
        let pairing = try RelayServerPairingContext(now: now)
        let transport = CoordinatorTransport(pairing: pairing, now: now)
        let endpoint = RelayDiscoveredEndpoint(id: "Mac", displayName: "My Mac", baseURL: URL(string: "http://relay.local")!)
        let coordinator = RelaySessionCoordinator(transport: transport, clock: { now }, nonce: { UUID().uuidString })
        coordinator.startDiscovery()
        await coordinator.connect(to: endpoint)
        guard case .confirming = coordinator.state else { return XCTFail("Expected numeric comparison") }
        XCTAssertEqual(coordinator.shortAuthenticationString?.digits.count, 6)
        let pendingCandidate = await pairing.pendingCandidateSummary()
        let candidate = try XCTUnwrap(pendingCandidate)
        try await pairing.approve(candidateID: candidate.candidateID, now: now)
        await coordinator.confirmCodesMatch()
        guard case .connected = coordinator.state else { return XCTFail("Expected connected after both confirmations") }
    }
}

private actor CoordinatorTransport: RelayTransport {
    private let pairing: RelayServerPairingContext
    private let now: Date
    private let replayStore = try! RelayReplayNonceStore()
    private var provisionalSession: RelayEstablishedSession?
    private var establishedSession: RelayEstablishedSession?

    init(pairing: RelayServerPairingContext, now: Date) {
        self.pairing = pairing
        self.now = now
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let path = request.url!.path
        switch path {
        case RelayWireContract.challengePath:
            return response(try JSONEncoder().encode(RelayChallengeEnvelope(challenge: pairing.challenge)), request: request, status: 200)
        case RelayWireContract.pairingPath:
            let result = try await pairing.accept(JSONDecoder().decode(RelayPairingRequest.self, from: request.httpBody ?? Data()), now: now)
            provisionalSession = result.provisionalSession
            return response(try JSONEncoder().encode(RelayPairingCandidateEnvelope(candidate: result.candidate)), request: request, status: 201)
        case RelayWireContract.pairingConfirmPath:
            let session = try XCTUnwrap(provisionalSession)
            let authentication = try authenticatedRequest(request)
            try await session.verify(authentication, actualMethod: "POST", actualRequestTarget: path, body: request.httpBody ?? Data(), now: now, replayStore: replayStore)
            let confirmation = try JSONDecoder().decode(RelayPairingConfirmation.self, from: request.httpBody ?? Data())
            let result = try await pairing.confirm(confirmation, now: now)
            if let established = result.session { establishedSession = established }
            let body = try JSONEncoder().encode(RelayPairingConfirmationEnvelope(confirmation: result.response))
            let response = response(body, request: request, status: result.response.state == .waitingForMac ? 202 : 200)
            return try authenticate(response, session: session, request: authentication)
        case RelayWireContract.playlistSnapshotPath:
            let session = try XCTUnwrap(establishedSession)
            let authentication = try authenticatedRequest(request)
            try await session.verify(authentication, actualMethod: "GET", actualRequestTarget: path, body: Data(), now: now, replayStore: replayStore)
            return try authenticate(response(Data("{}".utf8), request: request, status: 200), session: session, request: authentication)
        default:
            return response(Data(), request: request, status: 404)
        }
    }

    private func authenticatedRequest(_ request: URLRequest) throws -> RelayAuthenticatedRequest {
        let encoded = try XCTUnwrap(request.value(forHTTPHeaderField: RelayWireContract.authenticationHeader))
        return try JSONDecoder().decode(RelayAuthenticatedRequest.self, from: try XCTUnwrap(Data(base64Encoded: encoded)))
    }

    private func authenticate(_ response: (Data, HTTPURLResponse), session: RelayEstablishedSession, request: RelayAuthenticatedRequest) throws -> (Data, HTTPURLResponse) {
        let authentication = try session.authenticateResponse(requestNonce: request.nonce, statusCode: response.1.statusCode, body: response.0)
        var headers = response.1.allHeaderFields as! [String: String]
        headers[RelayWireContract.responseAuthenticationHeader] = try JSONEncoder().encode(authentication).base64EncodedString()
        return (response.0, HTTPURLResponse(url: response.1.url!, statusCode: response.1.statusCode, httpVersion: "HTTP/1.1", headerFields: headers)!)
    }

    private func response(_ data: Data, request: URLRequest, status: Int) -> (Data, HTTPURLResponse) {
        (data, HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: ["content-type": RelayWireContract.jsonContentType])!)
    }
}

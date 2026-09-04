import Foundation
import XCTest
@testable import BDToAVPPlayer

actor FakeRelayTransport: RelayTransport {
    typealias Handler = @Sendable (URLRequest) async throws -> (Data, HTTPURLResponse)
    private var handler: Handler?
    private var requests: [URLRequest] = []
    func setHandler(_ handler: @escaping Handler) { self.handler = handler }
    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        requests.append(request)
        guard let handler else { throw URLError(.badServerResponse) }
        return try await handler(request)
    }
    func allRequests() -> [URLRequest] { requests }
}

func makeTestEndpoint(id: String = "Vision-Pro", baseURL: URL = URL(string: "http://relay.local:7431")!) -> RelayDiscoveredEndpoint {
    RelayDiscoveredEndpoint(id: id, displayName: id, baseURL: baseURL)
}

func makeAuthenticatedHTTPResponse(
    _ request: URLRequest,
    body: Data,
    serverSession: RelayEstablishedSession,
    statusCode: Int = 200,
    contentType: String = "application/json"
) throws -> HTTPURLResponse {
    let encoded = try XCTUnwrap(request.value(forHTTPHeaderField: RelayWireContract.authenticationHeader))
    let authentication = try JSONDecoder().decode(RelayAuthenticatedRequest.self, from: try XCTUnwrap(Data(base64Encoded: encoded)))
    let proof = try serverSession.authenticateResponse(requestNonce: authentication.nonce, statusCode: statusCode, body: body)
    return HTTPURLResponse(
        url: request.url!, statusCode: statusCode, httpVersion: "HTTP/1.1",
        headerFields: ["content-type": contentType, RelayWireContract.responseAuthenticationHeader: try JSONEncoder().encode(proof).base64EncodedString()]
    )!
}

func makePairedSessions(now: Date) async throws -> (client: RelayEstablishedSession, server: RelayEstablishedSession) {
    let pairing = try RelayServerPairingContext(now: now)
    let attempt = try RelayClientPairingAttempt(challenge: pairing.challenge, now: now)
    let offered = try await pairing.accept(attempt.request, now: now)
    let provisional = try attempt.complete(with: offered.candidate, now: now)
    try await pairing.approve(candidateID: offered.candidate.candidateID, now: now)
    let confirmed = try await pairing.confirm(try provisional.confirmation(decision: .codesMatch), now: now)
    return (try provisional.complete(with: try XCTUnwrap(confirmed.response.acceptance), now: now), try XCTUnwrap(confirmed.session))
}

func makePairedClientSession(now: Date) async throws -> RelayEstablishedSession {
    try await makePairedSessions(now: now).0
}

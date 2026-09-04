import XCTest
@testable import BDToAVPPlayer

final class RelayHLSResourceLoaderTests: XCTestCase {
    func testAuthenticatedRequestFactorySignsEstablishedSessions() async throws {
        let now = Date()
        let (client, server) = try await makePairedSessions(now: now)
        let prepared = try RelayAuthenticatedRequestFactory.makeRequest(
            baseURL: URL(string: "http://relay.local")!,
            path: RelayWireContract.playlistPath,
            signer: client,
            clock: { now },
            nonce: { "hls-request-0001" }
        )
        try await server.verify(
            prepared.authentication,
            actualMethod: "GET",
            actualRequestTarget: RelayWireContract.playlistPath,
            body: Data(),
            now: now,
            replayStore: try RelayReplayNonceStore()
        )
    }
}

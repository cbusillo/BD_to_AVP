import XCTest
@testable import BluRayToVisionPro

final class RelayHostTests: XCTestCase {
    func testMediaFailsClosedUntilCandidateAndBothApprovalsComplete() async throws {
        let fixture = try makeFixture()
        let host = try RelayHost.start(configuration: try RelayHostConfiguration(fixtureDirectory: fixture.directory), fixture: fixture.fixture)
        let blockedMedia = await host.handle(rawRequest(method: "GET", path: RelayWireContract.playlistPath), peer: .localNetwork)
        let blockedPeer = await host.handle(rawRequest(method: "GET", path: RelayWireContract.challengePath), peer: .nonLocal)
        XCTAssertEqual(blockedMedia.statusCode, 503)
        XCTAssertEqual(blockedPeer.statusCode, 403)

        let challengeData = await host.handle(rawRequest(method: "GET", path: RelayWireContract.challengePath), peer: .localNetwork).body
        let challenge = try JSONDecoder().decode(RelayChallengeEnvelope.self, from: challengeData).challenge
        let attempt = try RelayClientPairingAttempt(challenge: challenge, now: Date())
        let pairingResponse = await host.handle(rawRequest(method: "POST", path: RelayWireContract.pairingPath, body: try JSONEncoder().encode(attempt.request)), peer: .localNetwork)
        XCTAssertEqual(pairingResponse.statusCode, 201)
        let candidate = try JSONDecoder().decode(RelayPairingCandidateEnvelope.self, from: pairingResponse.body).candidate
        let provisional = try attempt.complete(with: candidate, now: Date())
        let blockedCandidateMedia = await host.handle(rawRequest(method: "GET", path: RelayWireContract.playlistPath), peer: .localNetwork)
        XCTAssertEqual(blockedCandidateMedia.statusCode, 503)

        try await host.approvePairingCandidate(candidate.candidateID)
        let confirmation = try provisional.confirmation(decision: .codesMatch)
        let body = try JSONEncoder().encode(confirmation)
        let signed = try provisional.authenticationSession.signRequest(
            method: "POST", requestTarget: RelayWireContract.pairingConfirmPath, timestamp: Date(), nonce: "host-confirm-0001", body: body
        )
        let response = await host.handle(
            rawRequest(method: "POST", path: RelayWireContract.pairingConfirmPath, body: body, authentication: signed),
            peer: .localNetwork
        )
        XCTAssertEqual(response.statusCode, 200)
        let lifecycle = await host.currentLifecycle()
        XCTAssertEqual(lifecycle, .paired)
    }

    func testStaleMacApprovalCannotDisplaceCurrentCandidate() async throws {
        let fixture = try makeFixture()
        let host = try RelayHost.start(configuration: try RelayHostConfiguration(fixtureDirectory: fixture.directory), fixture: fixture.fixture)
        let challenge = try JSONDecoder().decode(
            RelayChallengeEnvelope.self,
            from: await host.handle(rawRequest(method: "GET", path: RelayWireContract.challengePath), peer: .localNetwork).body
        ).challenge
        let attempt = try RelayClientPairingAttempt(challenge: challenge, now: Date())
        let candidate = try JSONDecoder().decode(
            RelayPairingCandidateEnvelope.self,
            from: await host.handle(rawRequest(method: "POST", path: RelayWireContract.pairingPath, body: try JSONEncoder().encode(attempt.request)), peer: .localNetwork).body
        ).candidate
        let stale = try RelayPairingCandidateIdentifier(rawValue: UUID().uuidString)
        await XCTAssertThrowsErrorAsync(try await host.approvePairingCandidate(stale), .pairingCandidateNotFound)
        let activeCandidate = await host.currentPairingCandidate()
        XCTAssertEqual(activeCandidate?.candidateID, candidate.candidateID)
    }

    private func makeFixture() throws -> (directory: URL, fixture: RelayEventHLSFixture) {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try Data([0]).write(to: directory.appendingPathComponent("init.mp4"))
        try Data([1]).write(to: directory.appendingPathComponent("segment-0001.m4s"))
        try "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-PLAYLIST-TYPE:EVENT\n#EXT-X-TARGETDURATION:6\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:6.0,\nsegment-0001.m4s\n".write(to: directory.appendingPathComponent("media.m3u8"), atomically: true, encoding: .utf8)
        return (directory, try RelayEventHLSFixture.load(directory: directory))
    }
}

private func rawRequest(
    method: String,
    path: String,
    body: Data = Data(),
    authentication: RelayAuthenticatedRequest? = nil
) -> Data {
    var headers = ["Host: relay", "Content-Length: \(body.count)"]
    if let authentication {
        headers.append("\(RelayWireContract.authenticationHeader): \(try! JSONEncoder().encode(authentication).base64EncodedString())")
    }
    return Data("\(method) \(path) HTTP/1.1\r\n\(headers.joined(separator: "\r\n"))\r\n\r\n".utf8) + body
}

private func XCTAssertThrowsErrorAsync<T>(
    _ expression: @autoclosure () async throws -> T,
    _ expected: RelaySessionError,
    file: StaticString = #filePath,
    line: UInt = #line
) async {
    do {
        _ = try await expression()
        XCTFail("Expected \(expected)", file: file, line: line)
    } catch {
        XCTAssertEqual(error as? RelaySessionError, expected, file: file, line: line)
    }
}

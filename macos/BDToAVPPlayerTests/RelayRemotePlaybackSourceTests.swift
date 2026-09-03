import Foundation
import XCTest
@testable import BDToAVPPlayer

final class RelayRemotePlaybackSourceTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_700_000_000)
    private let baseURL = URL(string: "http://relay.local:7431")!

    func testPlaylistSnapshotEnforcesRetainedWindowSeekPolicy() async throws {
        let session = try await makePairedClientSession(now: now)
        var source = try RelayRemotePlaybackSource(session: session, serverBaseURL: baseURL)
        try source.updateRetainedWindow(with: Data("""
        {
          "earliestPlayableTimeMilliseconds": 60000,
          "totalDurationMilliseconds": 120000,
          "isFinalized": false,
          "segments": [
            {
              "sequenceNumber": 30,
              "startTimeMilliseconds": 60000,
              "durationMilliseconds": 60000,
              "resourceIdentifier": "segment-030.m4s"
            }
          ]
        }
        """.utf8))

        XCTAssertEqual(source.retainedSeekPolicy.validateSeek(to: 30), RelayRetainedSeekDecision.beforeRetainedHistory(earliestPlayableTime: 60))
        XCTAssertEqual(source.retainedSeekPolicy.validateSeek(to: 90), RelayRetainedSeekDecision.playable)
        XCTAssertEqual(source.retainedSeekPolicy.validateSeek(to: 121), RelayRetainedSeekDecision.notYetAvailable(latestAvailableTime: 120))

        try source.updateRetainedWindow(with: Data("""
        {
          "earliestPlayableTimeMilliseconds": 60000,
          "totalDurationMilliseconds": 120000,
          "isFinalized": true,
          "segments": [
            {
              "sequenceNumber": 30,
              "startTimeMilliseconds": 60000,
              "durationMilliseconds": 60000,
              "resourceIdentifier": "segment-030.m4s"
            }
          ]
        }
        """.utf8))
        XCTAssertEqual(source.retainedSeekPolicy.validateSeek(to: 120), RelayRetainedSeekDecision.ended(finalDuration: 120))
    }

    func testAssetUsesTheAuthenticatedCustomPlaylistURL() async throws {
        let fixedNow = now
        let session = try await makePairedClientSession(now: fixedNow)
        var source = try RelayRemotePlaybackSource(session: session, serverBaseURL: baseURL)
        let (_, loader) = source.makeAssetAndLoader(transport: FakeRelayTransport(), clock: { fixedNow })
        XCTAssertNotNil(loader)
        XCTAssertEqual(
            RelayHLSResourceLoader.customPlaylistURL(for: baseURL)?.absoluteString,
            "bdtoavprelay://relay.local:7431/relay/v1/playlist.m3u8"
        )
        source.cancelLoader()
        source.cancelLoader()
    }

    func testRefreshingRetainedWindowUsesAuthenticatedSnapshotRoute() async throws {
        let fixedNow = now
        let session = try await makePairedClientSession(now: fixedNow)
        let transport = FakeRelayTransport()
        await transport.setHandler { request in
            let snapshot = try RelayPlaylistSnapshot(
                earliestPlayableTimeMilliseconds: 2_000,
                totalDurationMilliseconds: 4_000,
                isFinalized: false,
                segments: [
                    try RelayPlaylistSegment(
                        sequenceNumber: 1,
                        startTimeMilliseconds: 2_000,
                        durationMilliseconds: 2_000,
                        resourceIdentifier: "segment-001.m4s"
                    ),
                ]
            )
            return (try JSONEncoder().encode(snapshot), makeHTTPResponse(request))
        }
        var source = try RelayRemotePlaybackSource(session: session, serverBaseURL: baseURL)

        try await source.refreshRetainedWindow(
            transport: transport,
            clock: { fixedNow },
            nonce: { "snapshot-refresh-0001" }
        )

        XCTAssertEqual(source.retainedSeekPolicy.earliestPlayableTime, 2)
        XCTAssertEqual(source.retainedSeekPolicy.latestAvailableTime, 4)
        let requests = await transport.allRequests()
        let request = try XCTUnwrap(requests.first)
        XCTAssertEqual(request.url?.path, RelayWireContract.playlistSnapshotPath)
        XCTAssertNotNil(request.value(forHTTPHeaderField: RelayWireContract.authenticationHeader))
    }
}

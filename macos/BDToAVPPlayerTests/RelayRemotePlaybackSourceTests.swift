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
        XCTAssertEqual(source.retainedSeekPolicy.validateSeek(to: .greatestFiniteMagnitude), .invalidTime)

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

    func testAssetUsesDeviceLoopbackHTTP() async throws {
        let fixedNow = now
        let session = try await makePairedClientSession(now: fixedNow)
        var source = try RelayRemotePlaybackSource(session: session, serverBaseURL: baseURL)
        let (asset, loader) = try await source.makeAssetAndLoader(transport: FakeRelayTransport(), clock: { fixedNow })
        XCTAssertNotNil(loader)
        XCTAssertEqual(asset.url.scheme, "http")
        XCTAssertEqual(asset.url.host, "127.0.0.1")
        XCTAssertTrue(asset.url.path.hasSuffix(RelayWireContract.playlistPath))
        source.cancelLoader()
        source.cancelLoader()
    }

    func testRefreshingRetainedWindowUsesAuthenticatedSnapshotRoute() async throws {
        let fixedNow = now
        let sessions = try await makePairedSessions(now: fixedNow)
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
            let body = try JSONEncoder().encode(snapshot)
            return (
                body,
                try makeAuthenticatedHTTPResponse(
                    request,
                    body: body,
                    serverSession: sessions.server
                )
            )
        }
        var source = try RelayRemotePlaybackSource(session: sessions.client, serverBaseURL: baseURL)

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

    // MARK: Live stream end and session revocation

    private func refreshError(
        status: Int, body: Data, signed: Bool
    ) async throws -> Error? {
        let fixedNow = now
        let sessions = try await makePairedSessions(now: fixedNow)
        let transport = FakeRelayTransport()
        await transport.setHandler { request in
            guard signed else {
                return (body, HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: [:])!)
            }
            return (body, try makeAuthenticatedHTTPResponse(request, body: body, serverSession: sessions.server, statusCode: status))
        }
        var source = try RelayRemotePlaybackSource(session: sessions.client, serverBaseURL: baseURL)
        do {
            try await source.refreshRetainedWindow(transport: transport, clock: { fixedNow })
            return nil
        } catch {
            return error
        }
    }

    func testSignedStreamEndedBodyThrowsStreamEnded() async throws {
        let ended = try JSONEncoder().encode(RelayLiveStreamEnded(error: .streamEnded, code: "idle", message: nil))
        let error = try await refreshError(status: 410, body: ended, signed: true)
        XCTAssertEqual(error as? RelayTransportError, .streamEnded(code: "idle", message: nil))

        let failed = try JSONEncoder().encode(RelayLiveStreamEnded(error: .streamFailed, code: "live_start_timeout", message: "Too slow."))
        let failure = try await refreshError(status: 503, body: failed, signed: true)
        XCTAssertEqual(failure as? RelayTransportError, .streamEnded(code: "live_start_timeout", message: "Too slow."))
    }

    func testEmptySignedEndStatusesKeepTheirFixtureMapping() async throws {
        let expired = try await refreshError(status: 410, body: Data(), signed: true)
        XCTAssertEqual(expired as? RelayTransportError, .sessionExpired)
        let unpaired = try await refreshError(status: 503, body: Data(), signed: true)
        XCTAssertEqual(unpaired as? RelayTransportError, .unpaired)
    }

    func testUnsignedEmpty401IsSessionRevoked() async throws {
        let error = try await refreshError(status: 401, body: Data(), signed: false)
        XCTAssertEqual(error as? RelayTransportError, .sessionRevoked)
    }

    func testOtherUnsignedResponsesStayInvalid() async throws {
        for (status, body) in [(200, Data("{}".utf8)), (503, Data()), (401, Data("x".utf8))] {
            let error = try await refreshError(status: status, body: body, signed: false)
            XCTAssertEqual(error as? RelaySessionError, .invalidResponse, "unsigned \(status)")
        }
    }
}

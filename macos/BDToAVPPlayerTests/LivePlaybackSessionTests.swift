import AVFoundation
import Foundation
import XCTest
@testable import BDToAVPPlayer

/// A revoked parent session asks to pair again; a signed live stream end shows the Mac's
/// reason and never asks to pair.
@MainActor
final class LivePlaybackSessionTests: XCTestCase {
    private let baseURL = URL(string: "http://mac.local:7432")!

    private func install(
        _ session: MVHEVCPlayerSession,
        status: LiveStreamStatus?,
        respond: @escaping @Sendable (URLRequest, RelayEstablishedSession) throws -> (Data, HTTPURLResponse)
    ) async throws -> UUID {
        let parent = try await makePairedSessions(now: Date())
        let streamID = UUID()
        let client = try parent.client.liveStreamSession(streamID: streamID)
        let server = try parent.server.liveStreamSession(streamID: streamID)
        let transport = FakeRelayTransport()
        await transport.setHandler { request in try respond(request, server) }
        let source = try RelayRemotePlaybackSource(session: client, serverBaseURL: baseURL)
        let destination = FileManager.default.temporaryDirectory
            .appendingPathComponent("LivePlaybackSessionTests").appendingPathComponent(UUID().uuidString, isDirectory: true)
        let check = try XCTUnwrap(BuiltInStereoChecks.install(destinationDirectory: destination).first)
        session.installRelayPlaybackResources(
            source: source,
            transport: transport,
            item: AVPlayerItem(url: check.url),
            mediaItem: MediaItem(id: "relay:\(client.sessionID.rawValue)", title: "Rainforest", fileName: "live.m3u8", format: .mvHEVC),
            observeItemStatus: false,
            startRefreshLoop: false,
            live: RelayLivePlaybackContext(title: "Rainforest", streamID: streamID) { status }
        )
        return streamID
    }

    func testParentRevocationShowsTheRepairMessage() async throws {
        let session = MVHEVCPlayerSession()
        _ = try await install(session, status: nil) { request, _ in
            (Data(), HTTPURLResponse(url: request.url!, statusCode: 401, httpVersion: "HTTP/1.1", headerFields: [:])!)
        }
        await session.refreshRelayWindowNow()
        XCTAssertEqual(session.state, .failed)
        XCTAssertEqual(session.failureMessage, MVHEVCPlayerSession.sessionEndedMessage)
        XCTAssertEqual(session.relayTerminationEvent?.reason, .sessionEnded)
    }

    func testSignedStreamEndShowsTheMacReasonAndNeverAsksToPair() async throws {
        let session = MVHEVCPlayerSession()
        let status = LiveStreamStatus(state: .stopped, stopReason: "sharingStopped")
        let streamID = try await install(session, status: status) { request, server in
            let body = try JSONEncoder().encode(RelayLiveStreamEnded(error: .streamEnded, code: "sharingStopped", message: nil))
            return (body, try makeAuthenticatedHTTPResponse(request, body: body, serverSession: server, statusCode: 410))
        }
        await session.refreshRelayWindowNow()
        XCTAssertEqual(session.state, .failed)
        XCTAssertEqual(session.failureMessage, "Live playback stopped on the Mac: Movie Sharing was turned off.")
        XCTAssertFalse(session.failureMessage?.contains("Pair") ?? true)
        XCTAssertEqual(session.relayTerminationEvent?.reason, .streamEnded)
        XCTAssertEqual(session.relayTerminationEvent?.sessionID, streamID.uuidString.lowercased())
    }

    func testSignedStreamFailureUsesTheMacMessage() async throws {
        let session = MVHEVCPlayerSession()
        _ = try await install(session, status: nil) { request, server in
            let body = try JSONEncoder().encode(
                RelayLiveStreamEnded(error: .streamFailed, code: "live_hls_worker_failed", message: "The worker stopped.")
            )
            return (body, try makeAuthenticatedHTTPResponse(request, body: body, serverSession: server, statusCode: 503))
        }
        await session.refreshRelayWindowNow()
        XCTAssertEqual(session.failureMessage, "Live playback stopped on the Mac: The worker stopped.")
        XCTAssertEqual(session.relayTerminationEvent?.reason, .streamEnded)
    }

    func testCoordinatorStateDoesNotEndLivePlayback() async throws {
        let session = MVHEVCPlayerSession()
        _ = try await install(session, status: nil) { request, server in
            (Data(), try makeAuthenticatedHTTPResponse(request, body: Data(), serverSession: server, statusCode: 204))
        }
        session.handleRelayCoordinatorState(.idle)
        session.handleRelayCoordinatorState(.sessionExpired)
        XCTAssertNotEqual(session.state, .failed)
        XCTAssertNotEqual(session.state, .idle)
    }
}

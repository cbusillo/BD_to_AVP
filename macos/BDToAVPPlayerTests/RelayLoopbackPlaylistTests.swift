import Foundation
import XCTest
@testable import BDToAVPPlayer

/// A live window the Mac renders must survive the player's loopback bridge unchanged except for
/// its local capability prefix.
final class RelayLoopbackPlaylistTests: XCTestCase {
    private func window(_ sequences: ClosedRange<UInt64>, durationMicroseconds: Int64 = 2_085_417) -> [LiveHLSWindowSegment] {
        sequences.map {
            LiveHLSWindowSegment(
                sequence: $0,
                uri: String(format: "segment-%09llu.m4s", $0),
                startMicroseconds: Int64($0) * durationMicroseconds,
                durationMicroseconds: durationMicroseconds
            )
        }
    }

    func testRenderedLiveWindowPassesTheRealBridgeRewrite() throws {
        for complete in [false, true] {
            let rendered = try LiveHLSPlaylistRenderer.render(
                targetDurationSeconds: 3,
                initialization: "init.mp4",
                segments: window(40 ... 90),
                complete: complete
            )

            let rewritten = try RelayLoopbackHTTPServer.rewritePlaylist(Data(rendered.playlist.utf8), prefix: "/capability")
            let lines = String(decoding: rewritten, as: UTF8.self).components(separatedBy: "\n")

            let uris = lines.filter { !$0.isEmpty && !$0.hasPrefix("#") }
            XCTAssertEqual(uris.count, 51)
            XCTAssertTrue(uris.allSatisfy { $0.hasPrefix("/capability/relay/v1/media/segment-") })
            XCTAssertTrue(lines.contains("#EXT-X-MAP:URI=\"/capability/relay/v1/media/init.mp4\""))
            XCTAssertTrue(lines.contains("#EXT-X-MEDIA-SEQUENCE:40"))
            XCTAssertTrue(lines.contains("#EXT-X-START:TIME-OFFSET=0,PRECISE=YES"))
            XCTAssertFalse(lines.contains { $0.hasPrefix("#EXT-X-PLAYLIST-TYPE") })
            XCTAssertEqual(lines.contains("#EXT-X-ENDLIST"), complete)
            XCTAssertEqual(rendered.snapshot.isFinalized, complete)
        }
    }

    func testSnapshotMatchesThePlaylistWindow() throws {
        let rendered = try LiveHLSPlaylistRenderer.render(
            targetDurationSeconds: 3,
            initialization: "init.mp4",
            segments: window(40 ... 90),
            complete: false
        )
        let snapshot = rendered.snapshot
        XCTAssertEqual(snapshot.segments.first?.sequenceNumber, 40)
        XCTAssertEqual(snapshot.earliestPlayableTimeMilliseconds, 40 * 2_085_417 / 1_000)
        XCTAssertEqual(snapshot.totalDurationMilliseconds, 91 * 2_085_417 / 1_000)
        XCTAssertEqual(
            snapshot.segments.map(\.resourceIdentifier),
            rendered.playlist.components(separatedBy: "\n")
                .filter { $0.hasPrefix(RelayWireContract.mediaPathPrefix + "segment-") }
                .map { String($0.dropFirst(RelayWireContract.mediaPathPrefix.count)) }
        )
    }
}

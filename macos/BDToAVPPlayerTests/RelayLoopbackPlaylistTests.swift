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

    func testLiveMultivariantPlaylistPassesTheRealBridgeRewrite() throws {
        let playlist = LiveHLSPlaylistRenderer.renderMultivariant(audio: [
            LiveHLSAudioChoice(name: "German DTS-HD Master Audio", language: "de", playlistName: nil),
            LiveHLSAudioChoice(name: "English DTS-HD Master Audio", language: "en", playlistName: "audio-4353.m3u8"),
        ])
        let rewritten = try RelayLoopbackHTTPServer.rewritePlaylist(Data(playlist.utf8), prefix: "/capability")
        let lines = String(decoding: rewritten, as: UTF8.self).components(separatedBy: "\n")

        let media = lines.filter { $0.hasPrefix("#EXT-X-MEDIA:") }
        XCTAssertEqual(media.count, 2)
        XCTAssertFalse(media[0].contains("URI="))
        XCTAssertTrue(media[1].hasSuffix(",URI=\"/capability/relay/v1/media/audio-4353.m3u8\""))
        XCTAssertEqual(lines.filter { !$0.isEmpty && !$0.hasPrefix("#") }, ["/capability/relay/v1/media/video.m3u8"])

        // Every media playlist the variant and its renditions name is rewritten on the bridge too.
        XCTAssertTrue(RelayLoopbackHTTPServer.isPlaylistPath("/relay/v1/media/video.m3u8"))
        XCTAssertTrue(RelayLoopbackHTTPServer.isPlaylistPath("/relay/v1/media/audio-4353.m3u8"))
        XCTAssertTrue(RelayLoopbackHTTPServer.isPlaylistPath(RelayWireContract.playlistPath))
        XCTAssertFalse(RelayLoopbackHTTPServer.isPlaylistPath("/relay/v1/media/audio-4353-segment-000000001.m4s"))
    }

    func testRenditionURIsOutsideTheMediaPrefixAreRefused() {
        let base = "#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"audio\",NAME=\"English\",URI=\"%@\"\n"
        for uri in ["https://other.test/audio.m3u8", "/relay/v1/playlist.json", "/relay/v1/media/a%2e.m3u8"] {
            let playlist = String(format: base, uri)
            XCTAssertThrowsError(try RelayLoopbackHTTPServer.rewritePlaylist(Data(playlist.utf8), prefix: "/capability"), uri)
        }
        let trailing = "#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,URI=\"/relay/v1/media/audio-1.m3u8\",NAME=\"x\"\n"
        XCTAssertThrowsError(try RelayLoopbackHTTPServer.rewritePlaylist(Data(trailing.utf8), prefix: "/capability"))
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

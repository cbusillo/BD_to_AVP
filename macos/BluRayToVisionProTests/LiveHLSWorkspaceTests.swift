import Foundation
import XCTest
@testable import BluRayToVisionPro

/// Writes a live HLS workspace the way the worker's publisher does: media first, then an atomic
/// manifest replacement.
struct LiveHLSWorkspaceWriter {
    struct Segment {
        let sequence: UInt64
        let startMicroseconds: Int64
        let durationMicroseconds: Int64
        var uri: String { String(format: "segment-%09llu.m4s", sequence) }
    }

    let root: URL
    let targetDurationSeconds: Int

    init(root: URL, targetDurationSeconds: Int = 2) throws {
        self.root = root
        self.targetDurationSeconds = targetDurationSeconds
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try Data("init".utf8).write(to: root.appendingPathComponent("init.mp4"))
    }

    /// Consecutive segments of `durationMicroseconds` starting at `first`.
    static func segments(_ range: ClosedRange<UInt64>, durationMicroseconds: Int64 = 2_000_000) -> [Segment] {
        range.map {
            Segment(sequence: $0, startMicroseconds: Int64($0) * durationMicroseconds, durationMicroseconds: durationMicroseconds)
        }
    }

    /// Writes the manifest, with an alternate audio rendition on the same timeline for each of
    /// `audioPIDs`, as the worker does for a title with more than one audio track.
    func publish(
        _ segments: [Segment],
        retired: [String] = [],
        complete: Bool = false,
        audioPIDs: [Int] = [],
        overrides: [String: Any] = [:]
    ) throws {
        for segment in segments where !FileManager.default.fileExists(atPath: root.appendingPathComponent(segment.uri).path) {
            try Data("media \(segment.sequence)".utf8).write(to: root.appendingPathComponent(segment.uri))
        }
        for pid in audioPIDs {
            try Data("audio init \(pid)".utf8).write(to: root.appendingPathComponent("audio-\(pid)-init.mp4"))
            for segment in segments {
                try Data("audio \(pid) \(segment.sequence)".utf8).write(to: root.appendingPathComponent("audio-\(pid)-\(segment.uri)"))
            }
        }
        var manifest: [String: Any] = [
            "schema_version": 1,
            "type": "live_hls",
            "complete": complete,
            "playlist": "media.m3u8",
            "initialization": "init.mp4",
            "target_duration_seconds": targetDurationSeconds,
            "media_sequence": segments.first?.sequence ?? 0,
            "earliest_available_microseconds": segments.first?.startMicroseconds ?? 0,
            "latest_available_microseconds": segments.last.map { $0.startMicroseconds + $0.durationMicroseconds } ?? 0,
            "segments": segments.map {
                [
                    "sequence": $0.sequence,
                    "uri": $0.uri,
                    "start_microseconds": $0.startMicroseconds,
                    "duration_microseconds": $0.durationMicroseconds,
                    "size_bytes": 7,
                ] as [String: Any]
            },
            "retired_segments": retired.map { ["uri": $0, "size_bytes": 7, "delete_after_unix": 1.0] as [String: Any] },
            "producer": [
                "produced_microseconds": segments.last.map { $0.startMicroseconds + $0.durationMicroseconds } ?? 0,
                "wall_seconds": 1.5,
                "held_seconds": 0.5,
                "viewer_held_seconds": 0.25,
            ],
        ]
        if !audioPIDs.isEmpty {
            manifest["audio_renditions"] = audioPIDs.map { pid in
                [
                    "id": "aac:\(pid)",
                    "codec": "aac",
                    "source_pid": pid,
                    "complete": complete,
                    "initialization": "audio-\(pid)-init.mp4",
                    "target_duration_seconds": targetDurationSeconds,
                    "media_sequence": segments.first?.sequence ?? 0,
                    "earliest_available_microseconds": segments.first?.startMicroseconds ?? 0,
                    "latest_available_microseconds": segments.last.map { $0.startMicroseconds + $0.durationMicroseconds } ?? 0,
                    "segments": segments.map {
                        [
                            "sequence": $0.sequence,
                            "uri": "audio-\(pid)-\($0.uri)",
                            "start_microseconds": $0.startMicroseconds,
                            "duration_microseconds": $0.durationMicroseconds,
                            "size_bytes": 7,
                        ] as [String: Any]
                    },
                    "retired_segments": [[String: Any]](),
                ] as [String: Any]
            }
        }
        manifest.merge(overrides) { $1 }
        try write(JSONSerialization.data(withJSONObject: manifest))
    }

    func write(_ data: Data) throws {
        try data.write(to: root.appendingPathComponent("live-hls.json"), options: .atomic)
    }
}

final class LiveHLSWorkspaceTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("LiveHLSWorkspaceTests-\(UUID().uuidString)")
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    func testMissingManifestIsNotYetPublished() throws {
        var workspace = LiveHLSWorkspace(rootURL: root)
        XCTAssertFalse(try workspace.poll())
        XCTAssertNil(workspace.window.rendered)
        XCTAssertThrowsError(try workspace.media(named: "init.mp4"))
    }

    func testWindowGrowsEvictsAndServesOnlyListedMedia() throws {
        let writer = try LiveHLSWorkspaceWriter(root: root)
        var workspace = LiveHLSWorkspace(rootURL: root)
        try writer.publish(LiveHLSWorkspaceWriter.segments(0 ... 2))
        XCTAssertTrue(try workspace.poll())
        XCTAssertFalse(try workspace.poll(), "an unchanged manifest is not reread")

        // Segment 0 is evicted but still listed as retired, so it is served.
        try writer.publish(LiveHLSWorkspaceWriter.segments(1 ... 4), retired: ["segment-000000000.m4s"])
        XCTAssertTrue(try workspace.poll())
        XCTAssertEqual(try workspace.media(named: "segment-000000000.m4s"), Data("media 0".utf8))
        XCTAssertEqual(workspace.window.timing(for: "segment-000000000.m4s")?.startMicroseconds, 0)
        let playlist = try XCTUnwrap(workspace.window.rendered?.playlist)
        XCTAssertTrue(playlist.contains("#EXT-X-MEDIA-SEQUENCE:1\n"))
        XCTAssertFalse(playlist.contains("segment-000000000.m4s"))

        // Once the worker drops it from the retired list, it is refused even while the file exists.
        try writer.publish(LiveHLSWorkspaceWriter.segments(2 ... 5), retired: ["segment-000000001.m4s"])
        XCTAssertTrue(try workspace.poll())
        XCTAssertThrowsError(try workspace.media(named: "segment-000000000.m4s")) {
            XCTAssertEqual($0 as? LiveHLSWorkspace.MediaError, .notFound)
        }
        // A listed retired segment the worker has already unlinked is not found, not an error.
        try FileManager.default.removeItem(at: root.appendingPathComponent("segment-000000001.m4s"))
        XCTAssertThrowsError(try workspace.media(named: "segment-000000001.m4s")) {
            XCTAssertEqual($0 as? LiveHLSWorkspace.MediaError, .notFound)
        }
        XCTAssertEqual(try workspace.media(named: "segment-000000005.m4s"), Data("media 5".utf8))
        XCTAssertThrowsError(try workspace.media(named: "live-hls.json"))
        XCTAssertThrowsError(try workspace.media(named: "segment-000000006.m4s"))
    }

    func testPlaylistIsALiveWindowThatStartsAtTheEarliestSegmentAndEndsOnCompletion() throws {
        let writer = try LiveHLSWorkspaceWriter(root: root)
        var workspace = LiveHLSWorkspace(rootURL: root)
        try writer.publish(LiveHLSWorkspaceWriter.segments(3 ... 4))
        _ = try workspace.poll()
        let live = try XCTUnwrap(workspace.window.rendered)

        XCTAssertEqual(live.playlist, """
        #EXTM3U
        #EXT-X-VERSION:7
        #EXT-X-TARGETDURATION:2
        #EXT-X-MEDIA-SEQUENCE:3
        #EXT-X-START:TIME-OFFSET=0,PRECISE=YES
        #EXT-X-INDEPENDENT-SEGMENTS
        #EXT-X-MAP:URI="/relay/v1/media/init.mp4"
        #EXTINF:2.000000,
        /relay/v1/media/segment-000000003.m4s
        #EXTINF:2.000000,
        /relay/v1/media/segment-000000004.m4s

        """)
        XCTAssertFalse(live.playlist.contains("PLAYLIST-TYPE"))
        XCTAssertFalse(live.snapshot.isFinalized)

        try writer.publish(LiveHLSWorkspaceWriter.segments(3 ... 5), complete: true)
        _ = try workspace.poll()
        let complete = try XCTUnwrap(workspace.window.rendered)
        XCTAssertTrue(complete.playlist.hasSuffix("#EXT-X-ENDLIST\n"))
        XCTAssertTrue(complete.snapshot.isFinalized)
        XCTAssertTrue(workspace.window.complete)
    }

    func testFractionalMillisecondSegmentsKeepTheSnapshotContiguous() throws {
        // 23.976 fps GOPs: 50 frames last 2,085,417 µs, which is not a whole millisecond.
        let writer = try LiveHLSWorkspaceWriter(root: root, targetDurationSeconds: 3)
        var workspace = LiveHLSWorkspace(rootURL: root)
        let segments = LiveHLSWorkspaceWriter.segments(0 ... 99, durationMicroseconds: 2_085_417)
        try writer.publish(segments)
        _ = try workspace.poll()

        let rendered = try XCTUnwrap(workspace.window.rendered)
        XCTAssertEqual(rendered.snapshot.segments.count, 100)
        XCTAssertEqual(rendered.snapshot.totalDurationMilliseconds, 208_541)
        XCTAssertTrue(rendered.playlist.contains("#EXTINF:2.085417,\n"))
        // The snapshot decoder re-checks contiguity.
        let encoded = try JSONEncoder().encode(rendered.snapshot)
        XCTAssertEqual(try JSONDecoder().decode(RelayPlaylistSnapshot.self, from: encoded), rendered.snapshot)
    }

    func testMalformedManifestsFail() throws {
        let writer = try LiveHLSWorkspaceWriter(root: root)
        let base = LiveHLSWorkspaceWriter.segments(0 ... 2)
        func segmentObjects(_ edit: (inout [[String: Any]]) -> Void) -> [[String: Any]] {
            var objects: [[String: Any]] = base.map {
                ["sequence": $0.sequence, "uri": $0.uri, "start_microseconds": $0.startMicroseconds,
                 "duration_microseconds": $0.durationMicroseconds, "size_bytes": 7]
            }
            edit(&objects)
            return objects
        }
        let cases: [String: [String: Any]] = [
            "schema": ["schema_version": 2],
            "initialization": ["initialization": "../init.mp4"],
            "target": ["target_duration_seconds": 0],
            "staging name": ["segments": segmentObjects { $0[1]["uri"] = "segment-1.m4s" }],
            "path in name": ["segments": segmentObjects { $0[1]["uri"] = "../segment-000000001.m4s" }],
            "sequence gap": ["segments": segmentObjects { $0[2]["sequence"] = 5 }],
            "timeline gap": ["segments": segmentObjects { $0[2]["start_microseconds"] = 4_000_001 }],
            "over target": ["segments": segmentObjects { $0[0]["duration_microseconds"] = 2_500_001 }],
            "empty segment": ["segments": segmentObjects { $0[0]["duration_microseconds"] = 0 }],
            "media sequence": ["media_sequence": 1],
            "latest": ["latest_available_microseconds": 7_000_000],
            "retired name": ["retired_segments": [["uri": "media.m3u8", "size_bytes": 7]]],
        ]
        for (label, overrides) in cases {
            var workspace = LiveHLSWorkspace(rootURL: root)
            try writer.publish(base, overrides: overrides)
            XCTAssertThrowsError(try workspace.poll(), label) {
                XCTAssertTrue($0 is LiveHLSManifestError, label)
            }
        }
        var workspace = LiveHLSWorkspace(rootURL: root)
        try writer.write(Data(repeating: 0x20, count: LiveHLSManifest.maximumBytes + 1))
        XCTAssertThrowsError(try workspace.poll())
        try writer.write(Data("{".utf8))
        XCTAssertThrowsError(try workspace.poll())
    }

    func testManifestThatRegressesFails() throws {
        let writer = try LiveHLSWorkspaceWriter(root: root)
        var workspace = LiveHLSWorkspace(rootURL: root)
        try writer.publish(LiveHLSWorkspaceWriter.segments(2 ... 4), complete: true)
        _ = try workspace.poll()

        try writer.publish(LiveHLSWorkspaceWriter.segments(2 ... 4), complete: false)
        XCTAssertThrowsError(try workspace.poll(), "completion reverted")

        var rewound = LiveHLSWorkspace(rootURL: root)
        try writer.publish(LiveHLSWorkspaceWriter.segments(2 ... 4))
        _ = try rewound.poll()
        try writer.publish(LiveHLSWorkspaceWriter.segments(1 ... 4))
        XCTAssertThrowsError(try rewound.poll(), "media sequence went back")

        var retimed = LiveHLSWorkspace(rootURL: root)
        try writer.publish(LiveHLSWorkspaceWriter.segments(0 ... 1))
        _ = try retimed.poll()
        try writer.publish(LiveHLSWorkspaceWriter.segments(1 ... 2, durationMicroseconds: 1_500_000))
        XCTAssertThrowsError(try retimed.poll(), "a URI changed its timing")
    }

    func testAudioRenditionsAreRenderedAndServedBesideTheVideo() throws {
        let writer = try LiveHLSWorkspaceWriter(root: root)
        var workspace = LiveHLSWorkspace(rootURL: root)
        try writer.publish(LiveHLSWorkspaceWriter.segments(0 ... 2), audioPIDs: [4_353])
        XCTAssertTrue(try workspace.poll())

        XCTAssertEqual(workspace.window.audioWindows.map(\.pid), [4_353])
        let audio = try XCTUnwrap(workspace.window.mediaPlaylist(named: "audio-4353.m3u8"))
        XCTAssertTrue(audio.contains("#EXT-X-MAP:URI=\"/relay/v1/media/audio-4353-init.mp4\""))
        XCTAssertTrue(audio.contains("\n/relay/v1/media/audio-4353-segment-000000002.m4s\n"))
        XCTAssertEqual(workspace.window.mediaPlaylist(named: "video.m3u8"), workspace.window.rendered?.playlist)
        XCTAssertNil(workspace.window.mediaPlaylist(named: "audio-4352.m3u8"), "only carried renditions are served")
        XCTAssertEqual(try workspace.media(named: "audio-4353-segment-000000001.m4s"), Data("audio 4353 1".utf8))
        XCTAssertEqual(try workspace.media(named: "audio-4353-init.mp4"), Data("audio init 4353".utf8))
        XCTAssertThrowsError(try workspace.media(named: "audio-4352-segment-000000001.m4s"))

        try writer.publish(LiveHLSWorkspaceWriter.segments(0 ... 3))
        XCTAssertThrowsError(try workspace.poll(), "a rendition disappeared mid-stream") {
            XCTAssertEqual($0 as? LiveHLSManifestError, LiveHLSManifestError(reason: "audio_renditions_changed"))
        }
    }

    func testMalformedAudioRenditionsFail() throws {
        let writer = try LiveHLSWorkspaceWriter(root: root)
        func rendition(_ edit: (inout [String: Any]) -> Void) throws -> [[String: Any]] {
            try writer.publish(LiveHLSWorkspaceWriter.segments(0 ... 2), audioPIDs: [4_353])
            let data = try Data(contentsOf: root.appendingPathComponent("live-hls.json"))
            let manifest = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
            var renditions = try XCTUnwrap(manifest["audio_renditions"] as? [[String: Any]])
            edit(&renditions[0])
            return renditions
        }
        let cases: [String: [[String: Any]]] = [
            "video name": try rendition { $0["segments"] = [["sequence": 0, "uri": "segment-000000000.m4s",
                                                            "start_microseconds": 0, "duration_microseconds": 2_000_000, "size_bytes": 7]] },
            "other prefix": try rendition { $0["initialization"] = "audio-4352-init.mp4" },
            "pid": try rendition { $0["source_pid"] = 0 },
            "latest": try rendition { $0["latest_available_microseconds"] = 1 },
        ]
        for (label, renditions) in cases {
            var workspace = LiveHLSWorkspace(rootURL: root)
            try writer.publish(LiveHLSWorkspaceWriter.segments(0 ... 2), audioPIDs: [4_353], overrides: ["audio_renditions": renditions])
            XCTAssertThrowsError(try workspace.poll(), label)
        }
    }

    func testMultivariantPlaylistOffersTheMuxedTrackAndEachRendition() {
        let playlist = LiveHLSPlaylistRenderer.renderMultivariant(audio: [
            LiveHLSAudioChoice(name: "German DTS-HD Master Audio", language: "de", playlistName: nil),
            LiveHLSAudioChoice(name: "English DTS-HD Master Audio", language: "en", playlistName: "audio-4353.m3u8"),
            LiveHLSAudioChoice(name: "English DTS-HD Master Audio", language: "en", playlistName: "audio-4354.m3u8"),
        ])
        let lines = playlist.components(separatedBy: "\n")
        let media = lines.filter { $0.hasPrefix("#EXT-X-MEDIA:") }
        XCTAssertEqual(media.count, 3)
        XCTAssertTrue(media[0].contains("NAME=\"German DTS-HD Master Audio\",LANGUAGE=\"de\",DEFAULT=YES"))
        XCTAssertFalse(media[0].contains("URI="), "the selected track plays from the video segments")
        XCTAssertTrue(media[1].hasSuffix(",URI=\"/relay/v1/media/audio-4353.m3u8\""))
        XCTAssertTrue(media[1].contains("DEFAULT=NO"))
        XCTAssertTrue(media[2].contains("NAME=\"English DTS-HD Master Audio (3)\""), "names stay unique in the group")
        let variant = try? XCTUnwrap(lines.firstIndex { $0.hasPrefix("#EXT-X-STREAM-INF:") })
        XCTAssertEqual(variant.map { lines[$0 + 1] }, "/relay/v1/media/video.m3u8")
        XCTAssertEqual(variant.map { lines[$0].contains("AUDIO=\"audio\"") && lines[$0].contains("CLOSED-CAPTIONS=NONE") }, true)

        XCTAssertEqual(LiveHLSPlaylistRenderer.languageTag(forDiscLanguage: "deu"), "de")
        XCTAssertEqual(LiveHLSPlaylistRenderer.languageTag(forDiscLanguage: "eng"), "en")
        XCTAssertNil(LiveHLSPlaylistRenderer.languageTag(forDiscLanguage: nil))
        XCTAssertNil(LiveHLSPlaylistRenderer.languageTag(forDiscLanguage: "e\"n"))
    }

    func testSymlinkedManifestAndMediaAreRefused() throws {
        let writer = try LiveHLSWorkspaceWriter(root: root)
        var workspace = LiveHLSWorkspace(rootURL: root)
        try writer.publish(LiveHLSWorkspaceWriter.segments(0 ... 1))
        _ = try workspace.poll()
        let outside = root.deletingLastPathComponent().appendingPathComponent("outside-\(UUID().uuidString)")
        try Data("secret".utf8).write(to: outside)
        defer { try? FileManager.default.removeItem(at: outside) }
        let segment = root.appendingPathComponent("segment-000000001.m4s")
        try FileManager.default.removeItem(at: segment)
        try FileManager.default.createSymbolicLink(at: segment, withDestinationURL: outside)
        XCTAssertThrowsError(try workspace.media(named: "segment-000000001.m4s"))

        let manifest = root.appendingPathComponent("live-hls.json")
        try FileManager.default.removeItem(at: manifest)
        try FileManager.default.createSymbolicLink(at: manifest, withDestinationURL: outside)
        var fresh = LiveHLSWorkspace(rootURL: root)
        XCTAssertThrowsError(try fresh.poll())
    }
}

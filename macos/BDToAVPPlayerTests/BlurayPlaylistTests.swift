import XCTest
@testable import BDToAVPPlayer

/// Titles made of several clips (#1142). The fixtures are cut from real discs, read through
/// libbluray and libudfread from the disc images, which are never mounted. A cut keeps whole TS
/// packets, in file order, of the PES packets in a time window:
///
/// - Ocean Dreams 3D (2013), playlist 00022: clip 00022 from 1,048,560 for 5,630.875 s, then clip
///   00052 from 1,048,560 at playlist time 506,778,772. `oceandreams-clip1-tail` is clip 00022's
///   video from its last random access unit to its end, with its last audio (PID 0x1100, DTS).
///   `oceandreams-clip2-head` is the first half second of clip 00052, whose audio is DTS-HD MA.
/// - Fast X (2023), playlist 00000: clip 00000 from 54,090,090, a second after it starts, then
///   clip 00002 (from 54,000,000, 1.001 s, no audio) at playlist time 760,449,690.
///   `fastx-clip1-head` is clip 00000's first 1.25 s of video, with audio PID 0x1102 (E-AC-3) from
///   0.3 s before the playlist item starts. `fastx-clip2` is clip 00002's whole SSIF.
/// - The playlists and clip information files, as they are on the discs.
final class BlurayPlaylistTests: XCTestCase {
    private static let frameTicks: ClosedRange<UInt64> = 3_753...3_754

    // MARK: Clip information and playlists

    func testClipInformationGivesEntryPointsExtentsAndPresentationRange() throws {
        try withClipIndex("oceandreams-00052", pid: 0x1011) { index in
            XCTAssertTrue(index.single_sequence)
            XCTAssertEqual(index.presentation_start, 1_048_560)
            XCTAssertEqual(index.presentation_end, 2_125_886)
            XCTAssertEqual(index.source_packets, 111_168)
            // The same entries libbluray's own CLPI parser reads.
            XCTAssertEqual(index.entry_point_count, 17)
            XCTAssertEqual(entry(index.entry_points[0]), Entry(pts: 1_048_064, spn: 4))
            XCTAssertEqual(entry(index.entry_points[1]), Entry(pts: 1_123_328, spn: 353))
            XCTAssertEqual(entry(index.entry_points[3]), Entry(pts: 1_221_120, spn: 12_414))
            XCTAssertEqual(Array(UnsafeBufferPointer(start: index.extent_starts, count: index.extent_count)), [0, 100_352])
        }
        try withClipIndex("oceandreams-00512", pid: 0) { index in
            XCTAssertEqual(index.entry_point_count, 0)
            XCTAssertEqual(Array(UnsafeBufferPointer(start: index.extent_starts, count: index.extent_count)), [0, 30_720])
        }
    }

    func testEntryPointLookupFindsTheLastEntryAtOrBeforeATime() throws {
        try withClipIndex("oceandreams-00052", pid: 0x1011) { index in
            withUnsafePointer(to: index) { index in
                XCTAssertEqual(bd3d_entry_point_at(index, 1_123_328 + 3_000).map { entry($0.pointee) }, Entry(pts: 1_123_328, spn: 353))
                XCTAssertEqual(bd3d_entry_point_at(index, 1_123_328).map { entry($0.pointee) }, Entry(pts: 1_123_328, spn: 353))
                XCTAssertEqual(bd3d_entry_point_at(index, 0).map { entry($0.pointee) }, Entry(pts: 1_048_064, spn: 4))
                let last = entry(index.pointee.entry_points[index.pointee.entry_point_count - 1])
                XCTAssertEqual(bd3d_entry_point_at(index, 1 << 32).map { entry($0.pointee) }, last)
            }
        }
    }

    func testSSIFOffsetIsTheStartOfTheExtentPairHoldingAPacket() throws {
        try withClipIndex("oceandreams-00052", pid: 0x1011) { base in
            try withClipIndex("oceandreams-00512", pid: 0) { dependent in
                var base = base
                var dependent = dependent
                XCTAssertEqual(bd3d_ssif_offset(&base, &dependent, 353), 0)
                XCTAssertEqual(bd3d_ssif_offset(&base, &dependent, 100_351), 0)
                XCTAssertEqual(bd3d_ssif_offset(&base, &dependent, 100_352), (100_352 + 30_720) * 192)
                XCTAssertEqual(bd3d_ssif_offset(&base, &dependent, 110_000), (100_352 + 30_720) * 192)
            }
            try withClipIndex("fastx-00000", pid: 0x1011) { unrelated in
                var base = base
                var unrelated = unrelated
                XCTAssertEqual(bd3d_ssif_offset(&base, &unrelated, 353), -1, "Extent tables of different clips do not pair")
            }
        }
    }

    func testStereoSubPathNamesEachPlayItemsDependentClip() throws {
        XCTAssertEqual(try dependentClip("oceandreams-00022", item: 0), "00505")
        XCTAssertEqual(try dependentClip("oceandreams-00022", item: 1), "00512")
        XCTAssertEqual(try dependentClip("fastx-00000", item: 0), "00001")
        XCTAssertEqual(try dependentClip("fastx-00000", item: 1), "00003")
        XCTAssertNil(try dependentClip("fastx-00000", item: 2))
    }

    // MARK: Clip boundaries

    func testOceanDreamsClipBoundaryContinuesOnOnePlaylistTimeline() throws {
        let boundary: UInt64 = 506_778_772
        let tail = CollectingPacketSink()
        let head = CollectingPacketSink()

        _ = try BlurayTransportStream.demux(
            try fixture("oceandreams-clip1-tail", "m2ts"),
            segment: segment(inTime: 1_048_560, duration: boundary, playlistTime: 0),
            audioTrackIDs: [0x1100],
            into: tail
        )
        _ = try BlurayTransportStream.demux(
            try fixture("oceandreams-clip2-head", "m2ts"),
            segment: segment(inTime: 1_048_560, duration: 1_077_326, playlistTime: boundary),
            audioTrackIDs: [0x1100],
            into: head
        )

        XCTAssertEqual(tail.video.first?.isRandomAccess, true, "The cut starts at an I picture, not an IDR")
        XCTAssertEqual(head.video.first?.isRandomAccess, true)
        XCTAssertEqual(head.video.first?.pts, boundary)
        XCTAssertTrue(tail.video.allSatisfy { $0.pts < boundary })
        // Every frame in display order is one frame after the one before, across the boundary.
        let shown = (tail.video + head.video).map(\.pts).sorted()
        XCTAssertGreaterThan(shown.count, 20)
        XCTAssertEqual(zip(shown, shown.dropFirst()).filter { !Self.frameTicks.contains($1 - $0) }.count, 0)
        // Audio PES headers carry playlist time too: the DTS core of clip 00052 starts at the boundary.
        XCTAssertFalse(tail.audio.isEmpty)
        XCTAssertTrue(tail.audio.allSatisfy { ($0.pts ?? 0) < boundary })
        XCTAssertEqual(head.audio.first?.pts, boundary)
        XCTAssertTrue(head.audio.compactMap(\.pts).allSatisfy { (boundary..<boundary + 45_000).contains($0) })
    }

    func testFastXItemThatStartsInsideItsClipStartsAtItsEntryPointAndJoinsTheNextClip() throws {
        let inTime: UInt64 = 54_090_090
        let secondClip: UInt64 = 760_449_690
        let entryPTS = try withClipIndex("fastx-00000", pid: 0x1011) { index in
            withUnsafePointer(to: index) { bd3d_entry_point_at($0, inTime)?.pointee.pts }
        }
        let start = try XCTUnwrap(entryPTS)
        XCTAssertLessThan(inTime - start, 512, "An entry point sits where the item starts")
        let first = CollectingPacketSink()
        let unsegmented = CollectingPacketSink()
        let second = CollectingPacketSink()
        let firstData = try fixture("fastx-clip1-head", "m2ts")

        _ = try BlurayTransportStream.demux(
            firstData,
            segment: segment(inTime: inTime, duration: secondClip, playlistTime: 0, startOffset: Int64(start) - Int64(inTime)),
            audioTrackIDs: [0x1102],
            into: first
        )
        _ = try BlurayTransportStream.demux(firstData, audioTrackIDs: [0x1102], into: unsegmented)
        _ = try BlurayTransportStream.demux(
            try fixture("fastx-clip2", "m2ts"),
            segment: segment(inTime: 54_000_000, duration: 90_090, playlistTime: secondClip),
            into: second
        )

        // The second of the clip before the item is read for nothing, and none of it is delivered.
        XCTAssertEqual(first.video.first?.pts, 0)
        XCTAssertEqual(first.video.first?.isRandomAccess, true)
        XCTAssertTrue(first.video.allSatisfy { $0.pts < 1 << 32 })
        XCTAssertLessThan(first.video.count, unsegmented.video.count)
        XCTAssertFalse(first.audio.isEmpty)
        XCTAssertLessThan(first.audio.count, unsegmented.audio.count)
        XCTAssertTrue(first.audio.allSatisfy { ($0.pts ?? 0) < 1 << 32 })
        XCTAssertEqual(second.video.count, 24)
        XCTAssertEqual(second.video.first?.pts, secondClip)
        XCTAssertEqual(second.video.first?.isRandomAccess, true)
    }

    // MARK: Segment rules

    func testSegmentStartsAtARandomAccessUnitAndDropsWhatCannotDecode() throws {
        var stream = Data()
        // A dependent view whose base view was read before the start.
        stream += M2TS.pes(pid: 0x1012, pts: 100, dts: 100, payload: Self.dependent)
        stream += frame(pts: 3_754, dts: 3_754, Self.pSlice)
        stream += frame(pts: 15_016, dts: 7_508, Self.iSlice)
        // A leading picture: shown before the I picture, predicted from pictures never read.
        stream += frame(pts: 11_262, dts: 11_262, Self.pSlice)
        stream += frame(pts: 18_770, dts: 15_016, Self.pSlice)
        let sink = CollectingPacketSink()

        _ = try BlurayTransportStream.demux(stream, segment: segment(inTime: 0, duration: 90_000, playlistTime: 900_000), into: sink)

        XCTAssertEqual(sink.video.map(\.pts), [915_016, 918_770])
        XCTAssertEqual(sink.video.map(\.isRandomAccess), [true, false])
    }

    func testSegmentThatEndsInsideItsClipStopsAtTheRandomAccessUnitThere() throws {
        var stream = Data()
        for index in 0..<6 {
            let ticks = UInt64(index) * 3_754
            stream += frame(pts: ticks, dts: ticks, index.isMultiple(of: 3) ? Self.iSlice : Self.pSlice)
        }
        stream += M2TS.pes(pid: 0x1100, pts: 3_754, payload: Data(repeating: 1, count: 10))
        stream += M2TS.pes(pid: 0x1100, pts: 11_262, payload: Data(repeating: 2, count: 10))
        let sink = CollectingPacketSink()
        var trimmed = segment(inTime: 0, duration: 11_262, playlistTime: 0)
        trimmed.trim_end = true

        _ = try BlurayTransportStream.demux(stream, segment: trimmed, audioTrackIDs: [0x1100], into: sink)

        XCTAssertEqual(sink.video.map(\.pts), [0, 3_754, 7_508])
    }

    func testAudioOutsideTheSegmentIsDropped() throws {
        var stream = Data()
        stream += M2TS.pes(pid: 0x1100, pts: 1_000, payload: Data(repeating: 1, count: 10))
        stream += frame(pts: 9_000, dts: 9_000, Self.iSlice)
        stream += M2TS.pes(pid: 0x1100, pts: 9_000, payload: Data(repeating: 2, count: 10))
        stream += M2TS.pes(pid: 0x1100, pts: 99_000, payload: Data(repeating: 3, count: 10))
        let sink = CollectingPacketSink()

        _ = try BlurayTransportStream.demux(
            stream, segment: segment(inTime: 9_000, duration: 90_000, playlistTime: 0), audioTrackIDs: [0x1100], into: sink
        )

        XCTAssertEqual(sink.audio.map(\.pts), [0])
        XCTAssertEqual(sink.audio.map(\.payload), [Data(repeating: 2, count: 10)])
    }

    // MARK: Real discs

    func testRealOceanDreamsPlaysAcrossItsClipBoundary() throws {
        try Self.readAcrossBoundary(environment: "BD_TO_AVP_OCEAN_DREAMS_ISO", playlist: 22, boundary: 506_778_772)
    }

    func testRealFastXPlaysAcrossItsClipBoundary() throws {
        try Self.readAcrossBoundary(environment: "BD_TO_AVP_FAST_X_ISO", playlist: 0, boundary: 760_449_690)
    }

    /// Opens a real disc, checks its multi-clip main feature is the title to play, and reads from
    /// five seconds before the boundary to two seconds after it.
    private static func readAcrossBoundary(environment name: String, playlist: UInt32, boundary: UInt64) throws {
        guard let path = ProcessInfo.processInfo.environment[name], !path.isEmpty else {
            throw XCTSkip("Set \(name) to read a real multi-clip Blu-ray 3D disc.")
        }
        let source = try BlurayDiscSource(path: path, container: .discImage)
        let title = try XCTUnwrap(source.info.mainTitle)
        XCTAssertEqual(title.playlist, playlist)
        let sink = CollectingPacketSink()
        sink.stopAfterPTS = boundary + 180_000

        _ = try source.read(
            title: title,
            startingAt: boundary - 450_000,
            audioTrackIDs: title.audioTracks.map(\.id),
            subtitleTrackIDs: [],
            into: sink
        )

        XCTAssertEqual(sink.video.first?.isRandomAccess, true)
        let shown = sink.video.map(\.pts).sorted()
        XCTAssertTrue(shown.contains(boundary))
        XCTAssertEqual(zip(shown, shown.dropFirst()).filter { !frameTicks.contains($1 - $0) }.count, 0)
    }

    // MARK: Helpers

    private struct Entry: Equatable {
        let pts: UInt64
        let spn: UInt32
    }

    private static let iSlice = Data([0, 0, 0, 1, 0x21, 0x88]) + Data(repeating: 0xAB, count: 200)
    private static let pSlice = Data([0, 0, 0, 1, 0x41, 0x9A]) + Data(repeating: 0xCD, count: 40)
    private static let dependent = Data([0, 0, 0, 1, 0x14]) + Data(repeating: 0xEF, count: 100)

    private func entry(_ point: bd3d_entry_point) -> Entry {
        Entry(pts: point.pts, spn: point.spn)
    }

    private func frame(pts: UInt64, dts: UInt64, _ base: Data) -> Data {
        M2TS.pes(pid: 0x1011, pts: pts, dts: dts, payload: base) + M2TS.pes(pid: 0x1012, pts: pts, dts: dts, payload: Self.dependent)
    }

    private func segment(inTime: UInt64, duration: UInt64, playlistTime: UInt64, startOffset: Int64 = .min) -> bd3d_segment {
        bd3d_segment(in_time: inTime, duration: duration, playlist_time: playlistTime, start_offset: startOffset, trim_end: false)
    }

    private func fixture(_ name: String, _ fileExtension: String) throws -> Data {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: name, withExtension: fileExtension))
        return try Data(contentsOf: url)
    }

    private func withClipIndex<T>(_ name: String, pid: UInt16, _ body: (bd3d_clip_index) throws -> T) throws -> T {
        let data = try fixture(name, "clpi")
        var index = bd3d_clip_index()
        let parsed = data.withUnsafeBytes { bytes in
            bd3d_clip_index_parse(bytes.bindMemory(to: UInt8.self).baseAddress, bytes.count, pid, &index)
        }
        guard parsed else {
            XCTFail("\(name) did not parse")
            throw Bluray3DSourceError.titleUnavailable
        }
        defer { bd3d_clip_index_free(&index) }
        return try body(index)
    }

    private func dependentClip(_ name: String, item: UInt32) throws -> String? {
        let data = try fixture(name, "mpls")
        var clip = [CChar](repeating: 0, count: 6)
        let found = data.withUnsafeBytes { bytes in
            bd3d_dependent_clip_id(bytes.bindMemory(to: UInt8.self).baseAddress, bytes.count, item, &clip)
        }
        return found ? String(decoding: clip.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self) : nil
    }
}

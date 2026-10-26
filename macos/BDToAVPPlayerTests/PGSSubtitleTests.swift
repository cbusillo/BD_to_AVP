import CoreMedia
import CoreVideo
import Edge264MVC
import os
import XCTest
@testable import BDToAVPPlayer

/// Builds PGS segments the way a disc codes them.
enum PGS {
    static func segment(_ type: UInt8, _ body: [UInt8]) -> Data {
        Data([type, UInt8(body.count >> 8), UInt8(body.count & 0xFF)] + body)
    }

    static func word(_ value: Int) -> [UInt8] { [UInt8(value >> 8), UInt8(value & 0xFF)] }

    struct Placement {
        var objectID = 0
        var x: Int
        var y: Int
        var crop: PGSRect?
        var isForced = false
    }

    /// A presentation composition segment; state 0x80 starts an epoch, 0x40 is an acquisition
    /// point, 0 a normal update.
    static func composition(state: UInt8 = 0x80, paletteID: UInt8 = 0, _ placements: [Placement]) -> Data {
        var body = word(1920) + word(1080) + [0x10] + word(0) + [state, 0, paletteID, UInt8(placements.count)]
        for placement in placements {
            var flags: UInt8 = placement.isForced ? 0x40 : 0
            if placement.crop != nil { flags |= 0x80 }
            body += word(placement.objectID) + [0, flags] + word(placement.x) + word(placement.y)
            if let crop = placement.crop {
                body += word(crop.x) + word(crop.y) + word(crop.width) + word(crop.height)
            }
        }
        return segment(0x16, body)
    }

    static func window(width: Int = 1920, height: Int = 1080) -> Data {
        segment(0x17, [1, 0] + word(0) + word(0) + word(width) + word(height))
    }

    /// Palette entries as (index, Y, Cb, Cr, alpha); the segment stores Cr before Cb.
    static func palette(id: UInt8 = 0, _ entries: [(UInt8, UInt8, UInt8, UInt8, UInt8)]) -> Data {
        segment(0x14, [id, 0] + entries.flatMap { [$0.0, $0.1, $0.3, $0.2, $0.4] })
    }

    /// One object of `width` x `height` palette indices, coded one pixel per byte with an end of
    /// line after each row (index 0 as a one-pixel run). Split in `fragments` segments.
    static func object(id: Int = 0, width: Int, height: Int, pixels: [UInt8], fragments: Int = 1) -> [Data] {
        var coded: [UInt8] = []
        for row in 0..<height {
            for pixel in pixels[(row * width)..<((row + 1) * width)] {
                coded += pixel == 0 ? [0, 1] : [pixel]
            }
            coded += [0, 0]
        }
        let length = coded.count + 4
        let size = (coded.count + fragments - 1) / fragments
        return (0..<fragments).map { index in
            let part = Array(coded[min(coded.count, index * size)..<min(coded.count, (index + 1) * size)])
            var sequence: UInt8 = 0
            if index == 0 { sequence |= 0x80 }
            if index == fragments - 1 { sequence |= 0x40 }
            var body = word(id) + [0, sequence]
            if index == 0 {
                body += [UInt8(length >> 16), UInt8((length >> 8) & 0xFF), UInt8(length & 0xFF)] + word(width) + word(height)
            }
            return segment(0x15, body + part)
        }
    }

    static let end = segment(0x80, [])

    /// A display set that shows one opaque `width` x `height` block of index 1 at (x, y).
    static func block(x: Int, y: Int, width: Int, height: Int, luma: UInt8 = 235) -> [Data] {
        [composition([Placement(x: x, y: y)]), window(), palette([(1, luma, 128, 128, 255)])]
            + object(width: width, height: height, pixels: Array(repeating: 1, count: width * height))
            + [end]
    }

    /// A display set that clears the screen.
    static let clear = [composition(state: 0, []), window(), end]

    /// Segments as the disc delivers them, one PES packet each, all timed `pts`.
    static func packets(_ segments: [Data], pts: UInt64, pid: UInt16 = 0x1200) -> [Bluray3DPacket] {
        segments.map { .subtitle(BluRayPESPacket(pid: pid, streamID: 0xBD, streamIDExtension: nil, pts: pts, payload: $0)) }
    }
}

final class PGSSubtitleTests: XCTestCase {
    private static let second: Int64 = 90_000

    // MARK: Parsing

    /// A real English display set and the one that clears it, cut from a Blu-ray 3D remux's PGS
    /// track as a .sup file: each segment follows a "PG" header with its PTS and DTS.
    private func realSegments() throws -> [(pts: UInt64, segment: Data)] {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "residentevil-pgs-two-display-sets", withExtension: "sup"))
        let bytes = [UInt8](try Data(contentsOf: url))
        var segments: [(UInt64, Data)] = []
        var offset = 0
        while offset + 13 <= bytes.count {
            XCTAssertEqual(bytes[offset..<(offset + 2)], [0x50, 0x47])
            let pts = bytes[(offset + 2)..<(offset + 6)].reduce(UInt64(0)) { $0 << 8 | UInt64($1) }
            let length = Int(bytes[offset + 11]) << 8 | Int(bytes[offset + 12])
            segments.append((pts, Data(bytes[(offset + 10)..<(offset + 13 + length)])))
            offset += 13 + length
        }
        return segments
    }

    func testARealDisplaySetDecodesWhetherSegmentsComeOneByOneOrTogether() throws {
        let segments = try realSegments()
        // A disc sends each segment in its own PES packet; Matroska keeps a display set together.
        var separate = PGSDecoder()
        let fromDisc = segments.flatMap { separate.receive($0.segment, ticks: Int64($0.pts)) }
        var together = PGSDecoder()
        let fromMatroska = [segments[0..<5], segments[5...]].flatMap { set in
            together.receive(set.reduce(Data()) { $0 + $1.segment }, ticks: Int64(set.first?.pts ?? 0))
        }

        XCTAssertEqual(fromDisc, fromMatroska)
        XCTAssertEqual(fromDisc.map(\.ticks), [173_790, 357_750])
        let shown = try XCTUnwrap(fromDisc.first)
        XCTAssertEqual([shown.videoWidth, shown.videoHeight], [1920, 1080])
        let object = try XCTUnwrap(shown.objects.first)
        XCTAssertEqual(shown.objects.count, 1)
        XCTAssertEqual([object.x, object.y, object.width, object.height], [727, 870, 469, 63])
        XCTAssertEqual(object.crop, PGSRect(x: 0, y: 0, width: 469, height: 63))
        XCTAssertFalse(object.isForced)
        // "My name is Alice." covers this many pixels, as an independent decoder counts them.
        let pixels = PGSRunLength.decode(object.runLengthData, width: object.width, height: object.height)
        XCTAssertEqual(pixels.filter { shown.palette[Int($0)].alpha > 0 }.count, 14_631)
        XCTAssertTrue(fromDisc[1].isEmpty)
    }

    func testRunLengthCodesEveryRunForm() {
        let runs: [[UInt8]] = [
            [0x07],                   // one pixel of index 7
            [0x00, 0x82, 0x09],       // two pixels of index 9
            [0x00, 0x02],             // two transparent pixels
            [0x00, 0xC0, 0x03, 0x04], // three pixels of index 4, long form
            [0x00, 0x00],             // end of line
            [0x00, 0x40, 0x08],       // eight transparent pixels, long form
            [0x00, 0x00],
            [0x05, 0x05],             // a short line is padded
            [0x00, 0x00],
        ]
        let data = Data(runs.joined())
        XCTAssertEqual(
            PGSRunLength.decode(data, width: 8, height: 3),
            [7, 9, 9, 0, 0, 4, 4, 4] + Array(repeating: 0, count: 8) + [5, 5, 0, 0, 0, 0, 0, 0]
        )
        // A line that runs long is cut at the object's width and the next starts on time.
        XCTAssertEqual(PGSRunLength.decode(Data([0x00, 0x8A, 0x03, 0x00, 0x00, 0x02]), width: 4, height: 2), [3, 3, 3, 3, 2, 0, 0, 0])
    }

    func testPaletteReadsCrBeforeCbAndLeavesUndefinedEntriesTransparent() throws {
        var decoder = PGSDecoder()
        let displays = PGS.block(x: 0, y: 0, width: 1, height: 1).dropLast().flatMap { decoder.receive($0, ticks: 0) }
            + decoder.receive(PGS.palette([(1, 81, 90, 240, 200)]), ticks: nil)
            + decoder.receive(PGS.end, ticks: nil)
        let display = try XCTUnwrap(displays.last)
        XCTAssertEqual(display.palette[1], PGSPaletteEntry(y: 81, cb: 90, cr: 240, alpha: 200))
        XCTAssertEqual(display.palette[2], .transparent)
    }

    func testObjectsSpanFragmentsAndKeepTheirCropAndForcedFlag() throws {
        let pixels: [UInt8] = (0..<24).map { UInt8($0 % 3) }
        var decoder = PGSDecoder()
        let crop = PGSRect(x: 2, y: 1, width: 3, height: 2)
        let segments = [PGS.composition([PGS.Placement(objectID: 3, x: 100, y: 900, crop: crop, isForced: true)]), PGS.window()]
            + PGS.object(id: 3, width: 6, height: 4, pixels: pixels, fragments: 3) + [PGS.end]
        let display = try XCTUnwrap(segments.flatMap { decoder.receive($0, ticks: 0) }.first)
        let object = try XCTUnwrap(display.objects.first)
        XCTAssertEqual(object.crop, crop)
        XCTAssertTrue(object.isForced)
        XCTAssertEqual(PGSRunLength.decode(object.runLengthData, width: 6, height: 4), pixels)
    }

    func testAnUpdateAfterAResetShowsOnlyTheObjectsItCarries() throws {
        var decoder = PGSDecoder()
        _ = PGS.block(x: 10, y: 10, width: 2, height: 2).flatMap { decoder.receive($0, ticks: 0) }
        // A normal update that moves the object keeps it within the epoch.
        let move = [PGS.composition(state: 0, [PGS.Placement(x: 20, y: 10)]), PGS.end]
        XCTAssertEqual(move.flatMap { decoder.receive($0, ticks: 90) }.first?.objects.map(\.x), [20])

        // After a reset, as for a read that starts elsewhere, the same update has nothing to move.
        decoder.reset()
        XCTAssertEqual(move.flatMap { decoder.receive($0, ticks: 180) }.first?.objects, [])
        // An epoch start drops what came before.
        _ = PGS.block(x: 10, y: 10, width: 2, height: 2).flatMap { decoder.receive($0, ticks: 270) }
        let fresh = [PGS.composition([PGS.Placement(x: 20, y: 10)]), PGS.end]
        XCTAssertEqual(fresh.flatMap { decoder.receive($0, ticks: 360) }.first?.objects, [])
    }

    func testDamagedObjectsAreLeftOutRatherThanExpanded() throws {
        func objects(_ segments: [Data]) -> [PGSDisplayObject]? {
            var decoder = PGSDecoder()
            return segments.flatMap { decoder.receive($0, ticks: 0) }.first?.objects
        }
        // Wider than the 1920-pixel frame.
        XCTAssertEqual(objects(
            [PGS.composition([PGS.Placement(x: 0, y: 0)])] + PGS.object(width: 2000, height: 1, pixels: Array(repeating: 1, count: 2000))
                + [PGS.end]
        ), [])
        // A crop that misses the object.
        XCTAssertEqual(objects(
            [PGS.composition([PGS.Placement(x: 0, y: 0, crop: PGSRect(x: 8, y: 0, width: 2, height: 1))])]
                + PGS.object(width: 4, height: 1, pixels: [1, 1, 1, 1]) + [PGS.end]
        ), [])
        // Fragments that run past the declared length keep only what was declared.
        var fragments = PGS.object(width: 2, height: 1, pixels: [1, 1], fragments: 2)
        fragments[1] += Data(repeating: 0x07, count: 50)
        let object = try XCTUnwrap(objects([PGS.composition([PGS.Placement(x: 0, y: 0)])] + fragments + [PGS.end])?.first)
        XCTAssertEqual(object.runLengthData, Data([1, 1, 0, 0]))
    }

    func testTracksOfferOnlyPGSWithTheirLanguages() {
        let tracks = [
            Bluray3DTrack(id: 0x1200, kind: .subtitle, codingType: 0x90, language: "eng", name: nil),
            Bluray3DTrack(id: 0x1201, kind: .subtitle, codingType: 0x92, language: "eng", name: nil),
            Bluray3DTrack(id: 0x1202, kind: .subtitle, codingType: 0x90, language: "fra", name: nil),
            Bluray3DTrack(id: 0x1203, kind: .subtitle, codingType: 0x90, language: "fra", name: "SDH"),
        ]
        let offered = PGSSubtitleTrack.tracks(for: tracks, locale: Locale(identifier: "en_US"))
        XCTAssertEqual(offered.map(\.id), [0x1200, 0x1202, 0x1203])
        XCTAssertEqual(offered.map(\.label), ["English", "French — Track 2", "French — SDH — Track 3"])
        XCTAssertEqual(Set(offered.map(\.mediaOption.id)).count, 3)
    }

    // MARK: Timing

    func testDisplaysFollowTheVideoTimelineAndShowUntilTheNextOne() {
        let buffer = ReadAheadBuffer()
        XCTAssertTrue(buffer.receive(.video(MVCAccessUnit(pts: 0, dts: nil, isRandomAccess: true, data: Data(count: 10)))))
        for packet in PGS.packets(PGS.block(x: 0, y: 0, width: 2, height: 2), pts: UInt64(2 * Self.second))
            + PGS.packets(PGS.clear, pts: UInt64(4 * Self.second))
            + PGS.packets(PGS.block(x: 0, y: 0, width: 2, height: 2), pts: UInt64(6 * Self.second), pid: 0x1201) {
            XCTAssertTrue(buffer.receive(packet))
        }

        XCTAssertNil(buffer.subtitleDisplay(pid: 0x1200, at: 2 * Self.second - 1))
        XCTAssertEqual(buffer.subtitleDisplay(pid: 0x1200, at: 2 * Self.second)?.display.objects.count, 1)
        XCTAssertEqual(buffer.subtitleDisplay(pid: 0x1200, at: 4 * Self.second - 1)?.display.objects.count, 1)
        XCTAssertEqual(buffer.subtitleDisplay(pid: 0x1200, at: 4 * Self.second)?.display.isEmpty, true)
        // Every track is kept, so switching shows the new one's current display at once.
        XCTAssertNil(buffer.subtitleDisplay(pid: 0x1201, at: 5 * Self.second))
        XCTAssertEqual(buffer.subtitleDisplay(pid: 0x1201, at: 7 * Self.second)?.display.objects.count, 1)
    }

    func testDisplaysKeepCountingAcrossThe33BitWrap() {
        let buffer = ReadAheadBuffer()
        let wrap = Int64(1) << 33
        // A read that starts a second before the counter wraps.
        buffer.restart(at: wrap - Self.second)
        XCTAssertTrue(buffer.receive(.video(MVCAccessUnit(
            pts: UInt64(wrap - Self.second), dts: nil, isRandomAccess: true, data: Data(count: 10)
        ))))
        for packet in PGS.packets(PGS.block(x: 0, y: 0, width: 2, height: 2), pts: UInt64(Self.second)) {
            XCTAssertTrue(buffer.receive(packet))
        }
        XCTAssertNil(buffer.subtitleDisplay(pid: 0x1200, at: wrap))
        XCTAssertEqual(buffer.subtitleDisplay(pid: 0x1200, at: wrap + Self.second)?.display.ticks, wrap + Self.second)
    }

    func testASeekInsideTheBufferKeepsDisplaysAndANewReadStartsAfresh() {
        let buffer = ReadAheadBuffer()
        for second in 0..<10 {
            XCTAssertTrue(buffer.receive(.video(MVCAccessUnit(
                pts: UInt64(Int64(second) * Self.second), dts: nil, isRandomAccess: second % 4 == 0, data: Data(count: 10)
            ))))
            if second == 3 {
                PGS.packets(PGS.block(x: 0, y: 0, width: 2, height: 2), pts: UInt64(3 * Self.second)).forEach { _ = buffer.receive($0) }
            }
        }
        for _ in 0..<8 { _ = buffer.nextVideo(timeout: 1) }

        XCTAssertEqual(buffer.seek(to: 3 * Self.second), .moved(startTicks: 0))
        XCTAssertEqual(buffer.subtitleDisplay(pid: 0x1200, at: 3 * Self.second)?.display.ticks, 3 * Self.second)

        // A read that starts elsewhere drops what the old one decoded; the update that follows
        // refers to an object from before it, so it shows nothing rather than something stale.
        buffer.restart(at: 3 * Self.second)
        XCTAssertNil(buffer.subtitleDisplay(pid: 0x1200, at: 3 * Self.second))
        let update = [PGS.composition(state: 0, [PGS.Placement(x: 4, y: 0)]), PGS.end]
        PGS.packets(update, pts: UInt64(4 * Self.second)).forEach { _ = buffer.receive($0) }
        XCTAssertEqual(buffer.subtitleDisplay(pid: 0x1200, at: 4 * Self.second)?.display.isEmpty, true)
    }

    func testDisplaysBehindTheKeptVideoAreDroppedButTheCurrentOneStays() {
        let buffer = ReadAheadBuffer(limits: .init(aheadTarget: 100, behindLimit: 2, maximumBytes: 1 << 20))
        for second in 0..<12 {
            XCTAssertTrue(buffer.receive(.video(MVCAccessUnit(
                pts: UInt64(Int64(second) * Self.second), dts: nil, isRandomAccess: second % 4 == 0, data: Data(count: 10)
            ))))
            if second == 1 || second == 2 {
                PGS.packets(PGS.block(x: second, y: 0, width: 2, height: 2), pts: UInt64(Int64(second) * Self.second))
                    .forEach { _ = buffer.receive($0) }
            }
        }
        // The decoder at 8 s keeps video from the IDR at 4 s; the display from 2 s still shows there.
        for _ in 0..<9 { _ = buffer.nextVideo(timeout: 1) }
        XCTAssertEqual(buffer.loadedTicks?.lowerBound, 4 * Self.second)
        XCTAssertEqual(buffer.subtitleDisplay(pid: 0x1200, at: 4 * Self.second)?.display.ticks, 2 * Self.second)
        XCTAssertNil(buffer.subtitleDisplay(pid: 0x1200, at: Self.second + 1))
    }

    // MARK: Drawing

    private func frameBuffer(width: Int, height: Int, luma: UInt8 = 16) throws -> CVPixelBuffer {
        var created: CVPixelBuffer?
        CVPixelBufferCreate(nil, width, height, kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange, nil, &created)
        let buffer = try XCTUnwrap(created)
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        for plane in 0..<2 {
            let base = try XCTUnwrap(CVPixelBufferGetBaseAddressOfPlane(buffer, plane))
            memset(base, Int32(plane == 0 ? luma : 128), CVPixelBufferGetBytesPerRowOfPlane(buffer, plane) * CVPixelBufferGetHeightOfPlane(buffer, plane))
        }
        return buffer
    }

    private func lumaRow(_ buffer: CVPixelBuffer, _ row: Int) -> [UInt8] {
        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        let base = CVPixelBufferGetBaseAddressOfPlane(buffer, 0)!.assumingMemoryBound(to: UInt8.self)
        let line = base + row * CVPixelBufferGetBytesPerRowOfPlane(buffer, 0)
        return Array(UnsafeBufferPointer(start: line, count: CVPixelBufferGetWidth(buffer)))
    }

    private func chroma(_ buffer: CVPixelBuffer, column: Int, row: Int) -> [UInt8] {
        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        let base = CVPixelBufferGetBaseAddressOfPlane(buffer, 1)!.assumingMemoryBound(to: UInt8.self)
        let line = base + row * CVPixelBufferGetBytesPerRowOfPlane(buffer, 1)
        return [line[column * 2], line[column * 2 + 1]]
    }

    private func overlay(_ segments: [Data]) throws -> SubtitleOverlay {
        var decoder = PGSDecoder()
        return try XCTUnwrap(SubtitleOverlay(try XCTUnwrap(segments.flatMap { decoder.receive($0, ticks: 0) }.first)))
    }

    /// The left eye's copy sits right of the right eye's: crossed disparity, so the subtitle
    /// appears in front of the video's screen plane rather than behind it.
    func testEachEyeGetsItsCopyShiftedSoTheSubtitleStandsInFront() throws {
        let subtitle = try overlay(PGS.block(x: 20, y: 2, width: 4, height: 2))
        let left = try frameBuffer(width: 48, height: 8)
        let right = try frameBuffer(width: 48, height: 8)
        CVPixelBufferLockBaseAddress(left, [])
        subtitle.draw(into: left, eye: .left)
        CVPixelBufferUnlockBaseAddress(left, [])
        CVPixelBufferLockBaseAddress(right, [])
        subtitle.draw(into: right, eye: .right)
        CVPixelBufferUnlockBaseAddress(right, [])

        func drawnColumns(_ buffer: CVPixelBuffer) -> [Int] {
            lumaRow(buffer, 2).enumerated().filter { $0.element == 235 }.map(\.offset)
        }
        let leftColumns = drawnColumns(left)
        let rightColumns = drawnColumns(right)
        XCTAssertGreaterThan(SubtitleOverlay.eyeShift, 0)
        XCTAssertEqual(leftColumns, Array((20 + SubtitleOverlay.eyeShift)..<(24 + SubtitleOverlay.eyeShift)))
        XCTAssertEqual(rightColumns, Array((20 - SubtitleOverlay.eyeShift)..<(24 - SubtitleOverlay.eyeShift)))
        XCTAssertGreaterThan(leftColumns.first ?? 0, rightColumns.first ?? .max, "left-eye copy must sit right of the right-eye copy")
        XCTAssertEqual(lumaRow(left, 1), Array(repeating: 16, count: 48))
        XCTAssertEqual(lumaRow(left, 4), Array(repeating: 16, count: 48))
    }

    func testTranslucentPixelsBlendAndChromaFollowsTheCoveredShare() throws {
        // A 1x1 object half opaque, Cb 240 and Cr 16, at an even position so one chroma sample
        // covers it with three uncovered pixels.
        let segments = [PGS.composition([PGS.Placement(x: 10, y: 2)]), PGS.window(), PGS.palette([(1, 216, 240, 16, 128)])]
            + PGS.object(width: 1, height: 1, pixels: [1]) + [PGS.end]
        let subtitle = try overlay(segments)
        let frame = try frameBuffer(width: 32, height: 8)
        CVPixelBufferLockBaseAddress(frame, [])
        subtitle.draw(into: frame, eye: .right)
        CVPixelBufferUnlockBaseAddress(frame, [])
        let column = 10 - SubtitleOverlay.eyeShift
        // 16 + (216 - 16) * 128 / 255, rounded.
        XCTAssertEqual(lumaRow(frame, 2)[column], 116)
        // One of four pixels at half opacity moves chroma an eighth of the way.
        XCTAssertEqual(chroma(frame, column: column / 2, row: 1), [142, 114])
    }

    func testAnObjectPartlyOffTheDiscsFrameIsCutBeforeDrawing() throws {
        let subtitle = try overlay(PGS.block(x: 1918, y: 1078, width: 4, height: 4))
        XCTAssertEqual(subtitle.bounds, PGSRect(x: 1918, y: 1078, width: 2, height: 2))
    }

    func testDrawingPastTheFrameEdgeIsCut() throws {
        // Four rows from row 6 of an eight-row frame; the left eye's copy runs off the right edge.
        let x = 16 - SubtitleOverlay.eyeShift - 2
        let subtitle = try overlay(PGS.block(x: x, y: 6, width: 4, height: 4))
        let left = try frameBuffer(width: 16, height: 8)
        let right = try frameBuffer(width: 16, height: 8)
        CVPixelBufferLockBaseAddress(left, [])
        subtitle.draw(into: left, eye: .left)
        CVPixelBufferUnlockBaseAddress(left, [])
        CVPixelBufferLockBaseAddress(right, [])
        subtitle.draw(into: right, eye: .right)
        CVPixelBufferUnlockBaseAddress(right, [])

        XCTAssertEqual(lumaRow(left, 7), Array(repeating: 16, count: 14) + [235, 235])
        let rightStart = x - SubtitleOverlay.eyeShift
        var expected = [UInt8](repeating: 16, count: 16)
        expected.replaceSubrange(rightStart..<(rightStart + 4), with: [235, 235, 235, 235])
        XCTAssertEqual(lumaRow(right, 7), expected)
        XCTAssertEqual(lumaRow(right, 5), Array(repeating: 16, count: 16))
    }

    func testTheFactoryDrawsTheSubtitleIntoBothEyesForEitherBaseView() throws {
        let subtitle = try overlay(PGS.block(x: 16, y: 0, width: 2, height: 2))
        let width = 48
        let height = 4
        let lumaPlane = [UInt8](repeating: 16, count: width * height)
        let chromaPlane = [UInt8](repeating: 128, count: width * height / 4)
        for baseViewIsRightEye in [false, true] {
            let factory = StereoSampleBufferFactory(baseViewIsRightEye: baseViewIsRightEye)
            let sample = try lumaPlane.withUnsafeBufferPointer { luma in
                try chromaPlane.withUnsafeBufferPointer { chroma -> CMSampleBuffer in
                    var frame = BDAVPMVCFrame()
                    let planes = (luma.baseAddress, chroma.baseAddress, chroma.baseAddress)
                    frame.planes = (planes, planes)
                    frame.width = Int32(width)
                    frame.height = Int32(height)
                    frame.chroma_width = Int32(width / 2)
                    frame.chroma_height = Int32(height / 2)
                    frame.luma_stride = Int32(width)
                    frame.chroma_stride = Int32(width / 2)
                    return try withUnsafePointer(to: &frame) {
                        try factory.sampleBuffer(for: $0, presentationTime: .zero, subtitle: subtitle)
                    }
                }
            }
            let eyes = try Dictionary(uniqueKeysWithValues: XCTUnwrap(sample.taggedBuffers).map { tagged -> (String, Int?) in
                guard case let .pixelBuffer(pixels) = tagged.buffer else { throw XCTSkip("not a pixel buffer") }
                let eye = tagged.tags.contains(.stereoView(.leftEye)) ? "left" : "right"
                return (eye, lumaRow(pixels, 0).firstIndex(of: 235))
            })
            XCTAssertEqual(eyes, ["left": 16 + SubtitleOverlay.eyeShift, "right": 16 - SubtitleOverlay.eyeShift])
        }
    }

    // MARK: Player

    private final class SubtitledSource: Bluray3DSource {
        let info: Bluray3DSourceInfo
        private let requested = OSAllocatedUnfairLock(initialState: [[Int]]())
        var requestedSubtitleTracks: [[Int]] { requested.withLock { $0 } }

        init() {
            info = Bluray3DSourceInfo(container: .discImage, titles: [Bluray3DTitle(
                id: "playlist:00001", playlist: 1, durationSeconds: 60, isMainFeature: true, baseViewIsRightEye: false,
                unsupportedReason: nil, audioTracks: [],
                subtitleTracks: [
                    Bluray3DTrack(id: 0x1200, kind: .subtitle, codingType: 0x90, language: "eng", name: nil),
                    Bluray3DTrack(id: 0x1201, kind: .subtitle, codingType: 0x92, language: "eng", name: nil),
                    Bluray3DTrack(id: 0x1202, kind: .subtitle, codingType: 0x90, language: "deu", name: nil),
                ]
            )])
        }

        func read(
            title: Bluray3DTitle, from startTicks: Int64, audioTrackIDs: [Int], subtitleTrackIDs: [Int],
            into sink: Bluray3DPacketSink
        ) throws -> Bluray3DReadStatistics {
            requested.withLock { $0.append(subtitleTrackIDs) }
            return Bluray3DReadStatistics()
        }
    }

    @MainActor
    func testTheSubtitleListOffersPGSTracksAndTheChoiceReachesPlayback() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("PGSSubtitleTests-\(UUID().uuidString)", isDirectory: true)
        let movieURL = directory.appendingPathComponent("Movie.iso")
        let bookmarkStore = BookmarkStore(
            storageURL: directory.appendingPathComponent("bookmarks.json"),
            resolveBookmark: { _ in (movieURL, false) },
            makeLease: { SecurityScopedResourceLease(url: $0, startAccessing: { true }, stopAccessing: {}) },
            resourceExists: { _ in true }
        )
        let item = MediaItem(id: "subtitled", title: "Movie", fileName: "Movie.iso", format: .bluray3D)
        try bookmarkStore.save(bookmarkData: Data([1]), for: item.id)
        let resumeStore = ResumeStore(storageURL: directory.appendingPathComponent("resume.json"))
        let source = SubtitledSource()
        let session = MVHEVCPlayerSession()
        session.nativeSourceOpener = { _, _ in source }

        await session.prepare(mediaItem: item, bookmarkStore: bookmarkStore, resumeStore: resumeStore)

        XCTAssertEqual(session.subtitleOptions.first?.id, "off")
        XCTAssertEqual(session.subtitleOptions.count, 3)
        XCTAssertEqual(session.selectedSubtitleID, "off")
        XCTAssertNil(session.subtitleListNote)
        let german = try XCTUnwrap(session.subtitleOptions.last)
        session.selectSubtitle(id: german.id)
        XCTAssertEqual(session.selectedSubtitleID, german.id)
        XCTAssertEqual(session.nativeSubtitleTrackID, 0x1202)
        session.selectSubtitle(id: "off")
        XCTAssertEqual(session.selectedSubtitleID, "off")
        XCTAssertNil(session.nativeSubtitleTrackID)
        // Every PGS track is read so a switch shows at once; the text track is not.
        let deadline = Date(timeIntervalSinceNow: 5)
        while source.requestedSubtitleTracks.isEmpty, Date() < deadline {
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertEqual(source.requestedSubtitleTracks.first, [0x1200, 0x1202])
        session.finish()
    }

    /// Reads a minute of a real source's PGS tracks from BD_TO_AVP_PGS_START seconds and checks
    /// that each shows something it can draw. The default test run leaves BD_TO_AVP_PGS_SOURCE
    /// (an ISO, disc folder or MKV) unset.
    func testARealSourcesSubtitlesDecodeAndDraw() throws {
        let environment = ProcessInfo.processInfo.environment
        guard let path = environment["BD_TO_AVP_PGS_SOURCE"], !path.isEmpty else {
            throw XCTSkip("Set BD_TO_AVP_PGS_SOURCE to read a real source's subtitles.")
        }
        let source = try Bluray3DSourceOpener.open(URL(fileURLWithPath: path))
        let title = try XCTUnwrap(source.info.mainTitle)
        let tracks = PGSSubtitleTrack.tracks(for: title.subtitleTracks)
        XCTAssertFalse(tracks.isEmpty)
        let start = Int64((Double(environment["BD_TO_AVP_PGS_START"] ?? "") ?? 0) * 90_000)
        let buffer = ReadAheadBuffer(limits: .init(aheadTarget: 120, behindLimit: 120, maximumBytes: 1 << 30))
        buffer.restart(at: start)
        final class Minute: Bluray3DPacketSink {
            let buffer: ReadAheadBuffer
            let end: UInt64
            init(buffer: ReadAheadBuffer, end: UInt64) {
                self.buffer = buffer
                self.end = end
            }

            func receive(_ packet: Bluray3DPacket) -> Bool {
                if case let .video(unit) = packet, unit.pts > end { return false }
                return buffer.receive(packet)
            }
        }
        let sink = Minute(buffer: buffer, end: UInt64(start + 60 * 90_000))
        _ = try source.read(title: title, from: start, audioTrackIDs: [], subtitleTrackIDs: tracks.map { Int($0.id) }, into: sink)

        let shown = tracks.compactMap { track -> SubtitleOverlay? in
            stride(from: start, to: start + 60 * 90_000, by: 9_000).lazy
                .compactMap { buffer.subtitleDisplay(pid: track.id, at: $0).flatMap { SubtitleOverlay($0.display) } }
                .first
        }
        // A track of forced captions alone can be silent for a minute; the rest are not.
        XCTAssertFalse(shown.isEmpty)
        print("PGS tracks showing within the minute: \(shown.count) of \(tracks.count)")
        for overlay in shown {
            XCTAssertTrue(overlay.bounds.width > 0 && overlay.bounds.x >= 0 && overlay.bounds.x + overlay.bounds.width <= 1920)
            XCTAssertTrue(overlay.bounds.y >= 0 && overlay.bounds.y + overlay.bounds.height <= 1080)
        }
    }
}

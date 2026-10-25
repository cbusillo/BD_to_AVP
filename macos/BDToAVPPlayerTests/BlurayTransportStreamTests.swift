import XCTest
@testable import BDToAVPPlayer

final class CollectingPacketSink: Bluray3DPacketSink {
    var packets: [Bluray3DPacket] = []
    /// Stops the reader once this many video access units have arrived.
    var videoLimit = Int.max
    /// Stops the reader at the first video access unit timed after this.
    var stopAfterPTS = UInt64.max

    var video: [MVCAccessUnit] {
        packets.compactMap { if case let .video(unit) = $0 { return unit } else { return nil } }
    }

    var audio: [BluRayPESPacket] {
        packets.compactMap { if case let .audio(packet) = $0 { return packet } else { return nil } }
    }

    var subtitles: [BluRayPESPacket] {
        packets.compactMap { if case let .subtitle(packet) = $0 { return packet } else { return nil } }
    }

    func receive(_ packet: Bluray3DPacket) -> Bool {
        if case let .video(unit) = packet, unit.pts > stopAfterPTS { return false }
        packets.append(packet)
        return video.count < videoLimit
    }
}

final class BlurayTransportStreamTests: XCTestCase {
    private let idr = Data([0, 0, 0, 1, 0x65]) + Data(repeating: 0xAB, count: 300)
    private let slice = Data([0, 0, 0, 1, 0x41]) + Data(repeating: 0xCD, count: 40)
    private let dependent = Data([0, 0, 0, 1, 0x14]) + Data(repeating: 0xEF, count: 250)

    func testPairsBaseAndDependentViewsAndSelectsTracks() throws {
        var stream = Data()
        stream += M2TS.pes(pid: 0x1011, pts: 9000, dts: 6000, payload: idr)
        stream += M2TS.pes(pid: 0x1100, pts: 9100, payload: Data(repeating: 1, count: 500))
        stream += M2TS.pes(pid: 0x1101, pts: 9100, payload: Data(repeating: 2, count: 100))
        stream += M2TS.pes(pid: 0x1012, pts: 9000, dts: 6000, payload: dependent)
        // The dependent view of the next frame arrives before its base view.
        stream += M2TS.pes(pid: 0x1012, pts: 12754, dts: 9754, payload: dependent)
        stream += M2TS.pes(pid: 0x1200, pts: 9200, payload: Data(repeating: 3, count: 20))
        stream += M2TS.pes(pid: 0x1011, pts: 12754, dts: 9754, payload: slice)
        let sink = CollectingPacketSink()

        let statistics = try BlurayTransportStream.demux(
            stream, audioTrackIDs: [0x1100], subtitleTrackIDs: [0x1200], chunkSize: 1000, into: sink
        )

        XCTAssertEqual(sink.video.map(\.pts), [9000, 12754])
        XCTAssertEqual(sink.video.map(\.dts), [6000, 9754])
        XCTAssertEqual(sink.video.map(\.isRandomAccess), [true, false])
        XCTAssertEqual(sink.video.map(\.data), [idr + dependent, slice + dependent])
        XCTAssertEqual(
            sink.audio,
            [BluRayPESPacket(pid: 0x1100, streamID: 0xBD, streamIDExtension: nil, pts: 9100, payload: Data(repeating: 1, count: 500))]
        )
        XCTAssertEqual(sink.subtitles.map(\.pts), [9200])
        XCTAssertEqual(statistics.videoAccessUnits, 2)
        XCTAssertEqual(statistics.bytes, UInt64(stream.count))
    }

    func testSinkCanStopReading() throws {
        var stream = Data()
        for frame in 0..<5 {
            let time = UInt64(frame) * 3754
            stream += M2TS.pes(pid: 0x1011, pts: time, dts: time, payload: slice)
            stream += M2TS.pes(pid: 0x1012, pts: time, dts: time, payload: dependent)
        }
        let sink = CollectingPacketSink()
        sink.videoLimit = 2

        _ = try BlurayTransportStream.demux(stream, into: sink)

        XCTAssertEqual(sink.video.count, 2)
    }

    func testDecodeTimesMayWrapPastThe33BitLimit() throws {
        let sink = CollectingPacketSink()

        _ = try BlurayTransportStream.demux(frames(decodeTimes: [8_589_934_000, 3_162]), into: sink)

        XCTAssertEqual(sink.video.map(\.dts), [8_589_934_000, 3_162])
    }

    func testDecodeTimesThatGoBackwardAreAnError() {
        XCTAssertThrowsError(try BlurayTransportStream.demux(frames(decodeTimes: [9_000, 3_000]), into: CollectingPacketSink())) {
            XCTAssertEqual($0 as? Bluray3DSourceError, .invalidStream("video decode times go backward"))
        }
    }

    private func frames(decodeTimes: [UInt64]) -> Data {
        decodeTimes.reduce(Data()) { stream, time in
            stream + M2TS.pes(pid: 0x1011, pts: time, dts: time, payload: slice)
                + M2TS.pes(pid: 0x1012, pts: time, dts: time, payload: dependent)
        }
    }

    func testABaseViewWithoutDependentViewIsAnError() {
        let stream = M2TS.pes(pid: 0x1011, pts: 9000, dts: 9000, payload: idr)

        XCTAssertThrowsError(try BlurayTransportStream.demux(stream, into: CollectingPacketSink())) { error in
            XCTAssertEqual(error as? Bluray3DSourceError, .invalidStream("a base view has no dependent view"))
        }
    }

    func testRealDiscImageSelectsTheMainTitleAndDeliversStereoFrames() throws {
        try RealBluraySource.read(environment: "BD_TO_AVP_RAINFOREST_ISO") {
            try BlurayDiscSource(path: $0.path, container: .discImage)
        }
    }
}

/// Reads real sources named by environment variables, which the default test run leaves unset.
enum RealBluraySource {
    /// Reads 240 stereo frames of a real source. With BD_TO_AVP_BLURAY3D_DUMP_DIR set, it also
    /// writes them as an Annex B file, for checking with the Mac's edge264_test.
    static func read(environment name: String, open: (URL) throws -> Bluray3DSource) throws {
        let environment = ProcessInfo.processInfo.environment
        guard let path = environment[name], !path.isEmpty else { throw XCTSkip("Set \(name) to read a real Blu-ray 3D source.") }
        let source = try open(URL(fileURLWithPath: path))
        let title = try XCTUnwrap(source.info.mainTitle)
        let sink = CollectingPacketSink()
        sink.videoLimit = 240

        _ = try source.read(
            title: title,
            audioTrackIDs: title.audioTracks.map(\.id),
            subtitleTrackIDs: title.subtitleTracks.map(\.id),
            into: sink
        )

        XCTAssertTrue(title.durationSeconds > 600)
        XCTAssertFalse(title.audioTracks.isEmpty)
        XCTAssertEqual(sink.video.first?.isRandomAccess, true)
        XCTAssertEqual(sink.video.count, 240)
        XCTAssertTrue(sink.video.allSatisfy { $0.data.contains(0x14) })
        XCTAssertEqual(Set(sink.audio.map(\.pid)), Set(title.audioStreams.map(\.pid)))
        if let directory = environment["BD_TO_AVP_BLURAY3D_DUMP_DIR"] {
            let output = URL(fileURLWithPath: directory).appendingPathComponent("\(name).264")
            try sink.video.reduce(Data()) { $0 + $1.data }.write(to: output)
        }
    }
}

/// Builds Blu-ray M2TS packets: a 4-byte arrival timestamp header and a 188-byte TS packet.
enum M2TS {
    static func pes(pid: UInt16, pts: UInt64, dts: UInt64? = nil, payload: Data) -> Data {
        var header = Data([0, 0, 1, pid < 0x1100 ? 0xE0 : 0xBD, 0, 0, 0x80])
        header.append(dts == nil ? 0x80 : 0xC0)
        header.append(dts == nil ? 5 : 10)
        header += timestamp(pts, marker: dts == nil ? 0x21 : 0x31)
        if let dts { header += timestamp(dts, marker: 0x11) }
        var remaining = header + payload
        var output = Data()
        var first = true
        while !remaining.isEmpty {
            let chunk = remaining.prefix(184)
            remaining = remaining.dropFirst(chunk.count)
            var packet = Data([0, 0, 0, 0, 0x47, UInt8(pid >> 8) | (first ? 0x40 : 0), UInt8(pid & 0xFF)])
            if chunk.count == 184 {
                packet.append(0x10)
            } else {
                // Pad short packets with an adaptation field of stuffing bytes.
                let stuffing = 184 - chunk.count - 1
                packet.append(0x30)
                packet.append(UInt8(stuffing))
                if stuffing > 0 { packet += Data([0x00]) + Data(repeating: 0xFF, count: stuffing - 1) }
            }
            packet += chunk
            output += packet
            first = false
        }
        return output
    }

    private static func timestamp(_ value: UInt64, marker: UInt8) -> Data {
        Data([
            marker | UInt8((value >> 29) & 0x0E),
            UInt8((value >> 22) & 0xFF),
            UInt8((value >> 14) & 0xFE) | 1,
            UInt8((value >> 7) & 0xFF),
            UInt8((value << 1) & 0xFE) | 1,
        ])
    }
}

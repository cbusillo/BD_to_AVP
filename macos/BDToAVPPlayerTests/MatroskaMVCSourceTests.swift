import XCTest
@testable import BDToAVPPlayer

final class MatroskaMVCSourceTests: XCTestCase {
    private let sps = Data([0x67, 0x64, 0x00, 0x29, 0xAC])
    private let subsetSPS = Data([0x6F, 0x76, 0x00, 0x29])
    private let pps = Data([0x68, 0xEE, 0x3C, 0x80])
    private let dependentPPS = Data([0x68, 0xEE, 0x3C, 0x81])

    func testListsTheMVCTitleWithItsAudioAndSubtitleTracks() throws {
        let source = try MatroskaMVCSource(source: DataByteSource(file()))
        let title = try XCTUnwrap(source.info.mainTitle)

        XCTAssertEqual(source.info.container, .matroska)
        XCTAssertEqual(title.durationSeconds, 5_400, accuracy: 0.001)
        XCTAssertEqual(
            title.audioTracks,
            [
                Bluray3DTrack(id: 2, kind: .audio, codingType: 0, language: "eng", name: "Surround 7.1"),
                Bluray3DTrack(id: 3, kind: .audio, codingType: 0x81, language: nil, name: nil),
            ]
        )
        XCTAssertEqual(title.subtitleTracks, [Bluray3DTrack(id: 4, kind: .subtitle, codingType: 0x90, language: "fre", name: nil)])
        // TrueHD in Matroska has no AC-3 core, so only the AC-3 track reaches the audio player.
        XCTAssertEqual(title.audioStreams.map(\.pid), [3])
    }

    func testDeliversAnnexBStereoFramesWithParameterSetsFirst() throws {
        let source = try MatroskaMVCSource(source: DataByteSource(file()))
        let sink = CollectingPacketSink()

        let statistics = try source.read(
            title: try XCTUnwrap(source.info.mainTitle), audioTrackIDs: [2], subtitleTrackIDs: [], into: sink
        )

        let start = Data([0, 0, 0, 1])
        let parameterSets = start + sps + start + subsetSPS + start + pps + start + dependentPPS
        XCTAssertEqual(
            sink.video.map(\.data),
            [
                parameterSets + start + Data([0x65, 1, 2]) + start + Data([0x14, 3, 4]),
                start + Data([0x41, 5]) + start + Data([0x14, 6]),
            ]
        )
        XCTAssertEqual(sink.video.map(\.isRandomAccess), [true, false])
        // Cluster at 10 s, blocks at +0 and +42 ms, as 90 kHz ticks.
        XCTAssertEqual(sink.video.map(\.pts), [900_000, 903_780])
        XCTAssertEqual(sink.audio.map(\.payload), [Data([7, 7]), Data([8, 8, 8])])
        XCTAssertEqual(sink.audio.map(\.pts), [900_000, 900_000])
        XCTAssertTrue(sink.subtitles.isEmpty)
        XCTAssertEqual(statistics.videoAccessUnits, 2)
        XCTAssertEqual(statistics.audioPackets, 2)
    }

    func testClustersOfUnknownSizeEndAtTheNextCluster() throws {
        let source = try MatroskaMVCSource(source: DataByteSource(file(unknownSizeClusters: true)))
        let sink = CollectingPacketSink()

        _ = try source.read(title: try XCTUnwrap(source.info.mainTitle), audioTrackIDs: [], subtitleTrackIDs: [], into: sink)

        XCTAssertEqual(sink.video.map(\.pts), [900_000, 903_780, 1_800_000])
    }

    func testAVCWithoutMVCIsListedButNotPlayable() throws {
        let source = try MatroskaMVCSource(source: DataByteSource(file(mvc: false)))

        XCTAssertEqual(source.info.titles.first?.unsupportedReason, .notStereo3D)
        XCTAssertNil(source.info.mainTitle)
    }

    func testNonMatroskaBytesAreUnrecognized() {
        XCTAssertThrowsError(try MatroskaMVCSource(source: DataByteSource(Data(repeating: 0, count: 64)))) { error in
            XCTAssertEqual(error as? Bluray3DSourceError, .unrecognized)
        }
    }

    // MARK: - Fixture

    private func file(mvc: Bool = true, unknownSizeClusters: Bool = false) -> Data {
        let avcC = Data([1, 0x64, 0, 0x29, 0xFF, 0xE1]) + length16(sps) + sps + Data([1]) + length16(pps) + pps
        let mvcC = Data([1, 0x80, 0, 0x29, 0xFF, 0x02]) + length16(sps) + sps + length16(subsetSPS) + subsetSPS
            + Data([2]) + length16(pps) + pps + length16(dependentPPS) + dependentPPS
        var video = EBML.uint(0xD7, 1) + EBML.uint(0x83, 1) + EBML.string(0x86, "V_MPEG4/ISO/AVC")
            + EBML.element(0x63A2, avcC)
        if mvc {
            video += EBML.element(0x41E4, EBML.uint(0x41E7, 0x6D76_6343) + EBML.element(0x41ED, mvcC))
        }
        let tracks = EBML.element(0xAE, video)
            + EBML.element(0xAE, EBML.uint(0xD7, 2) + EBML.uint(0x83, 2) + EBML.string(0x86, "A_TRUEHD") + EBML.string(0x536E, "Surround 7.1"))
            + EBML.element(0xAE, EBML.uint(0xD7, 3) + EBML.uint(0x83, 2) + EBML.string(0x86, "A_AC3") + EBML.string(0x22B59C, "und"))
            + EBML.element(0xAE, EBML.uint(0xD7, 4) + EBML.uint(0x83, 17) + EBML.string(0x86, "S_HDMV/PGS") + EBML.string(0x22B59C, "fre"))
        let info = EBML.uint(0x2AD7B1, 1_000_000) + EBML.element(0x4489, withUnsafeBytes(of: Double(5_400_000).bitPattern.bigEndian) { Data($0) })
        let keyframe = lengthPrefixed([Data([0x65, 1, 2]), Data([0x14, 3, 4])])
        let delta = lengthPrefixed([Data([0x41, 5]), Data([0x14, 6])])
        let firstCluster = EBML.uint(0xE7, 10_000)
            + EBML.element(0xA3, block(track: 1, time: 0, flags: 0x80, keyframe))
            // Two Xiph-laced audio frames of 2 and 3 bytes.
            + EBML.element(0xA3, block(track: 2, time: 0, flags: 0x82, Data([1, 2, 7, 7, 8, 8, 8])))
            + EBML.element(0xA0, EBML.element(0xA1, block(track: 1, time: 42, flags: 0, delta)) + EBML.element(0xFB, Data([0xFF])))
            + EBML.element(0xA3, block(track: 4, time: 0, flags: 0x80, Data([0x16])))
        let secondCluster = EBML.uint(0xE7, 20_000) + EBML.element(0xA3, block(track: 1, time: 0, flags: 0x80, delta))
        let clusters = unknownSizeClusters
            ? EBML.unknownSize(0x1F43_B675, firstCluster) + EBML.unknownSize(0x1F43_B675, secondCluster)
            : EBML.element(0x1F43_B675, firstCluster)
        let header = EBML.element(0x1A45_DFA3, EBML.string(0x4282, "matroska"))
        return header + EBML.element(0x1853_8067, EBML.element(0x1549_A966, info) + EBML.element(0x1654_AE6B, tracks) + clusters)
    }

    private func block(track: UInt8, time: Int16, flags: UInt8, _ payload: Data) -> Data {
        Data([0x80 | track]) + withUnsafeBytes(of: time.bigEndian) { Data($0) } + Data([flags]) + payload
    }

    private func length16(_ data: Data) -> Data { Data([UInt8(data.count >> 8), UInt8(data.count & 0xFF)]) }

    private func lengthPrefixed(_ units: [Data]) -> Data {
        units.reduce(Data()) { $0 + withUnsafeBytes(of: UInt32($1.count).bigEndian) { Data($0) } + $1 }
    }
}

/// Writes EBML elements with 8-byte sizes.
enum EBML {
    static func id(_ value: UInt32) -> Data {
        let bytes = withUnsafeBytes(of: value.bigEndian) { Data($0) }
        return Data(bytes.drop { $0 == 0 })
    }

    static func element(_ identifier: UInt32, _ payload: Data) -> Data {
        id(identifier) + Data([0x01]) + withUnsafeBytes(of: UInt64(payload.count).bigEndian) { Data($0.dropFirst()) } + payload
    }

    static func unknownSize(_ identifier: UInt32, _ payload: Data) -> Data {
        id(identifier) + Data([0x01, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]) + payload
    }

    static func uint(_ identifier: UInt32, _ value: UInt64) -> Data {
        element(identifier, withUnsafeBytes(of: value.bigEndian) { Data($0) })
    }

    static func string(_ identifier: UInt32, _ value: String) -> Data {
        element(identifier, Data(value.utf8))
    }
}

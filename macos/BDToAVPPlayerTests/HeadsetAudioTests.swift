import AVFoundation
import XCTest
@testable import BDToAVPPlayer

/// Fixtures are short cuts of real discs' audio PIDs, as 192-byte M2TS packets read through
/// libbluray: Rainforest playlist 00005 (German 0x1100 and English 0x1101, DTS-HD MA 5.1),
/// Fast X playlist 00000 (TrueHD 7.1 with its AC-3 core on 0x1100, E-AC-3 7.1 on 0x1102) and
/// Ocean Dreams playlist 00004 (DTS 2.0 on 0x1100).
final class HeadsetAudioTests: XCTestCase {
    private static let rainforestStreams = [
        BluRayAudioStreamDescriptor(pid: 0x1100, coding: .dtsHDMaster, languageCode: "deu"),
        BluRayAudioStreamDescriptor(pid: 0x1101, coding: .dtsHDMaster, languageCode: "eng"),
    ]
    private static let rainforestOriginPTS: UInt64 = 48_754

    // MARK: Source interface

    func testTransportStreamReaderReassemblesRainforestAudioPES() throws {
        let packets = try fixturePackets("rainforest-dts-hd-ma-deu-eng", pids: [0x1100, 0x1101])
        let german = packets.filter { $0.pid == 0x1100 }
        let english = packets.filter { $0.pid == 0x1101 }

        XCTAssertFalse(german.isEmpty)
        XCTAssertFalse(english.isEmpty)
        // DTS-HD MA on Blu-ray: the core in PES extension 0x71, the HD substream in 0x72.
        XCTAssertEqual(Set(english.compactMap(\.streamIDExtension)), [0x71, 0x72])
        XCTAssertEqual(english.first?.pts, Self.rainforestOriginPTS)
        let corePTS = english.filter { $0.streamIDExtension == 0x71 }.compactMap(\.pts)
        XCTAssertEqual(corePTS, corePTS.sorted())
    }

    func testTransportStreamReaderAcceptsAnyChunking() throws {
        let data = try fixture("oceandreams-dts")
        var whole = TransportStreamPESReader(pids: [0x1100])
        let expected = whole.append(data) + whole.finish()
        var chunked = TransportStreamPESReader(pids: [0x1100])
        var actual: [BluRayPESPacket] = []
        var offset = 0
        while offset < data.count {
            let end = min(offset + 1_000, data.count)
            actual += chunked.append(data[offset..<end])
            offset = end
        }
        actual += chunked.finish()
        XCTAssertEqual(actual, expected)
        XCTAssertFalse(expected.isEmpty)
    }

    // MARK: Timeline

    func testTimelineMapsPTSAroundTheOriginAndAcrossTheWrap() {
        let timeline = BluRayPresentationTimeline(originPTS: 48_754)
        XCTAssertEqual(timeline.ticks(for: 48_754), 0)
        XCTAssertEqual(timeline.ticks(for: 48_754 + 90_000), 90_000)
        XCTAssertEqual(timeline.ticks(for: 48_754 - 2_880), -2_880)

        let late = BluRayPresentationTimeline(originPTS: UInt64(BluRayPresentationTimeline.ptsModulus) - 90_000)
        XCTAssertEqual(late.ticks(for: 1_000, near: 80_000), 91_000, "PTS after the 33-bit wrap keeps counting")
        XCTAssertEqual(late.ticks(for: UInt64(BluRayPresentationTimeline.ptsModulus) - 90_000 - 450), -450)
    }

    // MARK: What plays, and its label

    func testAudioListNamesTheFormatThatPlays() {
        let english = Locale(identifier: "en_US")
        XCTAssertEqual(HeadsetAudioTrack.tracks(for: Self.rainforestStreams, locale: english).map(\.label), [
            "German (DTS core)", "English (DTS core)",
        ])
        let mixed = HeadsetAudioTrack.tracks(
            for: [
                .init(pid: 0x1100, coding: .trueHD, languageCode: "eng"),
                .init(pid: 0x1101, coding: .ac3, languageCode: "eng"),
                .init(pid: 0x1102, coding: .eac3, languageCode: "fra"),
                .init(pid: 0x1103, coding: .dts, languageCode: "spa"),
                .init(pid: 0x1104, coding: .lpcm, languageCode: nil),
                .init(pid: 0x1105, coding: .dtsHDSecondary, languageCode: "eng"),
            ],
            locale: english
        )
        XCTAssertEqual(mixed.map(\.label), [
            "English (Dolby Digital core)",
            "English (Dolby Digital)",
            "French (Dolby Digital Plus)",
            "Spanish (DTS)",
            "Unknown language (PCM)",
        ])
    }

    func testSameLanguageAndFormatTracksStayDistinct() {
        let tracks = HeadsetAudioTrack.tracks(
            for: [
                .init(pid: 0x1100, coding: .dtsHDMaster, languageCode: "eng"),
                .init(pid: 0x1101, coding: .dts, languageCode: "eng"),
                .init(pid: 0x1102, coding: .dtsHDHighResolution, languageCode: "eng"),
            ],
            locale: Locale(identifier: "en_US")
        )
        XCTAssertEqual(Set(tracks.map(\.label)).count, 3)
        XCTAssertEqual(tracks[1].label, "English (DTS)")
    }

    func testPreferredTrackFollowsTheWearersLanguage() {
        let tracks = HeadsetAudioTrack.tracks(for: Self.rainforestStreams, locale: Locale(identifier: "en_US"))
        XCTAssertEqual(HeadsetAudioTrack.preferredTrack(in: tracks, preferredLanguages: ["en-US"])?.id, 0x1101)
        XCTAssertEqual(HeadsetAudioTrack.preferredTrack(in: tracks, preferredLanguages: ["de-DE"])?.id, 0x1100)
        XCTAssertEqual(HeadsetAudioTrack.preferredTrack(in: tracks, preferredLanguages: ["ja-JP"])?.id, 0x1100)
    }

    // MARK: Decoding

    func testDTSHDMasterAudioPlaysItsCoreThroughFFmpeg() throws {
        let packets = try fixturePackets("rainforest-dts-hd-ma-deu-eng", pids: [0x1101])
        let decoder = try HeadsetAudioPlayback.dtsCore(isHDTrack: true).makeDecoder()
        let timeline = BluRayPresentationTimeline(originPTS: Self.rainforestOriginPTS)
        var chunks: [DecodedAudioChunk] = []
        for packet in packets {
            chunks += try decoder.decode(packet, ticks: packet.pts.map { timeline.ticks(for: $0) })
        }

        let corePackets = packets.filter { $0.streamIDExtension == 0x71 }
        XCTAssertEqual(chunks.count, corePackets.count, "one core frame per core PES; the HD substream is skipped")
        XCTAssertEqual(chunks.first?.startTicks, 0)
        XCTAssertEqual(Set(chunks.map(\.sampleRate)), [48_000])
        XCTAssertEqual(chunks.first?.speakers, [.left, .right, .center, .lfe, .leftSurround, .rightSurround])
        XCTAssertEqual(Set(chunks.map(\.frameCount)), [512])
        XCTAssertGreaterThan(peak(chunks.flatMap(\.samples)), 0)
        assertContiguous(chunks)
    }

    func testFFmpegFrameworkIsTheLGPLBuild() {
        XCTAssertEqual(DTSCoreDecoder.ffmpegLicense, "LGPL version 2.1 or later")
    }

    func testDTSCoreParserCutsTheCoreFromACombinedFrame() throws {
        let packets = try fixturePackets("rainforest-dts-hd-ma-deu-eng", pids: [0x1100])
        let core = try XCTUnwrap(packets.first { $0.streamIDExtension == 0x71 })
        let substream = try XCTUnwrap(packets.first { $0.streamIDExtension == 0x72 })
        let combined = core.payload + substream.payload

        let frames = DTSCoreFrameParser.frames(in: combined)
        XCTAssertEqual(frames.map(\.bytes), [core.payload])
        XCTAssertEqual(frames.first?.samplesPerFrame, 512)
        XCTAssertTrue(DTSCoreFrameParser.frames(in: substream.payload).isEmpty)
    }

    func testPlainDTSDecodes() throws {
        let packets = try fixturePackets("oceandreams-dts", pids: [0x1100])
        let decoder = try HeadsetAudioPlayback(coding: .dts)!.makeDecoder()
        var chunks: [DecodedAudioChunk] = []
        for packet in packets {
            chunks += try decoder.decode(packet, ticks: packet.pts.map { Int64($0) })
        }
        XCTAssertFalse(chunks.isEmpty)
        XCTAssertEqual(chunks.first?.speakers, [.left, .right])
    }

    func testTrueHDPlaysItsAC3CoreOnly() throws {
        let packets = try fixturePackets("fastx-truehd-eac3", pids: [0x1100])
        XCTAssertTrue(packets.contains { $0.streamIDExtension == 0x72 }, "the fixture carries TrueHD units")
        let decoder = try HeadsetAudioPlayback(coding: .trueHD)!.makeDecoder()
        var chunks: [DecodedAudioChunk] = []
        for packet in packets {
            chunks += try decoder.decode(packet, ticks: packet.pts.map { Int64($0) })
        }
        XCTAssertEqual(chunks.count, packets.filter { $0.streamIDExtension == 0x76 }.count)
        // AudioToolbox trims its 288-sample decoder delay from the first frame.
        XCTAssertEqual(chunks.map(\.frameCount), [1248] + Array(repeating: 1536, count: chunks.count - 1))
        XCTAssertEqual(chunks.first?.channels, 6)
        XCTAssertGreaterThan(peak(chunks.flatMap(\.samples)), 0)
        assertContiguous(chunks)
    }

    func testEAC3DecodesWithItsDependentSubstream() throws {
        let packets = try fixturePackets("fastx-truehd-eac3", pids: [0x1102])
        let decoder = try HeadsetAudioPlayback(coding: .eac3)!.makeDecoder()
        var chunks: [DecodedAudioChunk] = []
        for packet in packets {
            chunks += try decoder.decode(packet, ticks: packet.pts.map { Int64($0) })
        }
        XCTAssertFalse(chunks.isEmpty)
        XCTAssertEqual(chunks.first?.channels, 8, "AC-3 core plus the E-AC-3 dependent substream is 7.1")
        XCTAssertEqual(Set(chunks.dropFirst().map(\.frameCount)), [1536])
        XCTAssertGreaterThan(peak(chunks.flatMap(\.samples)), 0)
        assertContiguous(chunks)
    }

    func testBluRayLPCMDropsPaddingAndKeepsDiscChannelOrder() throws {
        // 3/0 (L R C plus a padding channel), 16-bit, 48 kHz, two frames.
        var payload = Data([0x00, 0x10, 0x41, 0x40])
        let frames: [[Int16]] = [[1_000, -1_000, 16_384, 77], [-32_768, 32_767, 0, 77]]
        for frame in frames {
            for sample in frame {
                payload.append(UInt8(UInt16(bitPattern: sample) >> 8))
                payload.append(UInt8(UInt16(bitPattern: sample) & 0xFF))
            }
        }
        let decoded = try XCTUnwrap(BluRayLPCMFrame(payload: payload))
        XCTAssertEqual(decoded.sampleRate, 48_000)
        XCTAssertEqual(decoded.speakers, [.left, .right, .center])
        let expected: [Float] = [1_000.0 / 32_768.0, -1_000.0 / 32_768.0, 0.5, -1, 32_767.0 / 32_768.0, 0]
        XCTAssertEqual(decoded.samples, expected)

        // 3/2+LFE, 24-bit: the LFE comes last on disc.
        var surround = Data([0x00, 0x12, 0x91, 0xC0])
        for value in [1, 2, 3, 4, 5, 6] {
            surround.append(contentsOf: [UInt8(value), 0, 0])
        }
        let decodedSurround = try XCTUnwrap(BluRayLPCMFrame(payload: surround))
        XCTAssertEqual(decodedSurround.speakers, [.left, .right, .center, .leftSurround, .rightSurround, .lfe])
        XCTAssertEqual(decodedSurround.samples.map { Int(($0 * 128).rounded()) }, [1, 2, 3, 4, 5, 6])
    }

    func testPCMSampleBufferCarriesTimelineTimeAndLayout() throws {
        let chunk = DecodedAudioChunk(startTicks: 45_000, sampleRate: 48_000, speakers: [.left, .right], samples: Array(repeating: 0.25, count: 960))
        let buffer = try PCMSampleBufferFactory().sampleBuffer(for: chunk)
        XCTAssertEqual(buffer.presentationTimeStamp.seconds, 0.5, accuracy: 1e-9)
        XCTAssertEqual(buffer.numSamples, 480)
        XCTAssertEqual(buffer.duration.seconds, 0.01, accuracy: 1e-6)
        let description = try XCTUnwrap(buffer.formatDescription?.audioStreamBasicDescription)
        XCTAssertEqual(description.mChannelsPerFrame, 2)
    }

    // MARK: Switching bookkeeping

    func testHistoryKeepsThePacketCoveringTheSwitchPoint() {
        var history = AudioPacketHistory()
        for index in 0..<10 {
            history.append(packet(pid: 1, pts: UInt64(index * 1_000)), ticks: Int64(index * 1_000))
        }
        history.trim(before: 4_500)
        XCTAssertEqual(history.entries[1]?.first?.ticks, 4_000)
        XCTAssertEqual(history.packets(for: 1, from: 6_200).first?.ticks, 6_000)
        XCTAssertEqual(history.packets(for: 1, from: 100).first?.ticks, 4_000)
        XCTAssertTrue(history.packets(for: 2, from: 0).isEmpty)
    }

    func testPlayerSwitchesTracksOnTheSharedClockWithoutRestarting() throws {
        let packets = try fixturePackets("rainforest-dts-hd-ma-deu-eng", pids: [0x1100, 0x1101])
        let synchronizer = AVSampleBufferRenderSynchronizer()
        let player = HeadsetAudioPlayer(
            streams: Self.rainforestStreams,
            timeline: BluRayPresentationTimeline(originPTS: Self.rainforestOriginPTS),
            synchronizer: synchronizer,
            initialTrackID: 0x1100,
            locale: Locale(identifier: "en_US")
        )
        XCTAssertTrue(synchronizer.renderers.contains { $0 === player.renderer })
        XCTAssertEqual(player.selectedTrackID, 0x1100)

        for packet in packets {
            player.receive(packet)
        }
        waitUntil { player.enqueuedRanges.last.map { $0.endTicks >= 20_000 } ?? false }
        XCTAssertEqual(Set(player.enqueuedRanges.map(\.pid)), [0x1100])

        player.select(trackID: 0x1101)
        waitUntil { player.enqueuedRanges.contains { $0.pid == 0x1101 } }
        XCTAssertEqual(player.selectedTrackID, 0x1101)
        XCTAssertNil(player.lastError)

        let ranges = player.enqueuedRanges
        let firstEnglish = try XCTUnwrap(ranges.first { $0.pid == 0x1101 })
        // With the clock at zero, the switch takes effect 100 ms in, on a core frame boundary.
        XCTAssertGreaterThanOrEqual(firstEnglish.startTicks, HeadsetAudioPlayer.switchLeadTicks)
        XCTAssertLessThan(firstEnglish.startTicks, HeadsetAudioPlayer.switchLeadTicks + 960)
        let english = ranges.filter { $0.pid == 0x1101 }
        for (previous, next) in zip(english, english.dropFirst()) {
            XCTAssertEqual(next.startTicks, previous.endTicks)
        }
        XCTAssertEqual(synchronizer.rate, 0, "switching does not start or restart the clock")
    }

    func testPlayerPlaysOnTheRunningClock() throws {
        let packets = try fixturePackets("rainforest-dts-hd-ma-deu-eng", pids: [0x1100, 0x1101])
        let synchronizer = AVSampleBufferRenderSynchronizer()
        let player = HeadsetAudioPlayer(
            streams: Self.rainforestStreams,
            timeline: BluRayPresentationTimeline(originPTS: Self.rainforestOriginPTS),
            synchronizer: synchronizer,
            initialTrackID: 0x1101
        )
        for packet in packets {
            player.receive(packet)
        }
        waitUntil { !player.enqueuedRanges.isEmpty }
        synchronizer.setRate(1, time: .zero)
        waitUntil(timeout: 3) { synchronizer.currentTime().seconds > 0.1 }
        XCTAssertNotEqual(player.renderer.status, .failed, "\(String(describing: player.renderer.error))")
        XCTAssertNil(player.lastError)
        synchronizer.setRate(0, time: synchronizer.currentTime())
    }

    // MARK: Helpers

    private func fixture(_ name: String) throws -> Data {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: name, withExtension: "m2ts"))
        return try Data(contentsOf: url)
    }

    private func fixturePackets(_ name: String, pids: Set<UInt16>) throws -> [BluRayPESPacket] {
        var reader = TransportStreamPESReader(pids: pids)
        return reader.append(try fixture(name)) + reader.finish()
    }

    private func packet(pid: UInt16, pts: UInt64) -> BluRayPESPacket {
        BluRayPESPacket(pid: pid, streamID: 0xFD, streamIDExtension: 0x71, pts: pts, payload: Data())
    }

    private func peak(_ samples: [Float]) -> Float {
        samples.map(abs).max() ?? 0
    }

    private func assertContiguous(_ chunks: [DecodedAudioChunk], file: StaticString = #filePath, line: UInt = #line) {
        for (previous, next) in zip(chunks, chunks.dropFirst()) {
            XCTAssertEqual(next.startTicks, previous.endTicks, "decoded audio is contiguous on the timeline", file: file, line: line)
        }
    }

    private func waitUntil(timeout: TimeInterval = 5, _ condition: () -> Bool) {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition(), Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.02))
        }
        XCTAssertTrue(condition(), "condition not met within \(timeout) s")
    }
}

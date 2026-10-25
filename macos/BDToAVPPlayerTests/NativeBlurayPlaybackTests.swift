import AVFoundation
import os
import XCTest
@testable import BDToAVPPlayer

/// Plays edge264's 16x16 synthetic MVC stream (see MVCDecoderTests) through the real decoder,
/// renderer and synchronizer, from a fake source that can pause mid-title like a slow share.
final class NativeBlurayPlaybackTests: XCTestCase {
    private final class FixtureSource: Bluray3DSource {
        let info: Bluray3DSourceInfo
        let repetitions: Int
        /// The reader stops for `stall` seconds after this many units, as an SMB read can.
        let stallAfter: Int?
        let stall: TimeInterval
        private let units: [Data]
        /// Audio PES packets, delivered in PTS order between the video units.
        private let audio: [BluRayPESPacket]

        init(repetitions: Int, stallAfter: Int? = nil, stall: TimeInterval = 0, audio: [BluRayPESPacket] = []) throws {
            units = try MVCDecoderTests.accessUnits()
            self.repetitions = repetitions
            self.stallAfter = stallAfter
            self.stall = stall
            self.audio = audio
            let tracks = Set(audio.map(\.pid)).sorted().map {
                Bluray3DTrack(id: Int($0), kind: .audio, codingType: 0x86, language: $0 == 0x1100 ? "deu" : "eng", name: nil)
            }
            info = Bluray3DSourceInfo(container: .discImage, titles: [Self.title(seconds: Double(repetitions * 8) / 23.976, audio: tracks)])
        }

        static func title(seconds: Double, audio: [Bluray3DTrack]) -> Bluray3DTitle {
            Bluray3DTitle(
                id: "playlist:00001", playlist: 1, durationSeconds: seconds, isMainFeature: true,
                baseViewIsRightEye: false, unsupportedReason: nil, audioTracks: audio, subtitleTracks: []
            )
        }

        func read(
            title: Bluray3DTitle,
            audioTrackIDs: [Int],
            subtitleTrackIDs: [Int],
            into sink: Bluray3DPacketSink
        ) throws -> Bluray3DReadStatistics {
            // The fixture's base view is coded POC 0, 2, 6, 4 in each IDR sequence of four.
            let displayIndex = [0, 1, 3, 2]
            var statistics = Bluray3DReadStatistics()
            var pendingAudio = audio[...]
            for repetition in 0..<repetitions {
                for (index, unit) in units.enumerated() {
                    // The first unit carries the parameter sets; later repetitions resend it whole.
                    let frame = repetition * 8 + (index / 4) * 4 + displayIndex[index % 4]
                    // The Rainforest audio fixture starts at PTS 48,754, the title's first video PTS.
                    let pts = UInt64(NativeBlurayPlaybackTests.originPTS + (Double(frame) * 3_753.75).rounded())
                    while let next = pendingAudio.first, (next.pts ?? 0) <= pts {
                        pendingAudio.removeFirst()
                        if !sink.receive(.audio(next)) { return statistics }
                    }
                    if let stallAfter, statistics.videoAccessUnits == UInt64(stallAfter) {
                        Thread.sleep(forTimeInterval: stall)
                    }
                    let delivered = sink.receive(.video(MVCAccessUnit(pts: pts, dts: nil, isRandomAccess: index % 4 == 0, data: unit)))
                    statistics.videoAccessUnits += 1
                    if !delivered { return statistics }
                }
            }
            return statistics
        }
    }

    static let originPTS: Double = 48_754

    /// Rainforest's German and English DTS-HD MA tracks: 24 core frames of 960 ticks each per
    /// track, repeated back to back so the audio spans the whole test title.
    private func rainforestAudio(repeats: Int = 12) throws -> [BluRayPESPacket] {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "rainforest-dts-hd-ma-deu-eng", withExtension: "m2ts"))
        var reader = TransportStreamPESReader(pids: [0x1100, 0x1101])
        let packets = reader.append(try Data(contentsOf: url)) + reader.finish()
        return (0..<repeats).flatMap { repeatIndex in
            packets.map { packet in
                BluRayPESPacket(
                    pid: packet.pid, streamID: packet.streamID, streamIDExtension: packet.streamIDExtension,
                    pts: packet.pts.map { $0 + UInt64(repeatIndex * 24 * 960) }, payload: packet.payload
                )
            }
        }
    }

    private func makePlayback(_ source: FixtureSource, configuration: NativeBlurayPlayback.Configuration = .init()) -> NativeBlurayPlayback {
        var configuration = configuration
        configuration.decoderThreads = 1
        // A renderer takes frames only once it has a video target; a display layer is the one a
        // unit test can give it.
        let layer = AVSampleBufferDisplayLayer()
        let playback = NativeBlurayPlayback(
            source: source, title: source.info.titles[0], configuration: configuration, renderer: layer.sampleBufferRenderer
        )
        addTeardownBlock { playback.stop(); _ = layer }
        return playback
    }

    private func waitUntil(_ timeout: TimeInterval, _ condition: () -> Bool) -> Bool {
        let deadline = Date(timeIntervalSinceNow: timeout)
        while Date() < deadline {
            if condition() { return true }
            Thread.sleep(forTimeInterval: 0.01)
        }
        return condition()
    }

    func testPlaysEveryFrameOnTimeToTheEnd() throws {
        let source = try FixtureSource(repetitions: 9)
        let playback = makePlayback(source)
        playback.start()
        XCTAssertTrue(waitUntil(15) { playback.status.hasEnded }, "status \(playback.status)")
        let status = playback.status
        XCTAssertNil(status.failure)
        XCTAssertEqual(status.statistics.framesQueued, 72)
        XCTAssertEqual(status.statistics.lateFrames, 0)
        XCTAssertEqual(status.statistics.framesWithoutDependentView, 0)
        XCTAssertFalse(status.wantsPlayback)
        XCTAssertEqual(status.currentTime, 72 / 23.976, accuracy: 0.25)
        XCTAssertEqual(status.loadedRange?.lowerBound ?? -1, 0, accuracy: 0.001)
    }

    func testPauseHoldsTheClockAndPlayResumesIt() throws {
        let playback = makePlayback(try FixtureSource(repetitions: 30))
        playback.start()
        XCTAssertTrue(waitUntil(10) { playback.status.currentTime > 0.5 })
        playback.setPlaying(false)
        let paused = playback.status.currentTime
        Thread.sleep(forTimeInterval: 0.4)
        XCTAssertEqual(playback.status.currentTime, paused, accuracy: 0.001)
        playback.setPlaying(true)
        XCTAssertTrue(waitUntil(5) { playback.status.currentTime > paused + 0.3 })
        XCTAssertEqual(playback.status.statistics.lateFrames, 0)
    }

    func testAPauseWhileTheClockStartsIsNotUndone() throws {
        let playback = makePlayback(try FixtureSource(repetitions: 30))
        let paused = DispatchSemaphore(value: 0)
        let once = OSAllocatedUnfairLock(initialState: false)
        // As the video thread starts the clock, the wearer pauses from another thread.
        playback.willChangeClockRate = { rate in
            guard rate == 1, once.withLock({ first in defer { first = true }; return !first }) else { return }
            Thread { playback.setPlaying(false); paused.signal() }.start()
            _ = paused.wait(timeout: .now() + 0.2)
        }
        playback.start()
        XCTAssertTrue(waitUntil(10) { once.withLock { $0 } })
        XCTAssertTrue(waitUntil(2) { !playback.status.wantsPlayback })
        Thread.sleep(forTimeInterval: 0.3)
        XCTAssertEqual(playback.synchronizer.rate, 0, "the pause must win over the clock start")
        let position = playback.status.currentTime
        Thread.sleep(forTimeInterval: 0.3)
        XCTAssertEqual(playback.status.currentTime, position, accuracy: 0.001)
    }

    func testPlaysARealAudioTrackOnTheVideoClock() throws {
        let playback = makePlayback(try FixtureSource(repetitions: 9, audio: try rainforestAudio()))
        XCTAssertEqual(playback.audioTracks.map(\.id), [0x1100, 0x1101])
        playback.start()
        XCTAssertTrue(waitUntil(15) { playback.status.hasEnded }, "status \(playback.status)")
        let player = try XCTUnwrap(playback.audioPlayer)
        XCTAssertNil(playback.status.audioFailure)
        XCTAssertNil(player.lastError)
        let ranges = player.enqueuedRanges.filter { $0.pid == player.selectedTrackID }
        // Audio and video share time zero, the first video PTS, so the track starts at 0.
        XCTAssertEqual(try XCTUnwrap(ranges.first).startTicks, 0)
        // The source delivers audio up to the last video frame, at 71 frames of 3,753.75 ticks.
        XCTAssertGreaterThanOrEqual(try XCTUnwrap(ranges.last).endTicks, 266_516)
        for (previous, next) in zip(ranges, ranges.dropFirst()) {
            XCTAssertEqual(next.startTicks, previous.endTicks, "the audio is continuous")
        }
        XCTAssertEqual(playback.status.statistics.lateFrames, 0)
    }

    func testAudioThatCannotBeDecodedIsReportedWhileVideoPlaysOn() throws {
        // Keep each DTS core frame's sync word and header, so it is parsed and sent to the
        // decoder, but damage everything after the header.
        let damaged = try rainforestAudio().map { packet -> BluRayPESPacket in
            guard packet.streamIDExtension == 0x71 else { return packet }
            var bytes = [UInt8](packet.payload)
            for frame in DTSCoreFrameParser.frames(in: packet.payload) {
                guard let start = bytes.indices.first(where: { bytes[$0...].starts(with: frame.bytes.prefix(16)) }) else { continue }
                for index in (start + 16)..<(start + frame.bytes.count) { bytes[index] = 0xFF }
            }
            return BluRayPESPacket(pid: packet.pid, streamID: packet.streamID, streamIDExtension: packet.streamIDExtension, pts: packet.pts, payload: Data(bytes))
        }
        let playback = makePlayback(try FixtureSource(repetitions: 9, audio: damaged))
        playback.start()
        XCTAssertTrue(waitUntil(15) { playback.status.hasEnded }, "status \(playback.status)")
        XCTAssertNotNil(playback.status.audioFailure, "lastError \(String(describing: playback.audioPlayer?.lastError))")
        XCTAssertNil(playback.status.failure, "the video plays on")
        XCTAssertEqual(playback.status.statistics.framesQueued, 72)
        XCTAssertLessThanOrEqual(playback.audioPlayer?.pendingCount ?? 0, HeadsetAudioPlayer.maximumPendingChunks)
    }

    func testSeeksInsideTheLoadedPartAndRefusesOtherTargets() throws {
        let playback = makePlayback(try FixtureSource(repetitions: 30))
        playback.start()
        XCTAssertTrue(waitUntil(10) { playback.status.currentTime > 0.5 && (playback.status.loadedRange?.upperBound ?? 0) > 8 })
        XCTAssertEqual(playback.seek(to: 6), .seeking)
        XCTAssertTrue(waitUntil(5) { playback.status.currentTime > 6.2 }, "status \(playback.status)")
        XCTAssertEqual(playback.seek(to: 1), .seeking)
        XCTAssertTrue(waitUntil(5) { (1.1..<3).contains(playback.status.currentTime) }, "status \(playback.status)")
        guard case let .outside(loaded) = playback.seek(to: 600) else {
            return XCTFail("A target past the title is outside the loaded part")
        }
        XCTAssertEqual(loaded?.upperBound ?? 0, 239 / 23.976, accuracy: 0.05)
        XCTAssertNil(playback.status.failure)
    }

    func testAStalledSourceStopsTheClockUntilItCatchesUp() throws {
        // About 1.3 s of video, then a 1.5 s stall.
        let source = try FixtureSource(repetitions: 8, stallAfter: 32, stall: 1.5)
        var configuration = NativeBlurayPlayback.Configuration()
        configuration.startSeconds = 0.5
        configuration.resumeSeconds = 0.5
        let playback = makePlayback(source, configuration: configuration)
        playback.start()
        XCTAssertTrue(waitUntil(10) { playback.status.isBuffering && playback.status.statistics.stalls == 1 })
        let stalledAt = playback.status.currentTime
        XCTAssertLessThan(stalledAt, 32 / 23.976 + 0.05)
        XCTAssertTrue(waitUntil(15) { playback.status.hasEnded }, "status \(playback.status)")
        XCTAssertEqual(playback.status.statistics.framesQueued, 64)
        XCTAssertEqual(playback.status.statistics.lateFrames, 0)
    }
}

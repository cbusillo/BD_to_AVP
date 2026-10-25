import AVFoundation
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

        init(repetitions: Int, stallAfter: Int? = nil, stall: TimeInterval = 0) throws {
            units = try MVCDecoderTests.accessUnits()
            self.repetitions = repetitions
            self.stallAfter = stallAfter
            self.stall = stall
            info = Bluray3DSourceInfo(container: .discImage, titles: [Self.title(seconds: Double(repetitions * 8) / 23.976)])
        }

        static func title(seconds: Double) -> Bluray3DTitle {
            Bluray3DTitle(
                id: "playlist:00001", playlist: 1, durationSeconds: seconds, isMainFeature: true,
                baseViewIsRightEye: false, unsupportedReason: nil, audioTracks: [], subtitleTracks: []
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
            for repetition in 0..<repetitions {
                for (index, unit) in units.enumerated() {
                    // The first unit carries the parameter sets; later repetitions resend it whole.
                    let frame = repetition * 8 + (index / 4) * 4 + displayIndex[index % 4]
                    let pts = UInt64(1_000 + (Double(frame) * 3_753.75).rounded())
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

import XCTest
@testable import BDToAVPPlayer

final class PlaybackStatsTests: XCTestCase {
    private func sample(isOnShare: Bool) -> PlaybackStatsSample {
        PlaybackStatsSample(
            title: "Movie",
            fileName: "Movie.mov",
            format: .mvHEVC,
            fileByteCount: 1_000_000_000,
            isOnShare: isOnShare,
            shareBytesRead: isOnShare ? 4_000_000 : nil,
            isConnectionLost: false,
            currentTime: 10,
            duration: 100,
            loadedThrough: 25,
            video: PlaybackVideoDetails(codec: "MV-HEVC", width: 1920, height: 1080, frameRate: 23.976, bitsPerSecond: 30_000_000)
        )
    }

    private func value(_ label: String, in sections: [PlaybackStatsSection]) -> String? {
        sections.flatMap(\.rows).first { $0.label == label }?.value
    }

    func testReadSpeedIsMeasuredOverTheLastFewSeconds() {
        var meter = PlaybackThroughputMeter()

        XCTAssertNil(meter.record(bytes: 0, at: 10))
        XCTAssertEqual(meter.record(bytes: 2_000_000, at: 11), 2_000_000)
        XCTAssertEqual(meter.record(bytes: 6_000_000, at: 12), 3_000_000)
        // Samples older than the window drop out, so a pause in reads shows quickly.
        XCTAssertEqual(meter.record(bytes: 6_000_000, at: 16), 0)
    }

    func testANewMovieStartsTheReadSpeedOver() {
        var meter = PlaybackThroughputMeter()
        _ = meter.record(bytes: 0, at: 10)
        _ = meter.record(bytes: 9_000_000, at: 11)

        XCTAssertNil(meter.record(bytes: 1_000, at: 12))
        XCTAssertEqual(meter.record(bytes: 1_001_000, at: 13), 1_000_000)
    }

    func testAMovieOnAShareShowsItsReadSpeed() {
        let sections = PlaybackStatsPresentation.sections(for: sample(isOnShare: true), readBytesPerSecond: 2_500_000)

        XCTAssertEqual(value("Read speed", in: sections), "20.0 Mb/s")
        XCTAssertEqual(value("Stored", in: sections), "Network share")
        XCTAssertEqual(value("Average bit rate", in: sections), "80.0 Mb/s")
        XCTAssertEqual(value("Loaded ahead", in: sections), "15.0 s")
        XCTAssertEqual(value("Picture per eye", in: sections), "1920 × 1080")
    }

    func testALostShareSaysSoInsteadOfASpeed() {
        var lost = sample(isOnShare: true)
        lost.isConnectionLost = true

        let sections = PlaybackStatsPresentation.sections(for: lost, readBytesPerSecond: 2_500_000)

        XCTAssertEqual(value("Read speed", in: sections), "Connection lost")
    }

    func testAMovieOnTheHeadsetHasNoNetworkSection() {
        let sections = PlaybackStatsPresentation.sections(for: sample(isOnShare: false), readBytesPerSecond: nil)

        XCTAssertFalse(sections.contains { $0.title == "Network" })
        XCTAssertEqual(value("Stored", in: sections), "On this Vision Pro")
    }

    func testVideoCodecsAreNamed() {
        XCTAssertEqual(PlaybackStatsPresentation.codecName(fourCC: 0x6876_6331, format: .mvHEVC), "MV-HEVC")
        XCTAssertEqual(PlaybackStatsPresentation.codecName(fourCC: 0x6876_6331, format: .sideBySide), "HEVC")
        XCTAssertEqual(PlaybackStatsPresentation.codecName(fourCC: 0x6176_6331, format: .overUnder), "H.264")
        XCTAssertEqual(PlaybackStatsPresentation.codecName(fourCC: 0x6170_3468, format: .sideBySide), "ap4h")
    }
}

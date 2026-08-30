import XCTest
@testable import BDToAVPPlayer

final class PlaybackPresentationTests: XCTestCase {
    func testTimeFormatterUsesMinutesAndSeconds() {
        XCTAssertEqual(PlaybackTimeFormatter.string(for: 65.9), "1:05")
    }

    func testTimeFormatterIncludesHoursWhenNeeded() {
        XCTAssertEqual(PlaybackTimeFormatter.string(for: 3_661), "1:01:01")
    }

    func testTimeFormatterHandlesInvalidValues() {
        XCTAssertEqual(PlaybackTimeFormatter.string(for: .nan), "0:00")
        XCTAssertEqual(PlaybackTimeFormatter.string(for: -1), "0:00")
    }

    func testSeekClampsToThePlayableRange() {
        XCTAssertEqual(PlaybackSeekPolicy.clampedTime(-2, duration: 120), 0)
        XCTAssertEqual(PlaybackSeekPolicy.clampedTime(200, duration: 120), 120)
        XCTAssertEqual(PlaybackSeekPolicy.clampedTime(40, duration: 120), 40)
    }

    func testSeekKeepsFinitePositionWhenDurationIsUnavailable() {
        XCTAssertEqual(PlaybackSeekPolicy.clampedTime(40, duration: .nan), 40)
    }

    func testResumePolicyWritesAnInProgressPosition() {
        XCTAssertEqual(ResumeWritePolicy.decision(currentTime: 42, duration: 120), .write(42))
    }

    func testResumePolicyRemovesCompletedPlayback() {
        XCTAssertEqual(ResumeWritePolicy.decision(currentTime: 117, duration: 120), .remove)
    }

    func testResumePolicySkipsInvalidPosition() {
        XCTAssertEqual(ResumeWritePolicy.decision(currentTime: .infinity, duration: 120), .skip)
    }
}

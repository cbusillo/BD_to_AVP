import XCTest
@testable import BDToAVPPlayer

final class MediaFormatInspectorTests: XCTestCase {
    func testHVC1WithoutStereoSignalIsUnsupported() {
        let format = MediaFormatInspector.classify(
            .init(isStereoVideo: false, isStereoMultiviewVideo: false, width: 3840, height: 2160)
        )

        XCTAssertEqual(format, .unsupported)
    }

    func testStereoMultiviewSignalWinsOverDimensions() {
        let format = MediaFormatInspector.classify(
            .init(isStereoVideo: true, isStereoMultiviewVideo: true, width: 1920, height: 1080)
        )

        XCTAssertEqual(format, .mvHEVC)
    }

    func testWideStereoVideoIsSideBySide() {
        let format = MediaFormatInspector.classify(
            .init(isStereoVideo: true, isStereoMultiviewVideo: false, width: 3840, height: 1080)
        )

        XCTAssertEqual(format, .sideBySide)
    }

    func testTallStereoVideoIsOverUnder() {
        let format = MediaFormatInspector.classify(
            .init(isStereoVideo: true, isStereoMultiviewVideo: false, width: 1080, height: 3840)
        )

        XCTAssertEqual(format, .overUnder)
    }
}

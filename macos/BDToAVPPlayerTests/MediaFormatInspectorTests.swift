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

    func testExplicitSideBySidePackingDoesNotRequirePlaybackAssistantSignal() {
        let format = MediaFormatInspector.classify(
            .init(
                isStereoVideo: false,
                isStereoMultiviewVideo: false,
                width: 1920,
                height: 1080,
                packedStereoFormat: .sideBySide
            )
        )

        XCTAssertEqual(format, .sideBySide)
    }

    func testExplicitOverUnderPackingDoesNotRequirePlaybackAssistantSignal() {
        let format = MediaFormatInspector.classify(
            .init(
                isStereoVideo: false,
                isStereoMultiviewVideo: false,
                width: 1920,
                height: 1080,
                packedStereoFormat: .overUnder
            )
        )

        XCTAssertEqual(format, .overUnder)
    }

    func testPackedStereoRequiresHEVC() {
        let format = MediaFormatInspector.classify(
            .init(
                isStereoVideo: true,
                isStereoMultiviewVideo: false,
                isHEVC: false,
                width: 3840,
                height: 1080,
                packedStereoFormat: .sideBySide
            )
        )

        XCTAssertEqual(format, .unsupported)
    }

    func testPackedStereoRejectsHDR() {
        let format = MediaFormatInspector.classify(
            .init(
                isStereoVideo: true,
                isStereoMultiviewVideo: false,
                isHDR: true,
                width: 3840,
                height: 1080,
                packedStereoFormat: .sideBySide
            )
        )

        XCTAssertEqual(format, .unsupported)
    }

    func testFilenameTokensIdentifyPackedStereoWithoutGuessingFromDimensions() {
        XCTAssertEqual(MediaFormatInspector.packedStereoFormat(fileName: "Movie.FSBS.mov"), .sideBySide)
        XCTAssertEqual(MediaFormatInspector.packedStereoFormat(fileName: "Movie_SBS.mov"), .sideBySide)
        XCTAssertEqual(MediaFormatInspector.packedStereoFormat(fileName: "Movie_OU.mov"), .overUnder)
        XCTAssertEqual(MediaFormatInspector.packedStereoFormat(fileName: "Movie.FOU.mov"), .overUnder)
        XCTAssertNil(MediaFormatInspector.packedStereoFormat(fileName: "The.Tab.2019.mp4"))
        XCTAssertNil(MediaFormatInspector.packedStereoFormat(fileName: "Movie-TB.m4v"))
        XCTAssertNil(MediaFormatInspector.packedStereoFormat(fileName: "Movie.HSBS.mov"))
        XCTAssertNil(MediaFormatInspector.packedStereoFormat(fileName: "Movie_HOU.mov"))
        XCTAssertNil(MediaFormatInspector.packedStereoFormat(fileName: "Movie.Half-SBS.mov"))
        XCTAssertNil(MediaFormatInspector.packedStereoFormat(fileName: "Movie.H-OU.mov"))
        XCTAssertNil(MediaFormatInspector.packedStereoFormat(fileName: "VeryWideMovie.mov"))
    }
}

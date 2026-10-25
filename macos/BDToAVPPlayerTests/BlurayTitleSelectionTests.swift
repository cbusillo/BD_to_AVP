import XCTest
@testable import BDToAVPPlayer

final class BlurayTitleSelectionTests: XCTestCase {
    private let stereoDisc = BlurayDiscFacts(isStereo3D: true, isEncrypted: false)

    func testMainTitleIsTheLongestPlayableTitle() {
        let titles = BlurayTitleSelection.ordered(
            [
                facts(playlist: 1, minutes: 5),
                facts(playlist: 5, minutes: 90),
                facts(playlist: 800, minutes: 120, clips: 3),
                facts(playlist: 7, minutes: 60),
            ],
            disc: stereoDisc
        )

        XCTAssertEqual(titles.map(\.playlist), [5, 7, 1, 800])
        XCTAssertEqual(Bluray3DSourceInfo(container: .discImage, titles: titles).mainTitle?.playlist, 5)
        XCTAssertEqual(titles.last?.unsupportedReason, .multipleClips)
    }

    func testEqualLengthPrefersTheDiscMainFeatureThenTheLowerPlaylist() {
        let titles = BlurayTitleSelection.ordered(
            [
                facts(playlist: 9, minutes: 90),
                facts(playlist: 8, minutes: 90),
                facts(playlist: 12, minutes: 90, mainFeature: true),
            ],
            disc: stereoDisc
        )

        XCTAssertEqual(titles.map(\.playlist), [12, 8, 9])
    }

    func testDiscWideProblemsMakeEveryTitleUnplayable() {
        let encrypted = BlurayTitleSelection.ordered(
            [facts(playlist: 5, minutes: 90)],
            disc: BlurayDiscFacts(isStereo3D: true, isEncrypted: true)
        )
        let flat = BlurayTitleSelection.ordered(
            [facts(playlist: 5, minutes: 90)],
            disc: BlurayDiscFacts(isStereo3D: false, isEncrypted: false)
        )

        XCTAssertEqual(encrypted.first?.unsupportedReason, .encrypted)
        XCTAssertEqual(flat.first?.unsupportedReason, .notStereo3D)
        XCTAssertNil(Bluray3DSourceInfo(container: .discImage, titles: encrypted).mainTitle)
    }

    func testTitleShapesTheSSIFReaderCannotPlayAreRejected() {
        let reasons = [
            facts(playlist: 1, minutes: 90, angles: 2),
            facts(playlist: 2, minutes: 90, completeClip: false),
            facts(playlist: 3, minutes: 90, stereoStream: false),
        ].map { BlurayTitleSelection.unsupportedReason($0, disc: stereoDisc) }

        XCTAssertEqual(reasons, [.multipleAngles, .partialClip, .stereoStreamMissing])
    }

    func testStreamsBecomeTracksWithCodecsAndLanguages() {
        let title = BlurayTitleSelection.ordered(
            [
                facts(
                    playlist: 5,
                    minutes: 90,
                    audio: [
                        .init(pid: 0x1100, codingType: 0x86, language: "eng"),
                        .init(pid: 0x1101, codingType: 0x81, language: ""),
                        .init(pid: 0x1102, codingType: 0x24, language: "eng"),
                    ],
                    subtitles: [.init(pid: 0x1200, codingType: 0x90, language: "fra")]
                ),
            ],
            disc: stereoDisc
        )[0]

        XCTAssertEqual(title.id, "playlist:00005")
        XCTAssertEqual(title.durationSeconds, 90 * 60)
        XCTAssertEqual(title.audioTracks.map(\.id), [0x1100, 0x1101, 0x1102])
        XCTAssertEqual(
            title.subtitleTracks,
            [Bluray3DTrack(id: 0x1200, kind: .subtitle, codingType: 0x90, language: "fra", name: nil)]
        )
        // The audio player gets every stream it has a coding for, with the playlist's language.
        XCTAssertEqual(
            title.audioStreams,
            [
                BluRayAudioStreamDescriptor(pid: 0x1100, coding: .dtsHDMaster, languageCode: "eng"),
                BluRayAudioStreamDescriptor(pid: 0x1101, coding: .ac3, languageCode: nil),
            ]
        )
    }

    private func facts(
        playlist: UInt32,
        minutes: UInt64,
        angles: UInt32 = 1,
        clips: UInt32 = 1,
        mainFeature: Bool = false,
        completeClip: Bool = true,
        stereoStream: Bool = true,
        audio: [BlurayTitleFacts.Stream] = [],
        subtitles: [BlurayTitleFacts.Stream] = []
    ) -> BlurayTitleFacts {
        BlurayTitleFacts(
            playlist: playlist,
            durationTicks: minutes * 60 * 90_000,
            angleCount: angles,
            clipCount: clips,
            isMainFeature: mainFeature,
            coversCompleteClip: completeClip,
            hasStereoStream: stereoStream,
            baseViewIsRightEye: false,
            clipID: String(format: "%05u", playlist),
            audio: audio,
            subtitles: subtitles
        )
    }
}

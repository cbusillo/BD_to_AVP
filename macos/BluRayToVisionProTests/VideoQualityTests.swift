import XCTest
@testable import BluRayToVisionPro

final class VideoQualityTests: XCTestCase {
    func testQualityStepIdentifiersAndLabelsAreStable() {
        XCTAssertEqual(
            QualityStep.allCases.map(\.rawValue),
            [
                "space_saver",
                "compact",
                "efficient",
                "balanced",
                "detailed",
                "high_detail",
                "maximum_detail",
            ]
        )
        XCTAssertEqual(QualityStep.allCases.map(\.ordinal), Array(1 ... 7))
        XCTAssertEqual(
            QualityStep.allCases.map(\.title),
            [
                "Space Saver",
                "Compact",
                "Efficient",
                "Balanced",
                "Detailed",
                "High Detail",
                "Maximum Detail",
            ]
        )
        XCTAssertTrue(QualityStep.allCases.allSatisfy { !$0.detail.isEmpty })
    }

    func testCatalogPublishesOnlyCheckedBalancedMappings() {
        XCTAssertEqual(
            VideoQualityCatalog.mapping(for: .balanced, target: .directMVHEVC),
            .direct(quality: 0.7)
        )
        XCTAssertEqual(
            VideoQualityCatalog.mapping(for: .balanced, target: .directMVHEVCMetalFX2x),
            .direct(quality: 0.6)
        )
        XCTAssertEqual(
            VideoQualityCatalog.mapping(for: .balanced, target: .generatedMVHEVC),
            .generated(eyeBitrateMbps: 20, mergeQuality: 75)
        )
        XCTAssertEqual(
            VideoQualityCatalog.mapping(for: .balanced, target: .fileUpscale),
            .upscale(quality: 75)
        )
        XCTAssertNil(VideoQualityCatalog.mapping(for: .balanced, target: .av1Stereo))
        XCTAssertNil(VideoQualityCatalog.mapping(for: .detailed, target: .directMVHEVC))
        XCTAssertEqual(VideoQualityCatalog.selectableMVHEVCSteps, [.balanced])
    }

    func testDefaultOptionsUseBalancedIntentAndRetainIndependentCustomValues() {
        let options = EncodingOptions()

        XCTAssertEqual(options.videoQuality.mode, .ladder)
        XCTAssertEqual(options.videoQuality.selectedStep, .balanced)
        XCTAssertEqual(options.mvHEVC.directFinalBitrate.mode, .automatic)
        XCTAssertEqual(options.mvHEVC.directFinalBitrate.customMbps, 40)
        XCTAssertEqual(options.mvHEVC.generatedEyeBitrate.mode, .automatic)
        XCTAssertEqual(options.mvHEVC.generatedEyeBitrate.customMbps, 20)
        XCTAssertEqual(options.mvHEVC.generatedMergeQuality, 75)
        XCTAssertEqual(options.upscaleQuality, 75)
        XCTAssertEqual(options.videoQuality.custom.directFinalBitrate.mode, .automatic)
        XCTAssertEqual(options.videoQuality.custom.generatedEyeBitrate.mode, .automatic)
    }

    func testSwitchingBetweenCustomAndBalancedIsLossless() throws {
        var options = EncodingOptions()
        options.editCustomQuality { custom in
            custom.directFinalBitrate = BitratePreference(mode: .custom, customMbps: 48)
            custom.generatedEyeBitrate = BitratePreference(mode: .custom, customMbps: 35)
            custom.generatedMergeQuality = 84
            custom.av1CRF = 24
            custom.upscaleQuality = 87
        }
        let custom = options.videoQuality.custom

        try options.selectQualityStep(.balanced)

        XCTAssertEqual(options.videoQuality.mode, .ladder)
        XCTAssertEqual(options.videoQuality.custom, custom)
        XCTAssertEqual(options.mvHEVC.directFinalBitrate.mode, .automatic)
        XCTAssertEqual(options.mvHEVC.directFinalBitrate.customMbps, 48)
        XCTAssertEqual(options.mvHEVC.generatedEyeBitrate.mode, .automatic)
        XCTAssertEqual(options.mvHEVC.generatedEyeBitrate.customMbps, 35)
        XCTAssertEqual(options.mvHEVC.generatedMergeQuality, 75)
        XCTAssertEqual(options.av1CRF, 24)
        XCTAssertEqual(options.upscaleQuality, 75)

        options.selectCustomQuality()

        XCTAssertEqual(options.videoQuality.mode, .custom)
        XCTAssertEqual(options.mvHEVC.directFinalBitrate, custom.directFinalBitrate)
        XCTAssertEqual(options.mvHEVC.generatedEyeBitrate, custom.generatedEyeBitrate)
        XCTAssertEqual(options.mvHEVC.generatedMergeQuality, custom.generatedMergeQuality)
        XCTAssertEqual(options.av1CRF, custom.av1CRF)
        XCTAssertEqual(options.upscaleQuality, custom.upscaleQuality)
    }

    func testUnsupportedKnownStepFailsClosed() {
        for step in QualityStep.allCases where step != .balanced {
            var options = EncodingOptions()

            XCTAssertThrowsError(try options.selectQualityStep(step)) { error in
                XCTAssertEqual(error as? VideoQualityStateError, .unsupportedStep(step))
            }
            XCTAssertEqual(options.videoQuality.selectedStep, .balanced)
        }
    }

    func testSelectionDistinguishesCustomFromBalancedWithoutFallbackAlias() {
        var options = EncodingOptions()

        XCTAssertEqual(options.videoQuality.selection, .step(.balanced))
        XCTAssertEqual(options.videoQuality.displayTitle, "Balanced")

        options.selectCustomQuality()

        XCTAssertEqual(options.videoQuality.selection, .custom)
        XCTAssertEqual(options.videoQuality.displayTitle, "Custom")
        XCTAssertEqual(options.videoQuality.lastLadderStep, .balanced)
    }

    func testSelectingAV1RestoresRetainedCustomCRF() throws {
        var options = EncodingOptions()
        options.editCustomQuality { custom in
            custom.av1CRF = 24
        }
        try options.selectQualityStep(.balanced)

        options.selectVideoOutputMode(.av1Stereo)

        XCTAssertEqual(options.videoOutputMode, .av1Stereo)
        XCTAssertEqual(options.videoQuality.selection, .custom)
        XCTAssertEqual(options.av1CRF, 24)
    }

    func testSelectingLadderForAV1FailsWithoutMutatingCustomIntent() {
        var options = EncodingOptions()
        options.editCustomQuality { custom in
            custom.av1CRF = 24
        }
        options.selectVideoOutputMode(.av1Stereo)
        let before = options

        XCTAssertThrowsError(try options.selectQualityStep(.balanced)) { error in
            XCTAssertEqual(error as? VideoQualityStateError, .incompatibleOutputMode)
        }
        XCTAssertEqual(options, before)
    }

    func testSingleExpertEditPreservesUnrelatedAutomaticBitratePolicies() {
        var options = EncodingOptions()

        options.editCustomQuality { custom in
            custom.generatedMergeQuality = 84
        }

        XCTAssertEqual(options.videoQuality.mode, .custom)
        XCTAssertEqual(options.mvHEVC.directFinalBitrate.mode, .automatic)
        XCTAssertEqual(options.mvHEVC.directFinalBitrate.customMbps, 40)
        XCTAssertEqual(options.mvHEVC.generatedEyeBitrate.mode, .automatic)
        XCTAssertEqual(options.mvHEVC.generatedEyeBitrate.customMbps, 20)
        XCTAssertEqual(options.mvHEVC.generatedMergeQuality, 84)
        XCTAssertEqual(options.upscaleQuality, 75)
    }

    func testInvalidExplicitIntentDowngradesToCurrentCustomValues() {
        let unsupported = VideoQualityIntent(
            mode: .ladder,
            lastLadderStep: .detailed,
            custom: .defaults
        )
        let unsupportedOptions = EncodingOptions(videoQuality: unsupported)
        XCTAssertEqual(unsupportedOptions.videoQuality.mode, .custom)
        XCTAssertEqual(unsupportedOptions.videoOutputMode, .mvHEVC)

        let av1Options = EncodingOptions(
            videoOutputMode: .av1Stereo,
            videoQuality: .balanced,
            av1CRF: 24
        )
        XCTAssertEqual(av1Options.videoQuality.mode, .custom)
        XCTAssertEqual(av1Options.videoOutputMode, .av1Stereo)
        XCTAssertEqual(av1Options.av1CRF, 24)
    }

    func testAutomaticNilGeneratedRetentionSurvivesBalancedRoundTrip() throws {
        var retained = VideoQualityCustomValues.defaults
        retained.generatedEyeBitrate = BitratePreference(mode: .automatic, customMbps: nil)
        var options = EncodingOptions(videoQuality: .custom(values: retained))

        try options.selectQualityStep(.balanced)
        let normalized = try options.normalizedQualityState()
        let roundTrip = try JSONDecoder().decode(
            EncodingOptions.self,
            from: JSONEncoder().encode(options)
        )

        XCTAssertEqual(normalized.videoQuality.mode, .ladder)
        XCTAssertNil(normalized.mvHEVC.generatedEyeBitrate.customMbps)
        XCTAssertEqual(roundTrip.videoQuality.mode, .ladder)
        XCTAssertNil(roundTrip.videoQuality.custom.generatedEyeBitrate.customMbps)
        XCTAssertNil(roundTrip.mvHEVC.generatedEyeBitrate.customMbps)
    }

    func testLegacyAV1AndExplicitGeneratedCustomValuesMigrateToCustom() {
        let av1 = EncodingOptions(videoOutputMode: .av1Stereo, av1CRF: 24)
        XCTAssertEqual(av1.videoQuality.mode, .custom)
        XCTAssertEqual(av1.videoQuality.custom.av1CRF, 24)

        let generatedCustom = EncodingOptions(
            mvHEVC: MVHEVCOptions(
                generatedEyeBitrate: BitratePreference(mode: .custom, customMbps: 20),
                generatedMergeQuality: 75
            )
        )
        XCTAssertEqual(generatedCustom.videoQuality.mode, .custom)
        XCTAssertEqual(generatedCustom.videoQuality.custom.generatedEyeBitrate.mode, .custom)
        XCTAssertEqual(generatedCustom.videoQuality.custom.generatedEyeBitrate.customMbps, 20)
    }

    func testUncoordinatedConcreteEditNormalizesToCustomWithoutChangingValues() throws {
        var options = EncodingOptions()
        options.mvHEVC.generatedMergeQuality = 84

        let normalized = try options.normalizedQualityState()

        XCTAssertEqual(normalized.videoQuality.mode, .custom)
        XCTAssertEqual(normalized.videoQuality.custom.generatedMergeQuality, 84)
        XCTAssertEqual(normalized.mvHEVC.generatedMergeQuality, 84)
    }

    func testBuiltInProfilesHaveExplicitReviewedIntent() {
        XCTAssertEqual(BuiltInProfile.balanced.options.videoQuality.mode, .ladder)
        XCTAssertEqual(BuiltInProfile.balanced.options.videoQuality.selectedStep, .balanced)
        XCTAssertEqual(BuiltInProfile.originalResolution.options.videoQuality.mode, .custom)
        XCTAssertEqual(BuiltInProfile.fourKUpscale.options.videoQuality.mode, .custom)
    }
}

import Foundation
import XCTest
@testable import BluRayToVisionPro

final class VideoRouteQualitySummaryTests: XCTestCase {
    func testCatalogQualityTitlesCoverEveryTargetAndStep() {
        for target in VideoQualityTarget.allCases {
            for step in QualityStep.allCases {
                let expected = VideoQualityCatalog.mapping(for: step, target: target) == nil
                    ? "Custom"
                    : step.title

                XCTAssertEqual(
                    VideoQualityCatalog.qualityTitle(for: .step(step), target: target),
                    expected,
                    "Unexpected title for \(target.rawValue) × \(step.rawValue)"
                )
            }
        }
    }

    func testWorkerRouteSummariesUseOnlyMatchingMappedQualityTitles() {
        for target in VideoQualityTarget.allCases {
            for step in QualityStep.allCases {
                let report = report(for: target, step: step)
                let expectedTitle = expectedTitle(for: target, step: step)

                XCTAssertTrue(
                    report.settingsSummary.hasPrefix(expectedTitle),
                    "Unexpected title for \(target.rawValue) × \(step.rawValue): \(report.settingsSummary)"
                )
                if expectedTitle == "Custom" {
                    XCTAssertFalse(report.settingsSummary.contains(step.title))
                }
            }
        }
    }

    func testWorkerRouteSummariesTreatMissingOrStaleMappingVersionsAsCustom() {
        for mappingVersion in [nil, VideoQualityCatalog.mappingVersion - 1] {
            let report = VideoRouteReport(
                intent: "generated",
                selected: "generated_mv_hevc",
                reason: "generated_route_requested",
                bitrateMbps: nil,
                eyeBitrateMbps: 20,
                mergeQuality: 75,
                crf: nil,
                fallbackReason: nil,
                fallbackTiming: nil,
                qualityIntent: VideoRouteReport.QualityIntent(
                    mode: QualityIntentMode.ladder.rawValue,
                    step: QualityStep.balanced.rawValue,
                    mappingVersion: mappingVersion
                )
            )

            XCTAssertEqual(report.settingsSummary, "Custom · 20 Mbps per eye · merge 75")
        }
    }

    func testGeneratedUpscaleAndExistingArtifactRequireConcreteMatchingValues() {
        let generatedUpscale = VideoRouteReport(
            intent: "generated",
            selected: "generated_mv_hevc",
            reason: "upscale_crop_requires_generated_artifacts",
            bitrateMbps: nil,
            eyeBitrateMbps: 20,
            mergeQuality: 75,
            crf: nil,
            fallbackReason: nil,
            fallbackTiming: nil,
            upscaleQuality: 66,
            qualityIntent: ladderIntent(.balanced)
        )
        XCTAssertEqual(generatedUpscale.settingsSummary, "Custom · 20 Mbps per eye · merge 75 · upscale quality 66")

        let existingArtifact = VideoRouteReport(
            intent: "existing_artifact",
            selected: "existing_artifact",
            reason: "resume_uses_existing_video_artifact",
            bitrateMbps: nil,
            eyeBitrateMbps: nil,
            mergeQuality: nil,
            crf: nil,
            fallbackReason: nil,
            fallbackTiming: nil,
            upscaleQuality: 66,
            qualityIntent: ladderIntent(.maximumDetail)
        )
        XCTAssertEqual(existingArtifact.settingsSummary, "Custom · Upscale quality 66")

        let noReencode = VideoRouteReport(
            intent: "existing_artifact",
            selected: "existing_artifact",
            reason: "resume_uses_existing_video_artifact",
            bitrateMbps: nil,
            eyeBitrateMbps: nil,
            mergeQuality: nil,
            crf: nil,
            fallbackReason: nil,
            fallbackTiming: nil,
            qualityIntent: ladderIntent(.maximumDetail)
        )
        XCTAssertEqual(noReencode.settingsSummary, "No video re-encode")
    }

    func testRoutePlansLabelUnsupportedGeneratedAndExistingArtifactQualityCustom() throws {
        var generatedOptions = EncodingOptions()
        generatedOptions.videoQuality = VideoQualityIntent(
            mode: .ladder,
            lastLadderStep: .maximumDetail,
            custom: .defaults
        )
        generatedOptions.upscaleEnabled = true
        generatedOptions.cropBlackBars = true
        let generatedPlan = VideoRoutePlan(encoding: generatedOptions)
        XCTAssertEqual(generatedPlan.kind, .generatedMVHEVC)
        XCTAssertTrue(generatedPlan.settingsSummary.hasPrefix("Custom ·"))
        XCTAssertTrue(generatedPlan.settingsSummary.contains("20 Mbps per eye · merge 75"))

        var existingOptions = generatedOptions
        existingOptions.upscaleEnabled = true
        let existingPlan = VideoRoutePlan(
            encoding: existingOptions,
            job: JobOptions(startStage: .upscaleVideo)
        )
        XCTAssertEqual(existingPlan.kind, .existingArtifact)
        XCTAssertEqual(existingPlan.settingsSummary, "Custom · upscale quality 75")
    }

    private func report(for target: VideoQualityTarget, step: QualityStep) -> VideoRouteReport {
        let mapping = VideoQualityCatalog.mapping(for: step, target: target)
        switch target {
        case .directMVHEVC, .directMVHEVCMetalFX2x:
            let quality: Double
            if case let .direct(mappedQuality) = mapping {
                quality = mappedQuality
            } else {
                quality = 0.91
            }
            return VideoRouteReport(
                intent: "automatic",
                selected: "direct_mv_hevc",
                reason: "direct_eligible",
                bitrateMbps: nil,
                eyeBitrateMbps: nil,
                mergeQuality: nil,
                crf: nil,
                fallbackReason: nil,
                fallbackTiming: nil,
                rateControl: "quality",
                quality: quality,
                upscaleMode: target == .directMVHEVCMetalFX2x ? "metalfx" : nil,
                qualityIntent: ladderIntent(step)
            )
        case .generatedMVHEVC:
            let eyeBitrate: Int
            let mergeQuality: Int
            if case let .generated(mappedEyeBitrate, mappedMergeQuality) = mapping {
                eyeBitrate = mappedEyeBitrate
                mergeQuality = mappedMergeQuality
            } else {
                eyeBitrate = 33
                mergeQuality = 88
            }
            return VideoRouteReport(
                intent: "generated",
                selected: "generated_mv_hevc",
                reason: "generated_route_requested",
                bitrateMbps: nil,
                eyeBitrateMbps: eyeBitrate,
                mergeQuality: mergeQuality,
                crf: nil,
                fallbackReason: nil,
                fallbackTiming: nil,
                qualityIntent: ladderIntent(step)
            )
        case .fileUpscale:
            let quality: Int
            if case let .upscale(mappedQuality) = mapping {
                quality = mappedQuality
            } else {
                quality = 66
            }
            return VideoRouteReport(
                intent: "existing_artifact",
                selected: "existing_artifact",
                reason: "resume_uses_existing_video_artifact",
                bitrateMbps: nil,
                eyeBitrateMbps: nil,
                mergeQuality: nil,
                crf: nil,
                fallbackReason: nil,
                fallbackTiming: nil,
                upscaleQuality: quality,
                qualityIntent: ladderIntent(step)
            )
        case .av1Stereo:
            return VideoRouteReport(
                intent: "encode",
                selected: "av1",
                reason: "av1_output_requested",
                bitrateMbps: nil,
                eyeBitrateMbps: nil,
                mergeQuality: nil,
                crf: 32,
                fallbackReason: nil,
                fallbackTiming: nil,
                qualityIntent: ladderIntent(step)
            )
        }
    }

    private func expectedTitle(for target: VideoQualityTarget, step: QualityStep) -> String {
        switch target {
        case .directMVHEVC, .directMVHEVCMetalFX2x:
            step.title
        case .generatedMVHEVC:
            step == .balanced ? step.title : "Custom"
        case .fileUpscale:
            [.balanced, .detailed].contains(step) ? step.title : "Custom"
        case .av1Stereo:
            "Custom"
        }
    }

    private func ladderIntent(_ step: QualityStep) -> VideoRouteReport.QualityIntent {
        VideoRouteReport.QualityIntent(
            mode: "ladder",
            step: step.rawValue,
            mappingVersion: VideoQualityCatalog.mappingVersion
        )
    }
}

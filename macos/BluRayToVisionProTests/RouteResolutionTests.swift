import XCTest
@testable import BluRayToVisionPro

final class RouteResolutionTests: XCTestCase {
    func testEveryTriggerAndQualityStepProducesAValidDraftOrConflict() throws {
        let edits: [(RouteQualityTrigger, RouteQualityEdit)] = [
            (.generatedRouteRequirement, .qualityStep(.maximumDetail)),
            (.reusableIntermediates, .reusableIntermediates(true)),
            (.softwareEncoder, .softwareEncoder(true)),
            (.restartStage, .restartStage(.createLeftRightFiles)),
            (.upscaleCrop, .upscaleEnabled(true)),
            (.fieldOfView, .fieldOfView(0)),
            (.resolutionOverride, .resolutionOverride("1280x720")),
            (.outputMode, .outputMode(.av1Stereo)),
        ]

        for qualityStep in QualityStep.allCases {
            var options = ConversionOptions()
            try options.encoding.selectQualityStep(qualityStep)

            for (expectedTrigger, edit) in edits {
                let proposal = RouteQualityEngine.propose(options: options, edit: edit)
                switch proposal {
                case let .resolved(draft):
                    XCTAssertEqual(
                        RouteQualityEngine.validate(draft.options),
                        .success(draft),
                        "\(expectedTrigger.rawValue)/\(qualityStep.rawValue)"
                    )
                case let .conflict(conflict):
                    XCTAssertEqual(conflict.trigger, expectedTrigger, "\(qualityStep.rawValue)")
                    XCTAssertNil(conflict.selectedResolutionID)
                    XCTAssertEqual(conflict.mappingVersion, VideoQualityCatalog.mappingVersion)
                    XCTAssertEqual(conflict.resolutions.count, 3)
                    XCTAssertTrue(conflict.resolutions.contains { $0.choice == .preservePriorIntent && $0.isAvailable })
                    XCTAssertTrue(conflict.resolutions.contains { $0.choice == .useCustomExactSettings && $0.isAvailable })

                    for option in conflict.resolutions where option.isAvailable {
                        let result = RouteQualityEngine.resolve(option, conflict: conflict)
                        guard case let .success(draft) = result else {
                            return XCTFail("Resolution failed for \(expectedTrigger.rawValue)/\(qualityStep.rawValue): \(result)")
                        }
                        XCTAssertEqual(RouteQualityEngine.validate(draft.options), .success(draft))
                        XCTAssertEqual(
                            try XCTUnwrap(RouteQualityEngine.resolve(option, conflict: conflict).get().options),
                            draft.options
                        )
                    }
                case let .invalid(message):
                    XCTFail("Unexpected invalid proposal for \(expectedTrigger.rawValue)/\(qualityStep.rawValue): \(message)")
                }
            }
        }
    }

    func testConflictDoesNotMutateLiveOptionsAndStartsUnselected() throws {
        var options = ConversionOptions()
        try options.encoding.selectQualityStep(.maximumDetail)
        let original = options
        let state = RouteQualityResolutionState()

        state.apply(.reusableIntermediates(true), to: &options)

        XCTAssertEqual(options, original)
        XCTAssertNotNil(state.conflict)
        XCTAssertNil(state.conflict?.selectedResolutionID)
        XCTAssertEqual(state.blockReason, state.conflict?.blockReason)
    }

    func testMappedAndCustomResolutionsAreStableAndIdempotent() throws {
        var options = ConversionOptions()
        try options.encoding.selectQualityStep(.maximumDetail)
        let proposal = RouteQualityEngine.propose(
            options: options,
            edit: .reusableIntermediates(true)
        )
        guard case let .conflict(conflict) = proposal else {
            return XCTFail("Expected generated-route conflict")
        }

        let mapped = try XCTUnwrap(conflict.resolutions.first { $0.choice == .keepRequestedWorkflow })
        XCTAssertEqual(mapped.mappedStep, .balanced)
        XCTAssertEqual(mapped.id, "keep_requested_workflow:v\(VideoQualityCatalog.mappingVersion)")

        let mappedDraft = try XCTUnwrap(try? RouteQualityEngine.resolve(mapped, conflict: conflict).get())
        XCTAssertEqual(mappedDraft.options.encoding.videoQuality.selection, .step(.balanced))
        XCTAssertEqual(try XCTUnwrap(try? RouteQualityEngine.resolve(mapped, conflict: conflict).get()).options, mappedDraft.options)

        let custom = try XCTUnwrap(conflict.resolutions.first { $0.choice == .useCustomExactSettings })
        let customDraft = try XCTUnwrap(try? RouteQualityEngine.resolve(custom, conflict: conflict).get())
        XCTAssertEqual(customDraft.options.encoding.videoQuality.mode, .custom)
        XCTAssertEqual(customDraft.options.encoding.mvHEVC.generatedMergeQuality, options.encoding.mvHEVC.generatedMergeQuality)
        XCTAssertEqual(try XCTUnwrap(try? RouteQualityEngine.resolve(custom, conflict: conflict).get()).options, customDraft.options)
    }

    func testRendererCoversAllResolutionChoices() throws {
        var options = ConversionOptions()
        try options.encoding.selectQualityStep(.maximumDetail)
        guard case let .conflict(conflict) = RouteQualityEngine.propose(
            options: options,
            edit: .reusableIntermediates(true)
        ) else {
            return XCTFail("Expected route-quality conflict")
        }

        for option in conflict.resolutions {
            XCTAssertFalse(option.id.isEmpty)
            XCTAssertFalse(option.title.isEmpty)
            XCTAssertFalse(option.detail.isEmpty)
        }
    }
}

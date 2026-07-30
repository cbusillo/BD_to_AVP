import CoreMedia
import XCTest
@testable import SpatialPlaybackProbe

final class PlaybackValidationTests: XCTestCase {
    func testPreparationRequiresPlayerAndFingerprint() {
        XCTAssertFalse(
            PlaybackValidationRules.preparationCompleted(playerReady: false, fingerprintReady: false)
        )
        XCTAssertFalse(
            PlaybackValidationRules.preparationCompleted(playerReady: true, fingerprintReady: false)
        )
        XCTAssertFalse(
            PlaybackValidationRules.preparationCompleted(playerReady: false, fingerprintReady: true)
        )
        XCTAssertTrue(
            PlaybackValidationRules.preparationCompleted(playerReady: true, fingerprintReady: true)
        )
    }

    func testTransferredDocumentMovieDoesNotRequireSecondFullFileCopy() {
        let documentsURL = URL(fileURLWithPath: "/container/Documents", isDirectory: true)

        XCTAssertFalse(
            PlaybackProbeModel.requiresAutomaticAssetCacheCopy(
                sourceURL: documentsURL.appendingPathComponent("Probe.mov"),
                documentsURL: documentsURL
            )
        )
        XCTAssertFalse(
            PlaybackProbeModel.requiresAutomaticAssetCacheCopy(
                sourceURL: documentsURL.appendingPathComponent("Nested/Probe.mov"),
                documentsURL: documentsURL
            )
        )
        XCTAssertTrue(
            PlaybackProbeModel.requiresAutomaticAssetCacheCopy(
                sourceURL: URL(fileURLWithPath: "/external/Probe.mov"),
                documentsURL: documentsURL
            )
        )
    }

    func testPresentationExpectationDefaultsToStereoAndAcceptsSpatialOverride() {
        XCTAssertEqual(PlaybackPresentationExpectation.resolve(environment: [:]), .stereo)
        XCTAssertEqual(
            PlaybackPresentationExpectation.resolve(
                environment: [PlaybackPresentationExpectation.environmentKey: "spatial"]
            ),
            .spatial
        )
        XCTAssertTrue(PlaybackPresentationExpectation.stereo.matches(isStereo: true, isSpatial: false))
        XCTAssertFalse(PlaybackPresentationExpectation.stereo.matches(isStereo: true, isSpatial: true))
        XCTAssertTrue(PlaybackPresentationExpectation.spatial.matches(isStereo: true, isSpatial: true))
    }

    func testPassingChecksAndObservationsProducePass() {
        let checks = PlaybackCheckID.allCases.map {
            PlaybackCheck(id: $0, status: .passed, detail: "Passed")
        }
        let observations = PlaybackObservations(
            videoRemainedVisible: .yes,
            appearedThreeDimensional: .yes,
            eyeOrderAppearedCorrect: .yes
        )

        XCTAssertEqual(
            PlaybackValidationRules.result(checks: checks, observations: observations),
            .passed
        )
    }

    func testAutomaticFailureProducesFailure() {
        var checks = PlaybackCheckID.allCases.map {
            PlaybackCheck(id: $0, status: .passed, detail: "Passed")
        }
        checks[0].status = .failed
        let observations = PlaybackObservations(
            videoRemainedVisible: .yes,
            appearedThreeDimensional: .yes,
            eyeOrderAppearedCorrect: .yes
        )

        XCTAssertEqual(
            PlaybackValidationRules.result(checks: checks, observations: observations),
            .failed
        )
    }

    func testNegativeObservationProducesFailure() {
        let checks = PlaybackCheckID.allCases.map {
            PlaybackCheck(id: $0, status: .passed, detail: "Passed")
        }
        let observations = PlaybackObservations(
            videoRemainedVisible: .no,
            appearedThreeDimensional: .yes,
            eyeOrderAppearedCorrect: .yes
        )

        XCTAssertEqual(
            PlaybackValidationRules.result(checks: checks, observations: observations),
            .failed
        )
    }

    func testUncertainObservationNeedsReview() {
        let checks = PlaybackCheckID.allCases.map {
            PlaybackCheck(id: $0, status: .passed, detail: "Passed")
        }
        let observations = PlaybackObservations(
            videoRemainedVisible: .yes,
            appearedThreeDimensional: .unsure,
            eyeOrderAppearedCorrect: .yes
        )

        XCTAssertEqual(
            PlaybackValidationRules.result(checks: checks, observations: observations),
            .needsReview
        )
    }

    func testReportContainsFilenameWithoutSourcePath() throws {
        let report = PlaybackValidationReport(
            schemaVersion: 4,
            validatorVersion: "0.1.0",
            validatorBuild: "42",
            generatedAt: "2026-07-17T20:00:00Z",
            operatingSystem: "visionOS",
            source: PlaybackSourceSummary(
                fileName: "Movie.mov",
                sha256: String(repeating: "a", count: 64),
                sizeBytes: 1_024,
                durationSeconds: 24,
                audioOptionCount: 2,
                subtitleOptionCount: 1
            ),
            decode: PlaybackDecodeSummary(
                videoCodec: .av1,
                codecTag: "av01",
                route: .fallback,
                av1HardwareDecodeSupported: false,
                stereoMVHEVCDecodeSupported: true,
                independentFrameDecodeSucceeded: true,
                independentFrameDecodeDetail: "decoded_first_frame"
            ),
            runtime: PlaybackRuntimeSummary(
                hardwareModel: "RealityDevice14,1",
                sustainedPlaybackSeconds: 30,
                thermalStateBefore: "nominal",
                thermalStateAfter: "fair"
            ),
            presentation: PlaybackPresentationSummary(
                expectation: .stereo,
                viewingMode: "stereo",
                spatialVideoMode: "screen",
                immersiveViewingMode: "none"
            ),
            automaticChecks: [],
            observations: PlaybackObservations(
                videoRemainedVisible: .yes,
                appearedThreeDimensional: .yes,
                eyeOrderAppearedCorrect: .yes
            ),
            result: .passed
        )

        let encodedReport = try JSONEncoder().encode(report)
        let reportText = try XCTUnwrap(String(data: encodedReport, encoding: .utf8))

        XCTAssertTrue(reportText.contains("Movie.mov"))
        XCTAssertTrue(reportText.contains(String(repeating: "a", count: 64)))
        XCTAssertTrue(reportText.contains("audioOptionCount"))
        XCTAssertTrue(reportText.contains("subtitleOptionCount"))
        XCTAssertTrue(reportText.contains("spatialVideoMode"))
        XCTAssertTrue(reportText.contains("expectation"))
        XCTAssertTrue(reportText.contains("av01"))
        XCTAssertTrue(reportText.contains("fallback"))
        XCTAssertTrue(reportText.contains("RealityDevice14,1"))
        XCTAssertTrue(reportText.contains("eyeOrderAppearedCorrect"))
        XCTAssertFalse(reportText.contains("/Users/"))
        XCTAssertFalse(reportText.contains("sourcePath"))
    }

    func testCodecIdentificationAndDecodeAssessmentAreCodecAware() {
        let av1Format = PlaybackVideoFormat(mediaSubtype: FourCharCode(0x6176_3031))
        let hevcFormat = PlaybackVideoFormat(mediaSubtype: FourCharCode(0x6876_6331))
        let unknownFormat = PlaybackVideoFormat(mediaSubtype: FourCharCode(0x7670_3039))

        XCTAssertEqual(av1Format, PlaybackVideoFormat(mediaSubtype: FourCharCode(0x6176_3031)))
        XCTAssertEqual(av1Format.codec, .av1)
        XCTAssertEqual(av1Format.codecTag, "av01")
        XCTAssertEqual(hevcFormat.codec, .hevc)
        XCTAssertEqual(unknownFormat.codec, .unknown)

        let fallback = PlaybackDecodeAssessment.evaluate(
            format: av1Format,
            playerReady: true,
            av1HardwareDecodeSupported: false,
            stereoMVHEVCDecodeSupported: true,
            independentFrameDecodeSucceeded: true
        )
        XCTAssertEqual(fallback.route, .fallback)
        XCTAssertTrue(fallback.passesCapabilityCheck)

        let unsupportedHEVC = PlaybackDecodeAssessment.evaluate(
            format: hevcFormat,
            playerReady: true,
            av1HardwareDecodeSupported: true,
            stereoMVHEVCDecodeSupported: false,
            independentFrameDecodeSucceeded: true
        )
        XCTAssertEqual(unsupportedHEVC.route, .unavailable)
        XCTAssertFalse(unsupportedHEVC.passesCapabilityCheck)

        let unknown = PlaybackDecodeAssessment.evaluate(
            format: unknownFormat,
            playerReady: true,
            av1HardwareDecodeSupported: true,
            stereoMVHEVCDecodeSupported: true,
            independentFrameDecodeSucceeded: true
        )
        XCTAssertFalse(unknown.passesCapabilityCheck)

        let failedFallback = PlaybackDecodeAssessment.evaluate(
            format: av1Format,
            playerReady: true,
            av1HardwareDecodeSupported: false,
            stereoMVHEVCDecodeSupported: true,
            independentFrameDecodeSucceeded: false
        )
        XCTAssertEqual(failedFallback.route, .unavailable)
        XCTAssertFalse(failedFallback.passesCapabilityCheck)

        let failedHardwareDecode = PlaybackDecodeAssessment.evaluate(
            format: av1Format,
            playerReady: true,
            av1HardwareDecodeSupported: true,
            stereoMVHEVCDecodeSupported: true,
            independentFrameDecodeSucceeded: false
        )
        XCTAssertEqual(failedHardwareDecode.route, .unavailable)
        XCTAssertFalse(failedHardwareDecode.passesCapabilityCheck)

        let notReadyFallback = PlaybackDecodeAssessment.evaluate(
            format: av1Format,
            playerReady: false,
            av1HardwareDecodeSupported: false,
            stereoMVHEVCDecodeSupported: true,
            independentFrameDecodeSucceeded: true
        )
        XCTAssertEqual(notReadyFallback.route, .unavailable)
        XCTAssertFalse(notReadyFallback.passesCapabilityCheck)
    }

    func testIncorrectEyeOrderFailsAndUnansweredEyeOrderIsIncomplete() {
        let checks = PlaybackCheckID.allCases.map {
            PlaybackCheck(id: $0, status: .passed, detail: "Passed")
        }
        let failedObservations = PlaybackObservations(
            videoRemainedVisible: .yes,
            appearedThreeDimensional: .yes,
            eyeOrderAppearedCorrect: .no
        )
        XCTAssertEqual(
            PlaybackValidationRules.result(checks: checks, observations: failedObservations),
            .failed
        )

        let incompleteObservations = PlaybackObservations(
            videoRemainedVisible: .yes,
            appearedThreeDimensional: .yes
        )
        XCTAssertFalse(incompleteObservations.isComplete)
    }

    func testSeekRequiresPlaybackAdvanceAndFreshSpatialRendering() {
        let passingEvidence = PlaybackSeekEvidence(
            seekFinished: true,
            targetErrorSeconds: 0.25,
            allowedTargetErrorSeconds: 1,
            playbackAdvanceSeconds: 0.4,
            requiredPlaybackAdvanceSeconds: 0.3,
            renderingReady: true,
            stereoPresentation: true,
            spatialPresentation: true,
            requiresSpatialPresentation: true
        )

        XCTAssertTrue(PlaybackValidationRules.seekPassed(passingEvidence))
        XCTAssertFalse(
            PlaybackValidationRules.seekPassed(
                PlaybackSeekEvidence(
                    seekFinished: true,
                    targetErrorSeconds: 0.25,
                    allowedTargetErrorSeconds: 1,
                    playbackAdvanceSeconds: 0,
                    requiredPlaybackAdvanceSeconds: 0.3,
                    renderingReady: true,
                    stereoPresentation: true,
                    spatialPresentation: true,
                    requiresSpatialPresentation: true
                )
            )
        )
        XCTAssertFalse(
            PlaybackValidationRules.seekPassed(
                PlaybackSeekEvidence(
                    seekFinished: true,
                    targetErrorSeconds: 0.25,
                    allowedTargetErrorSeconds: 1,
                    playbackAdvanceSeconds: 0.4,
                    requiredPlaybackAdvanceSeconds: 0.3,
                    renderingReady: false,
                    stereoPresentation: true,
                    spatialPresentation: true,
                    requiresSpatialPresentation: true
                )
            )
        )
        XCTAssertTrue(
            PlaybackValidationRules.seekPassed(
                PlaybackSeekEvidence(
                    seekFinished: true,
                    targetErrorSeconds: 0.25,
                    allowedTargetErrorSeconds: 1,
                    playbackAdvanceSeconds: 0.4,
                    requiredPlaybackAdvanceSeconds: 0.3,
                    renderingReady: true,
                    stereoPresentation: true,
                    spatialPresentation: false,
                    requiresSpatialPresentation: false
                )
            )
        )
        XCTAssertFalse(
            PlaybackValidationRules.seekPassed(
                PlaybackSeekEvidence(
                    seekFinished: true,
                    targetErrorSeconds: 0.25,
                    allowedTargetErrorSeconds: 1,
                    playbackAdvanceSeconds: 0.4,
                    requiredPlaybackAdvanceSeconds: 0.3,
                    renderingReady: true,
                    stereoPresentation: false,
                    spatialPresentation: false,
                    requiresSpatialPresentation: false
                )
            )
        )
    }

    func testSustainedPlaybackRequiresAdvanceRenderingAndPresentation() {
        XCTAssertTrue(
            PlaybackValidationRules.sustainedPlaybackPassed(
                PlaybackSustainedEvidence(
                    playbackAdvanceSeconds: 30,
                    requiredPlaybackAdvanceSeconds: 30,
                    renderingStayedReady: true,
                    stereoPresentationStayedActive: true,
                    expectedPresentationStayedActive: true
                )
            )
        )
        XCTAssertFalse(
            PlaybackValidationRules.sustainedPlaybackPassed(
                PlaybackSustainedEvidence(
                    playbackAdvanceSeconds: 29,
                    requiredPlaybackAdvanceSeconds: 30,
                    renderingStayedReady: true,
                    stereoPresentationStayedActive: true,
                    expectedPresentationStayedActive: true
                )
            )
        )
    }

    func testArtifactHasherProducesKnownSHA256() async throws {
        let temporaryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("playback-validator-hash-\(UUID().uuidString).txt")
        defer {
            try? FileManager.default.removeItem(at: temporaryURL)
        }
        try Data("abc".utf8).write(to: temporaryURL)

        let digest = try await PlaybackArtifactHasher.sha256Hex(at: temporaryURL)

        XCTAssertEqual(
            digest,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )
    }

    func testArtifactHasherPropagatesCancellation() async throws {
        let temporaryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("playback-validator-cancelled-hash-\(UUID().uuidString).bin")
        defer {
            try? FileManager.default.removeItem(at: temporaryURL)
        }
        try Data(repeating: 0xA5, count: 5 * 1_024 * 1_024).write(to: temporaryURL)

        let task = Task {
            try await PlaybackArtifactHasher.sha256Hex(at: temporaryURL)
        }
        task.cancel()

        do {
            _ = try await task.value
            XCTFail("Expected hashing to stop when its caller is cancelled.")
        } catch is CancellationError {
        } catch {
            XCTFail("Expected CancellationError, got \(error).")
        }
    }

    func testReportStoreWritesNamedArchiveAndLatestCopy() throws {
        let temporaryDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("playback-validator-report-\(UUID().uuidString)", isDirectory: true)
        defer {
            try? FileManager.default.removeItem(at: temporaryDirectory)
        }
        let generatedAt = try XCTUnwrap(ISO8601DateFormatter().date(from: "2026-07-17T23:01:31Z"))
        let reportData = Data("{\"result\":\"failed\"}".utf8)

        let files = try PlaybackReportStore.write(
            reportData,
            sourceFileName: "Probe Movie.mov",
            generatedAt: generatedAt,
            documentsDirectory: temporaryDirectory
        )

        XCTAssertEqual(
            files.archiveURL.lastPathComponent,
            "BD-to-AVP-Playback-Check-Probe-Movie-20260717-230131.json"
        )
        XCTAssertEqual(files.latestURL.lastPathComponent, "Latest-Playback-Report.json")
        XCTAssertEqual(try Data(contentsOf: files.archiveURL), reportData)
        XCTAssertEqual(try Data(contentsOf: files.latestURL), reportData)
    }
}

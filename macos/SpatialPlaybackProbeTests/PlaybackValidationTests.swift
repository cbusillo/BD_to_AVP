import XCTest
@testable import SpatialPlaybackProbe

final class PlaybackValidationTests: XCTestCase {
    func testPassingChecksAndObservationsProducePass() {
        let checks = PlaybackCheckID.allCases.map {
            PlaybackCheck(id: $0, status: .passed, detail: "Passed")
        }
        let observations = PlaybackObservations(
            videoRemainedVisible: .yes,
            appearedThreeDimensional: .yes
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
            appearedThreeDimensional: .yes
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
            appearedThreeDimensional: .yes
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
            appearedThreeDimensional: .unsure
        )

        XCTAssertEqual(
            PlaybackValidationRules.result(checks: checks, observations: observations),
            .needsReview
        )
    }

    func testReportContainsFilenameWithoutSourcePath() throws {
        let report = PlaybackValidationReport(
            schemaVersion: 1,
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
            automaticChecks: [],
            observations: PlaybackObservations(
                videoRemainedVisible: .yes,
                appearedThreeDimensional: .yes
            ),
            result: .passed
        )

        let encodedReport = try JSONEncoder().encode(report)
        let reportText = try XCTUnwrap(String(data: encodedReport, encoding: .utf8))

        XCTAssertTrue(reportText.contains("Movie.mov"))
        XCTAssertTrue(reportText.contains(String(repeating: "a", count: 64)))
        XCTAssertTrue(reportText.contains("audioOptionCount"))
        XCTAssertTrue(reportText.contains("subtitleOptionCount"))
        XCTAssertFalse(reportText.contains("/Users/"))
        XCTAssertFalse(reportText.contains("sourcePath"))
    }

    func testSeekRequiresPlaybackAdvanceAndFreshSpatialRendering() {
        let passingEvidence = PlaybackSeekEvidence(
            seekFinished: true,
            targetErrorSeconds: 0.25,
            allowedTargetErrorSeconds: 1,
            playbackAdvanceSeconds: 0.4,
            requiredPlaybackAdvanceSeconds: 0.3,
            renderingReady: true,
            spatialPresentation: true
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
                    spatialPresentation: true
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
                    spatialPresentation: true
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
}

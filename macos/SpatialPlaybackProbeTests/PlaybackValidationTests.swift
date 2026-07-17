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
            schemaVersion: 2,
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
            presentation: PlaybackPresentationSummary(
                viewingMode: "stereo",
                spatialVideoMode: "screen",
                immersiveViewingMode: "none"
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
        XCTAssertTrue(reportText.contains("spatialVideoMode"))
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

import Foundation
import XCTest
@testable import BluRayToVisionPro

final class ConversionWorkflowTests: XCTestCase {
    func testDraftBuildsPlannedAVPOutput() {
        let draft = ConversionDraft(
            source: ConversionSource(
                kind: .transportStream,
                url: URL(fileURLWithPath: "/Sources/Feature.m2ts")
            ),
            sourceDetails: SourceInspection(
                name: "Feature.m2ts",
                resolution: "1920x1080",
                frameRate: "24000/1001",
                interlaced: false,
                sizeBytes: 10
            ),
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies", isDirectory: true),
            options: ConversionOptions(encoding: BuiltInProfile.balanced.options)
        )

        XCTAssertEqual(draft.proposedOutputURL.path, "/Movies/Feature_AVP.mov")
    }

    func testProfilesHaveStableUniqueIdentifiers() {
        let profiles = BuiltInProfile.allCases.map(\.profile)
        let identifiers = profiles.map(\.id)

        XCTAssertEqual(Set(identifiers).count, profiles.count)
        XCTAssertFalse(BuiltInProfile.balanced.options.upscaleEnabled)
        XCTAssertTrue(BuiltInProfile.fourKUpscale.options.upscaleEnabled)
        XCTAssertEqual(BuiltInProfile.originalResolution.options.hevcQuality, 85)
        XCTAssertEqual(ConversionStage.combineToMVHEVC.rawValue, 5)
        XCTAssertEqual(ConversionStage.upscaleVideo.rawValue, 6)
        XCTAssertEqual(SubtitleLanguage.french.rawValue, "fre")
    }

    func testSourceInferencePreservesProductHierarchy() throws {
        try withTemporaryDirectory { directoryURL in
            let imageURL = directoryURL.appendingPathComponent("Feature.iso")
            let unsupportedImageURL = directoryURL.appendingPathComponent("Feature.img")
            let mkvURL = directoryURL.appendingPathComponent("Feature.mkv")
            let streamURL = directoryURL.appendingPathComponent("Feature.m2ts")
            _ = FileManager.default.createFile(atPath: imageURL.path, contents: Data())
            _ = FileManager.default.createFile(atPath: unsupportedImageURL.path, contents: Data())
            _ = FileManager.default.createFile(atPath: mkvURL.path, contents: Data())
            _ = FileManager.default.createFile(atPath: streamURL.path, contents: Data())

            XCTAssertEqual(ConversionSource.infer(from: imageURL)?.kind, .discImage)
            XCTAssertNil(ConversionSource.infer(from: unsupportedImageURL))
            XCTAssertEqual(ConversionSource.infer(from: mkvURL)?.kind, .matroska)
            XCTAssertEqual(ConversionSource.infer(from: streamURL)?.kind, .transportStream)
        }
    }

    func testBluRayFolderDetectionFindsBDMV() throws {
        try withTemporaryDirectory { directoryURL in
            let discURL = directoryURL.appendingPathComponent("Feature Disc", isDirectory: true)
            let bdmvURL = discURL.appendingPathComponent("BDMV", isDirectory: true)
            let lowercaseDiscURL = directoryURL.appendingPathComponent("Lowercase Disc", isDirectory: true)
            try FileManager.default.createDirectory(at: bdmvURL, withIntermediateDirectories: true)
            try FileManager.default.createDirectory(
                at: lowercaseDiscURL.appendingPathComponent("bdmv", isDirectory: true),
                withIntermediateDirectories: true
            )

            XCTAssertTrue(DiscSourceDetector.isBluRayFolder(discURL))
            XCTAssertTrue(DiscSourceDetector.isBluRayFolder(lowercaseDiscURL))
            XCTAssertEqual(ConversionSource.infer(from: discURL)?.kind, .bluRayFolder)
            XCTAssertEqual(ConversionSource.infer(from: bdmvURL)?.url, discURL)
        }
    }

    func testInsertedDiscDetectionCarriesResolvedDevicePath() throws {
        try withTemporaryDirectory { volumeURL in
            try FileManager.default.createDirectory(
                at: volumeURL.appendingPathComponent("BDMV", isDirectory: true),
                withIntermediateDirectories: true
            )

            let discs = DiscSourceDetector.insertedDiscs(
                in: [volumeURL],
                devicePathResolver: { _ in "/dev/disk9" }
            )

            XCTAssertEqual(discs.count, 1)
            XCTAssertEqual(discs.first?.kind, .physicalDisc)
            XCTAssertEqual(discs.first?.url, volumeURL)
            XCTAssertEqual(discs.first?.workerSourcePath, "/dev/disk9")
        }
    }

    func testInsertedDiscDetectionOmitsUnresolvedVolumes() throws {
        try withTemporaryDirectory { volumeURL in
            try FileManager.default.createDirectory(
                at: volumeURL.appendingPathComponent("BDMV", isDirectory: true),
                withIntermediateDirectories: true
            )

            let discs = DiscSourceDetector.insertedDiscs(
                in: [volumeURL],
                devicePathResolver: { _ in nil }
            )

            XCTAssertTrue(discs.isEmpty)
        }
    }

    func testCurrentCapabilitiesStayHonest() {
        XCTAssertTrue(AppCapabilities.current.conversionAvailable)
        XCTAssertEqual(
            AppCapabilities.current.conversionUnavailableReason,
            "Conversion requires a Blu-ray disc, Blu-ray folder, ISO, MKV, MTS, or M2TS source."
        )
    }

    func testFolderISOAndExistingFileKindsSupportConversion() {
        XCTAssertTrue(ConversionSourceKind.discImage.supportsMetadataInspection)
        XCTAssertTrue(ConversionSourceKind.discImage.supportsConversion)
        XCTAssertEqual(ConversionSourceKind.discImage.allowedExtensions, ["iso"])
        XCTAssertTrue(ConversionSourceKind.bluRayFolder.supportsMetadataInspection)
        XCTAssertTrue(ConversionSourceKind.bluRayFolder.supportsConversion)
        XCTAssertTrue(ConversionSourceKind.matroska.supportsConversion)
        XCTAssertTrue(ConversionSourceKind.transportStream.supportsConversion)
    }

    func testPhysicalDiscSupportsConversionWhileBatchFolderDoesNot() {
        XCTAssertTrue(ConversionSourceKind.physicalDisc.supportsMetadataInspection)
        XCTAssertTrue(ConversionSourceKind.physicalDisc.supportsConversion)
        XCTAssertFalse(ConversionSourceKind.sourceFolder.supportsConversion)
    }

    func testConversionJobSpecUsesConvertSourceOperation() {
        let draft = makeDraft(kind: .matroska, extension: "mkv")
        let spec = WorkerJobSpec(draft: draft)
        XCTAssertEqual(spec.operation, "convert_source")
        XCTAssertNotNil(spec.destination)
        XCTAssertNotNil(spec.encoding)
        XCTAssertNotNil(spec.job)
    }

    func testPreviewJobSpecIsIndependentFromFullConversion() throws {
        let conversion = makeDraft(kind: .transportStream, extension: "m2ts")
        let preview = try XCTUnwrap(
            PreviewDraft(
                parentJobID: UUID(uuidString: "11111111-1111-4111-8111-111111111111")!,
                conversion: conversion,
                outputLength: .threeMinutes,
                samplePosition: .middle
            )
        )
        let previewSpec = WorkerJobSpec(
            previewDraft: preview,
            destinationURL: URL(fileURLWithPath: "/tmp/preview", isDirectory: true)
        )
        let conversionSpec = WorkerJobSpec(draft: conversion)

        XCTAssertEqual(previewSpec.operation, "preview_source")
        XCTAssertEqual(previewSpec.preview?.durationSeconds, 180)
        XCTAssertEqual(previewSpec.preview?.position, "middle")
        XCTAssertEqual(conversionSpec.operation, "convert_source")
        XCTAssertNil(conversionSpec.preview)
    }

    func testConversionJobSpecEncodesSourceAndDestination() {
        let source = ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/src/movie.mkv"))
        let destination = URL(fileURLWithPath: "/Movies", isDirectory: true)
        let draft = ConversionDraft(
            source: source,
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: destination,
            options: ConversionOptions()
        )
        let spec = WorkerJobSpec(draft: draft)
        XCTAssertEqual(spec.source.kind, .directFile)
        XCTAssertEqual(spec.source.path, "/src/movie.mkv")
        XCTAssertEqual(spec.destination?.path, "/Movies")
    }

    func testBluRayFolderJobSpecUsesExplicitFolderKind() {
        let draft = ConversionDraft(
            source: ConversionSource(kind: .bluRayFolder, url: URL(fileURLWithPath: "/src/Disc")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies", isDirectory: true),
            options: ConversionOptions()
        )

        XCTAssertEqual(WorkerJobSpec(draft: draft).source.kind, .bluRayFolder)
    }

    func testPhysicalDiscJobSpecUsesResolvedDevicePathAndPreservesSourceVolume() {
        let source = ConversionSource(
            kind: .physicalDisc,
            url: URL(fileURLWithPath: "/Volumes/Feature", isDirectory: true),
            workerSourcePath: "/dev/disk9"
        )
        let spec = WorkerJobSpec(source: source)

        XCTAssertEqual(spec.source.kind, .physicalDisc)
        XCTAssertEqual(spec.source.path, "/dev/disk9")
        XCTAssertEqual(source.url.path, "/Volumes/Feature")
    }

    func testPhysicalDiscJobSpecNeverRequestsSourceRemoval() {
        var jobOptions = JobOptions()
        jobOptions.removeOriginalAfterSuccess = true
        let draft = ConversionDraft(
            source: ConversionSource(
                kind: .physicalDisc,
                url: URL(fileURLWithPath: "/Volumes/Feature", isDirectory: true),
                workerSourcePath: "/dev/disk9"
            ),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/output", isDirectory: true),
            options: ConversionOptions(job: jobOptions)
        )

        XCTAssertFalse(WorkerJobSpec(draft: draft).job?.removeOriginal == true)
    }

    func testConversionJobSpecEncodesVideoAndAudioSettings() {
        var encoding = EncodingOptions()
        encoding.hevcQuality = 80
        encoding.upscaleEnabled = true
        encoding.fieldOfView = 120
        encoding.audioHandling = .transcodeAAC
        encoding.audioBitrate = 512

        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/src/m.mkv")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies"),
            options: ConversionOptions(encoding: encoding)
        )
        let spec = WorkerJobSpec(draft: draft)
        XCTAssertEqual(spec.encoding?.mvHEVCQuality, 80)
        XCTAssertTrue(spec.encoding?.fxUpscale == true)
        XCTAssertEqual(spec.encoding?.fieldOfView, 120)
        XCTAssertTrue(spec.encoding?.transcodeAudio == true)
        XCTAssertEqual(spec.encoding?.audioBitrate, 512)
    }

    func testConversionJobSpecEncodesJobSettings() {
        var job = JobOptions()
        job.startStage = .extractMVCAndAudio
        job.keepStageFiles = true
        job.softwareEncoder = true

        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/src/m.mkv")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies"),
            options: ConversionOptions(job: job)
        )
        let spec = WorkerJobSpec(draft: draft)
        XCTAssertEqual(spec.job?.startStage, ConversionStage.extractMVCAndAudio.rawValue)
        XCTAssertTrue(spec.job?.keepFiles == true)
        XCTAssertTrue(spec.job?.softwareEncoder == true)
    }

    func testMakeMKVRecoveryBuildsFreshStageTwoDraft() throws {
        let draft = makeDraft(kind: .discImage, extension: "iso")
        let decision = WorkerDecision(
            identifier: "mkv_creation_decision_required",
            prompt: "MakeMKV reported errors.",
            choices: ["retry_continue_on_error", "cancel"],
            details: nil
        )

        let retry = try XCTUnwrap(
            draft.retrying(decision: decision, choice: .retryContinueOnError)
        )

        XCTAssertEqual(draft.options.job.startStage, .createMKV)
        XCTAssertFalse(draft.options.job.continueOnError)
        XCTAssertEqual(retry.options.job.startStage, .extractMVCAndAudio)
        XCTAssertTrue(retry.options.job.continueOnError)
        XCTAssertEqual(retry.source, draft.source)
        XCTAssertEqual(retry.destinationURL, draft.destinationURL)
    }

    func testSubtitleRecoveryBuildsFreshStageThreeDraftWithoutSubtitles() throws {
        let draft = makeDraft(kind: .discImage, extension: "iso")
        let decision = WorkerDecision(
            identifier: "subtitle_decision_required",
            prompt: "Subtitle extraction needs attention.",
            choices: ["retry_without_subtitles", "cancel"],
            details: nil
        )

        let retry = try XCTUnwrap(
            draft.retrying(decision: decision, choice: .retryWithoutSubtitles)
        )

        XCTAssertTrue(draft.options.encoding.includeSubtitles)
        XCTAssertEqual(retry.options.job.startStage, .extractSubtitles)
        XCTAssertFalse(retry.options.encoding.includeSubtitles)
        XCTAssertFalse(retry.options.job.continueOnError)
    }

    func testRecoveryDraftRejectsChoiceNotOfferedByDecision() {
        let draft = makeDraft(kind: .discImage, extension: "iso")
        let decision = WorkerDecision(
            identifier: "subtitle_decision_required",
            prompt: "Subtitle extraction needs attention.",
            choices: ["cancel"],
            details: nil
        )

        XCTAssertNil(draft.retrying(decision: decision, choice: .retryWithoutSubtitles))
    }

    func testDecisionOnlyExposesRecoveryChoicesForMatchingIdentifier() {
        let decision = WorkerDecision(
            identifier: "subtitle_decision_required",
            prompt: "Subtitle extraction needs attention.",
            choices: ["retry_continue_on_error", "retry_without_subtitles", "cancel", "future_choice"],
            details: nil
        )

        XCTAssertEqual(decision.supportedChoices, [.retryWithoutSubtitles, .cancel])
    }

    func testInspectionJobSpecOmitsConversionSettings() throws {
        let spec = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/src/m.mkv"))
        XCTAssertEqual(spec.operation, "inspect_source")
        XCTAssertNil(spec.destination)
        XCTAssertNil(spec.encoding)
        XCTAssertNil(spec.job)
        let data = try JSONEncoder().encode(spec)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertNil(json?["destination"], "inspection spec must not include destination")
        XCTAssertNil(json?["encoding"], "inspection spec must not include encoding")
        XCTAssertNil(json?["job"], "inspection spec must not include job options")
    }

    func testInspectionJobSpecClassifiesDirectoryURLAsBluRayFolder() {
        let sourceURL = URL(fileURLWithPath: "/src/Disc", isDirectory: true)

        XCTAssertEqual(WorkerJobSpec(sourceURL: sourceURL).source.kind, .bluRayFolder)
    }

    func testConversionJobSpecWireFormatMatchesWorkerContract() throws {
        let spec = WorkerJobSpec(draft: makeDraft(kind: .matroska, extension: "mkv"))
        let data = try JSONEncoder().encode(spec)
        let json = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        let encoding = try XCTUnwrap(json["encoding"] as? [String: Any])
        let job = try XCTUnwrap(json["job"] as? [String: Any])
        let source = try XCTUnwrap(json["source"] as? [String: Any])

        XCTAssertEqual(json["operation"] as? String, "convert_source")
        XCTAssertEqual(json["protocol_version"] as? Int, 3)
        XCTAssertEqual(source["kind"] as? String, "direct_file")
        XCTAssertEqual((json["destination"] as? [String: Any])?["path"] as? String, "/Movies")
        XCTAssertEqual(encoding["mv_hevc_quality"] as? Int, 75)
        XCTAssertEqual(encoding["language_code"] as? String, "eng")
        XCTAssertEqual(job["start_stage"] as? Int, 1)
        XCTAssertNil(job["output_length"])
        XCTAssertNil(json["conversion_settings"])
    }

    func testConversionJobSpecMatchesSharedPythonFixture() throws {
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/tmp/movie.mkv")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/output", isDirectory: true),
            options: ConversionOptions()
        )
        let spec = WorkerJobSpec(
            draft: draft,
            jobID: try XCTUnwrap(UUID(uuidString: "11111111-1111-4111-8111-111111111111"))
        )
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(spec)) as? NSDictionary
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/native_worker_convert_v3.json")
        let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL)) as? NSDictionary

        XCTAssertEqual(encoded, fixture)
    }

    func testPreviewJobSpecMatchesSharedPythonFixture() throws {
        let conversion = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/tmp/movie.mkv")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/output", isDirectory: true),
            options: ConversionOptions()
        )
        let preview = try XCTUnwrap(
            PreviewDraft(
                parentJobID: UUID(uuidString: "11111111-1111-4111-8111-111111111111")!,
                conversion: conversion,
                outputLength: .oneMinute,
                samplePosition: .middle
            )
        )
        let spec = WorkerJobSpec(
            previewDraft: preview,
            destinationURL: URL(
                fileURLWithPath: "/tmp/previews/22222222-2222-4222-8222-222222222222",
                isDirectory: true
            ),
            jobID: UUID(uuidString: "22222222-2222-4222-8222-222222222222")!
        )
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(spec)) as? NSDictionary
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/native_worker_preview_v3.json")
        let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL)) as? NSDictionary

        XCTAssertEqual(encoded, fixture)
    }

    func testPhysicalDiscJobSpecMatchesSharedPythonFixture() throws {
        let draft = ConversionDraft(
            source: ConversionSource(
                kind: .physicalDisc,
                url: URL(fileURLWithPath: "/Volumes/Feature", isDirectory: true),
                workerSourcePath: "/dev/disk9"
            ),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/output", isDirectory: true),
            options: ConversionOptions()
        )
        let spec = WorkerJobSpec(
            draft: draft,
            jobID: try XCTUnwrap(UUID(uuidString: "11111111-1111-4111-8111-111111111111"))
        )
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(spec)) as? NSDictionary
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/native_worker_convert_physical_disc_v3.json")
        let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL)) as? NSDictionary

        XCTAssertEqual(encoded, fixture)
    }

    private func makeDraft(
        kind: ConversionSourceKind,
        extension ext: String
    ) -> ConversionDraft {
        ConversionDraft(
            source: ConversionSource(kind: kind, url: URL(fileURLWithPath: "/src/movie.\(ext)")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies", isDirectory: true),
            options: ConversionOptions()
        )
    }

    private func withTemporaryDirectory(_ operation: (URL) throws -> Void) throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try operation(directoryURL)
    }
}

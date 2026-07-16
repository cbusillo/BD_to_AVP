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

    func testDraftPreservesDotsInExtensionlessInspectedName() {
        let draft = ConversionDraft(
            source: ConversionSource(
                kind: .matroska,
                url: URL(fileURLWithPath: "/Sources/Movie.Part1.mkv")
            ),
            sourceDetails: SourceInspection(
                name: "Movie.Part1",
                resolution: "1920x1080",
                frameRate: "24/1",
                interlaced: false,
                sizeBytes: 10
            ),
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies", isDirectory: true),
            options: ConversionOptions()
        )

        XCTAssertEqual(draft.proposedOutputURL.path, "/Movies/Movie.Part1_AVP.mov")
    }

    func testDraftUsesSelectedTitleOutputName() {
        let title = SourceTitle(
            id: "makemkv:2",
            name: "3D Video 1",
            outputName: "Feature - 3D Video 1",
            durationSeconds: 600,
            resolution: "1920x1080",
            frameRate: "24000/1001",
            mainFeature: false
        )
        let draft = ConversionDraft(
            source: ConversionSource(kind: .discImage, url: URL(fileURLWithPath: "/Sources/Feature.iso")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies", isDirectory: true),
            options: ConversionOptions(),
            selectedTitle: title
        )

        XCTAssertEqual(draft.proposedOutputURL.path, "/Movies/Feature - 3D Video 1_AVP.mov")
    }

    func testWithSourceDetailsPreservesSelectedTitle() {
        let title = SourceTitle(
            id: "makemkv:2",
            name: "3D Video 1",
            outputName: "Feature - 3D Video 1",
            durationSeconds: 600,
            resolution: "1920x1080",
            frameRate: "24000/1001",
            mainFeature: false
        )
        let draft = ConversionDraft(
            source: ConversionSource(kind: .discImage, url: URL(fileURLWithPath: "/Sources/Feature.iso")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies", isDirectory: true),
            options: ConversionOptions(),
            selectedTitle: title
        )

        let inspectedDraft = draft.withSourceDetails(
            SourceInspection(
                name: "Feature",
                resolution: "1920x1080",
                frameRate: "24000/1001",
                interlaced: false
            )
        )

        XCTAssertEqual(inspectedDraft.selectedTitle, title)
        XCTAssertEqual(inspectedDraft.proposedOutputURL.path, "/Movies/Feature - 3D Video 1_AVP.mov")
    }

    func testSourceInspectionDecodesDetectedTitles() throws {
        let data = Data(
            """
            {
              "name": "Feature",
              "resolution": "1920x1080",
              "frame_rate": "24000/1001",
              "interlaced": false,
              "titles": [
                {
                  "id": "makemkv:0",
                  "name": "Main Movie",
                  "output_name": "Feature",
                  "duration_seconds": 7200,
                  "resolution": "1920x1080",
                  "frame_rate": "24000/1001",
                  "main_feature": true
                }
              ]
            }
            """.utf8
        )

        let inspection = try JSONDecoder().decode(SourceInspection.self, from: data)

        XCTAssertEqual(inspection.titles.count, 1)
        XCTAssertEqual(inspection.mainTitle?.id, "makemkv:0")
        XCTAssertEqual(DiscTitleSelection.main.resolvedTitles(in: inspection), inspection.titles)
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
        XCTAssertEqual(SubtitleLanguage.french.code, "fra")
        XCTAssertEqual(SubtitleLanguage.german.code, "deu")
        XCTAssertEqual(SubtitleLanguage.chinese.code, "zho")
    }

    func testAudioHandlingPreservesProfileValuesAndPCMDefaults() {
        XCTAssertEqual(AudioHandling.allCases, [.automatic, .convertAAC, .pcm])
        XCTAssertEqual(AudioHandling.automatic.rawValue, "automatic")
        XCTAssertEqual(AudioHandling.convertAAC.rawValue, "transcodeAAC")
        XCTAssertEqual(AudioHandling.pcm.rawValue, "preserve")
        XCTAssertEqual(AudioHandling.pcm.title, "Uncompressed PCM")
        XCTAssertEqual(EncodingOptions().audioHandling, .pcm)
        XCTAssertTrue(BuiltInProfile.allCases.allSatisfy { $0.options.audioHandling == .pcm })
        XCTAssertEqual(ConversionStage.transcodeAudio.title, "7 — Prepare Audio")
    }

    func testAudioHandlingHelpAndSummariesDescribeAllModesPrecisely() {
        XCTAssertEqual(
            AudioHandling.automatic.detail,
            "Copies the selected audio set only when every track is qualified AAC; otherwise converts the entire set to AAC."
        )
        XCTAssertEqual(AudioHandling.convertAAC.detail, "Converts the entire selected audio set to AAC.")
        XCTAssertEqual(AudioHandling.pcm.detail, "Decodes the selected audio set to uncompressed PCM.")
        XCTAssertEqual(AudioHandling.automatic.bitrateLabel, "AAC fallback bitrate")
        XCTAssertEqual(AudioHandling.convertAAC.bitrateLabel, "AAC bitrate")
        XCTAssertNil(AudioHandling.pcm.bitrateLabel)
        XCTAssertTrue(EncodingOptions().compactSummary.contains("uncompressed PCM audio"))
        XCTAssertEqual(
            EncodingOptions(audioHandling: .automatic, audioBitrate: 448).compactSummary,
            "HEVC 75 · 20 Mbps eyes · source resolution · automatic audio (AAC fallback 448 kbps)"
        )
        XCTAssertEqual(
            EncodingOptions(audioHandling: .convertAAC, audioBitrate: 448).compactSummary,
            "HEVC 75 · 20 Mbps eyes · source resolution · AAC 448 kbps"
        )
        XCTAssertTrue(BuiltInProfile.balanced.summary.contains("uncompressed PCM audio"))
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

    func testSourceFolderDiscoveryIsRecursiveDeterministicAndSkipsUnsupportedTrees() throws {
        try withTemporaryDirectory { directoryURL in
            let nestedURL = directoryURL.appendingPathComponent("Nested", isDirectory: true)
            let hiddenURL = directoryURL.appendingPathComponent(".Hidden", isDirectory: true)
            let packageURL = directoryURL.appendingPathComponent("Archive.app", isDirectory: true)
            let bdmvStreamURL = directoryURL
                .appendingPathComponent("Disc", isDirectory: true)
                .appendingPathComponent("BDMV", isDirectory: true)
                .appendingPathComponent("STREAM", isDirectory: true)
            try FileManager.default.createDirectory(at: nestedURL, withIntermediateDirectories: true)
            try FileManager.default.createDirectory(at: hiddenURL, withIntermediateDirectories: true)
            try FileManager.default.createDirectory(at: packageURL, withIntermediateDirectories: true)
            try FileManager.default.createDirectory(at: bdmvStreamURL, withIntermediateDirectories: true)

            let expectedURLs = [
                directoryURL.appendingPathComponent("B.mkv"),
                nestedURL.appendingPathComponent("a.ISO"),
                nestedURL.appendingPathComponent("clip.M2TS"),
            ]
            for url in expectedURLs {
                _ = FileManager.default.createFile(atPath: url.path, contents: Data())
            }
            _ = FileManager.default.createFile(
                atPath: directoryURL.appendingPathComponent("unsupported.mp4").path,
                contents: Data()
            )
            _ = FileManager.default.createFile(
                atPath: hiddenURL.appendingPathComponent("hidden.mkv").path,
                contents: Data()
            )
            _ = FileManager.default.createFile(
                atPath: packageURL.appendingPathComponent("packaged.mkv").path,
                contents: Data()
            )
            _ = FileManager.default.createFile(
                atPath: bdmvStreamURL.appendingPathComponent("00001.m2ts").path,
                contents: Data()
            )
            try FileManager.default.createSymbolicLink(
                at: directoryURL.appendingPathComponent("Linked Folder", isDirectory: true),
                withDestinationURL: hiddenURL
            )
            try FileManager.default.createSymbolicLink(
                at: directoryURL.appendingPathComponent("Linked File.mkv"),
                withDestinationURL: hiddenURL.appendingPathComponent("hidden.mkv")
            )

            let folderSource = try XCTUnwrap(ConversionSource.infer(from: directoryURL))
            let firstDiscovery = SourceFolderDiscovery.discoverSources(in: directoryURL)
            let secondDiscovery = SourceFolderDiscovery.discoverSources(in: directoryURL)

            XCTAssertEqual(folderSource.kind, .sourceFolder)
            XCTAssertEqual(firstDiscovery.map(\.url), expectedURLs.sorted {
                let firstKey = $0.standardizedFileURL.path.lowercased()
                let secondKey = $1.standardizedFileURL.path.lowercased()
                return firstKey == secondKey ? $0.path < $1.path : firstKey < secondKey
            })
            XCTAssertEqual(firstDiscovery, secondDiscovery)
            XCTAssertEqual(firstDiscovery.map(\.kind), [.matroska, .discImage, .transportStream])
        }
    }

    func testSourceFolderDiscoveryExplainsEmptyFolderThroughEmptyQueue() throws {
        try withTemporaryDirectory { directoryURL in
            let folderSource = try XCTUnwrap(ConversionSource.infer(from: directoryURL))
            let queue = SourceFolderQueueState(
                folderSource: folderSource,
                sources: SourceFolderDiscovery.discoverSources(in: directoryURL)
            )

            XCTAssertTrue(queue.items.isEmpty)
            XCTAssertEqual(queue.summaryText, "No supported sources")
        }
    }

    func testBatchPreparationSnapshotsProfileDestinationAndOptionsPerItem() throws {
        try withTemporaryDirectory { directoryURL in
            let firstURL = directoryURL.appendingPathComponent("First.mkv")
            let secondURL = directoryURL.appendingPathComponent("Second.m2ts")
            _ = FileManager.default.createFile(atPath: firstURL.path, contents: Data())
            _ = FileManager.default.createFile(atPath: secondURL.path, contents: Data())
            let folderSource = try XCTUnwrap(ConversionSource.infer(from: directoryURL))
            var queue = SourceFolderQueueState(
                folderSource: folderSource,
                sources: SourceFolderDiscovery.discoverSources(in: directoryURL)
            )
            var profile = BuiltInProfile.balanced.profile
            var options = ConversionOptions()
            options.encoding.hevcQuality = 81
            options.job.keepStageFiles = true
            let destinationURL = directoryURL.appendingPathComponent("Output", isDirectory: true)

            queue.prepareForRun(
                profile: profile,
                destinationURL: destinationURL,
                options: options
            )
            profile.name = "Changed Later"
            options.encoding.hevcQuality = 12
            options.job.keepStageFiles = false

            XCTAssertEqual(queue.items.count, 2)
            for item in queue.items {
                let draft = try XCTUnwrap(item.draft)
                XCTAssertEqual(draft.profile.name, "Balanced")
                XCTAssertEqual(draft.destinationURL, destinationURL)
                XCTAssertEqual(draft.options.encoding.hevcQuality, 81)
                XCTAssertTrue(draft.options.job.keepStageFiles)
            }
            XCTAssertEqual(profile.name, "Changed Later")
            XCTAssertEqual(options.encoding.hevcQuality, 12)
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

    func testCustomProfileResolvesConcreteValuesWithoutOwningJobContext() throws {
        let encoding = EncodingOptions(
            hevcQuality: 92,
            leftRightBitrate: 44,
            upscaleEnabled: true,
            upscaleQuality: 88,
            linkQuality: false,
            fieldOfView: 105,
            frameRateOverride: "24000/1001",
            resolutionOverride: "3840x2160",
            cropBlackBars: true,
            swapEyes: true,
            audioHandling: .convertAAC,
            audioBitrate: 512,
            subtitles: SubtitlePolicy(mode: .off, preferredLanguage: .japanese)
        )
        let profile = EncodingProfile(
            id: "custom.11111111-1111-4111-8111-111111111111",
            name: "Cinema",
            options: encoding,
            kind: .custom,
            systemImage: "slider.horizontal.3"
        )
        var job = JobOptions()
        job.keepStageFiles = true
        job.overwriteExisting = true
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/Sources/Feature.mkv")),
            sourceDetails: nil,
            profile: profile,
            destinationURL: URL(fileURLWithPath: "/Movies", isDirectory: true),
            options: ConversionOptions(encoding: encoding, job: job)
        )

        let spec = WorkerJobSpec(draft: draft)
        let json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(spec)) as? [String: Any]

        XCTAssertEqual(spec.source.path, "/Sources/Feature.mkv")
        XCTAssertEqual(spec.destination?.path, "/Movies")
        XCTAssertEqual(spec.encoding?.mvHEVCQuality, 92)
        XCTAssertEqual(spec.encoding?.audio.bitrate, 512)
        XCTAssertTrue(spec.job?.keepFiles == true)
        XCTAssertTrue(spec.job?.overwrite == true)
        XCTAssertNil(json?["profile"])
        XCTAssertNil(json?["profile_id"])
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
        encoding.audioHandling = .convertAAC
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
        XCTAssertEqual(spec.encoding?.audio, WorkerJobSpec.Encoding.Audio(mode: .convertAAC, bitrate: 512))
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

        XCTAssertEqual(draft.options.encoding.subtitles.mode, .preferredPlusOthers)
        XCTAssertEqual(retry.options.job.startStage, .extractSubtitles)
        XCTAssertEqual(retry.options.encoding.subtitles.mode, .off)
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
        XCTAssertEqual(json["protocol_version"] as? Int, 6)
        XCTAssertEqual(source["kind"] as? String, "direct_file")
        XCTAssertEqual((json["destination"] as? [String: Any])?["path"] as? String, "/Movies")
        XCTAssertEqual(encoding["mv_hevc_quality"] as? Int, 75)
        let audio = try XCTUnwrap(encoding["audio"] as? [String: Any])
        XCTAssertEqual(audio["mode"] as? String, "pcm")
        XCTAssertEqual(audio["bitrate"] as? Int, 384)
        XCTAssertEqual(Set(audio.keys), ["mode", "bitrate"])
        XCTAssertNil(encoding["transcode_audio"])
        XCTAssertNil(encoding["audio_bitrate"])
        let subtitles = try XCTUnwrap(encoding["subtitles"] as? [String: Any])
        XCTAssertEqual(subtitles["mode"] as? String, "preferred_plus_others")
        XCTAssertEqual(subtitles["preferred_language"] as? String, "eng")
        XCTAssertEqual(job["start_stage"] as? Int, 1)
        XCTAssertNil(job["output_length"])
        XCTAssertNil(json["conversion_settings"])
    }

    func testConversionJobSpecUsesCanonicalDutchAndNullsLanguageWhenOff() throws {
        var options = ConversionOptions()
        options.encoding.subtitles = SubtitlePolicy(mode: .preferredOnly, preferredLanguage: .dutch)
        let source = ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/tmp/movie.mkv"))
        let draft = ConversionDraft(
            source: source,
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/output", isDirectory: true),
            options: options
        )

        var data = try JSONEncoder().encode(WorkerJobSpec(draft: draft))
        var json = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        var encoding = try XCTUnwrap(json["encoding"] as? [String: Any])
        var subtitles = try XCTUnwrap(encoding["subtitles"] as? [String: Any])
        XCTAssertEqual(subtitles["mode"] as? String, "preferred_only")
        XCTAssertEqual(subtitles["preferred_language"] as? String, "nld")
        XCTAssertEqual(Set(subtitles.keys), ["mode", "preferred_language"])
        XCTAssertNil(encoding["skip_subtitles"])
        XCTAssertNil(encoding["language_code"])
        XCTAssertNil(encoding["remove_extra_languages"])

        options.encoding.subtitles.mode = .off
        let offDraft = ConversionDraft(
            source: source,
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/output", isDirectory: true),
            options: options
        )
        data = try JSONEncoder().encode(WorkerJobSpec(draft: offDraft))
        json = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        encoding = try XCTUnwrap(json["encoding"] as? [String: Any])
        subtitles = try XCTUnwrap(encoding["subtitles"] as? [String: Any])
        XCTAssertEqual(subtitles["mode"] as? String, "off")
        XCTAssertTrue(subtitles["preferred_language"] is NSNull)
    }

    func testConversionJobSpecEncodesEveryAudioModeInNestedV6AudioObject() throws {
        let expectedModes: [(AudioHandling, String)] = [
            (.automatic, "automatic"),
            (.convertAAC, "convert_aac"),
            (.pcm, "pcm"),
        ]

        for (handling, expectedMode) in expectedModes {
            var options = ConversionOptions()
            options.encoding.audioHandling = handling
            options.encoding.audioBitrate = 448
            let draft = ConversionDraft(
                source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/tmp/movie.mkv")),
                sourceDetails: nil,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/tmp/output", isDirectory: true),
                options: options
            )

            let data = try JSONEncoder().encode(WorkerJobSpec(draft: draft))
            let json = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
            let encoding = try XCTUnwrap(json["encoding"] as? [String: Any])
            let audio = try XCTUnwrap(encoding["audio"] as? [String: Any])

            XCTAssertEqual(audio["mode"] as? String, expectedMode)
            XCTAssertEqual(audio["bitrate"] as? Int, 448)
            XCTAssertEqual(Set(audio.keys), ["mode", "bitrate"])
        }
    }

    func testSubtitlePolicyDoesNotChangeAudioEncodingFields() throws {
        var options = ConversionOptions()
        options.encoding.audioHandling = .convertAAC
        options.encoding.audioBitrate = 640
        let source = ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/tmp/movie.mkv"))

        func workerEncoding(for options: ConversionOptions) throws -> WorkerJobSpec.Encoding {
            let draft = ConversionDraft(
                source: source,
                sourceDetails: nil,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/tmp/output", isDirectory: true),
                options: options
            )
            return try XCTUnwrap(WorkerJobSpec(draft: draft).encoding)
        }

        let originalEncoding = try workerEncoding(for: options)
        options.encoding.subtitles = SubtitlePolicy(mode: .off, preferredLanguage: .dutch)
        let changedEncoding = try workerEncoding(for: options)

        XCTAssertEqual(changedEncoding.audio, originalEncoding.audio)
    }

    func testConversionJobSpecMatchesSharedV6WorkerFixture() throws {
        var options = ConversionOptions()
        options.encoding.audioHandling = .automatic
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/tmp/movie.mkv")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/output", isDirectory: true),
            options: options
        )
        let spec = WorkerJobSpec(
            draft: draft,
            jobID: try XCTUnwrap(UUID(uuidString: "11111111-1111-4111-8111-111111111111"))
        )
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(spec)) as? NSDictionary
        let fixture = try JSONSerialization.jsonObject(
            with: sharedFixtureData(named: "native_worker_convert_v6.json")
        ) as? NSDictionary

        XCTAssertEqual(encoded, fixture)
    }

    func testDiscJobSpecEncodesSelectedOpaqueTitleID() throws {
        let draft = ConversionDraft(
            source: ConversionSource(kind: .discImage, url: URL(fileURLWithPath: "/tmp/movie.iso")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/output", isDirectory: true),
            options: ConversionOptions(),
            selectedTitle: SourceTitle(
                id: "provider:playlist-01005",
                name: "Main Movie",
                outputName: "Movie",
                durationSeconds: 7_200,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: true
            )
        )

        let data = try JSONEncoder().encode(WorkerJobSpec(draft: draft))
        let json = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        let source = try XCTUnwrap(json["source"] as? [String: Any])

        XCTAssertEqual(source["title_id"] as? String, "provider:playlist-01005")
    }

    func testPreviewJobSpecMatchesSharedV6WorkerFixture() throws {
        var options = ConversionOptions()
        options.encoding.audioHandling = .automatic
        let conversion = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/tmp/movie.mkv")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/output", isDirectory: true),
            options: options
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
        let fixture = try JSONSerialization.jsonObject(
            with: sharedFixtureData(named: "native_worker_preview_v6.json")
        ) as? NSDictionary

        XCTAssertEqual(encoded, fixture)
    }

    func testPhysicalDiscJobSpecMatchesSharedV6WorkerFixture() throws {
        var options = ConversionOptions()
        options.encoding.audioHandling = .automatic
        let draft = ConversionDraft(
            source: ConversionSource(
                kind: .physicalDisc,
                url: URL(fileURLWithPath: "/Volumes/Feature", isDirectory: true),
                workerSourcePath: "/dev/disk9"
            ),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/output", isDirectory: true),
            options: options,
            selectedTitle: SourceTitle(
                id: "makemkv:0",
                name: "Main Movie",
                outputName: "Feature",
                durationSeconds: 7_200,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: true
            )
        )
        let spec = WorkerJobSpec(
            draft: draft,
            jobID: try XCTUnwrap(UUID(uuidString: "11111111-1111-4111-8111-111111111111"))
        )
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(spec)) as? NSDictionary
        let fixture = try JSONSerialization.jsonObject(
            with: sharedFixtureData(named: "native_worker_convert_physical_disc_v6.json")
        ) as? NSDictionary

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

    private func sharedFixtureData(named name: String) throws -> Data {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/\(name)")
        guard FileManager.default.fileExists(atPath: fixtureURL.path) else {
            throw XCTSkip("Waiting for the backend v6 fixture \(name).")
        }
        return try Data(contentsOf: fixtureURL)
    }

    private func withTemporaryDirectory(_ operation: (URL) throws -> Void) throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try operation(directoryURL)
    }
}

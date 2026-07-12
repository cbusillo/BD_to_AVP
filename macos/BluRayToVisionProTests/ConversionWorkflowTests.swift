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
            outputLength: .fullMovie,
            samplePosition: .beginning,
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
            let mkvURL = directoryURL.appendingPathComponent("Feature.mkv")
            let streamURL = directoryURL.appendingPathComponent("Feature.m2ts")
            _ = FileManager.default.createFile(atPath: imageURL.path, contents: Data())
            _ = FileManager.default.createFile(atPath: mkvURL.path, contents: Data())
            _ = FileManager.default.createFile(atPath: streamURL.path, contents: Data())

            XCTAssertEqual(ConversionSource.infer(from: imageURL)?.kind, .discImage)
            XCTAssertEqual(ConversionSource.infer(from: mkvURL)?.kind, .matroska)
            XCTAssertEqual(ConversionSource.infer(from: streamURL)?.kind, .transportStream)
        }
    }

    func testBluRayFolderDetectionFindsBDMV() throws {
        try withTemporaryDirectory { directoryURL in
            let discURL = directoryURL.appendingPathComponent("Feature Disc", isDirectory: true)
            let bdmvURL = discURL.appendingPathComponent("BDMV", isDirectory: true)
            try FileManager.default.createDirectory(at: bdmvURL, withIntermediateDirectories: true)

            XCTAssertTrue(DiscSourceDetector.isBluRayFolder(discURL))
            XCTAssertEqual(ConversionSource.infer(from: discURL)?.kind, .bluRayFolder)
        }
    }

    func testCurrentCapabilitiesStayHonest() {
        XCTAssertTrue(AppCapabilities.current.conversionAvailable)
        XCTAssertFalse(AppCapabilities.current.automaticUpdateChecksAvailable)
    }

    func testMKVAndTransportStreamKindsSupportConversion() {
        XCTAssertTrue(ConversionSourceKind.matroska.supportsConversion)
        XCTAssertTrue(ConversionSourceKind.transportStream.supportsConversion)
    }

    func testDiscAndFolderKindsDoNotSupportConversion() {
        XCTAssertFalse(ConversionSourceKind.physicalDisc.supportsConversion)
        XCTAssertFalse(ConversionSourceKind.discImage.supportsConversion)
        XCTAssertFalse(ConversionSourceKind.bluRayFolder.supportsConversion)
        XCTAssertFalse(ConversionSourceKind.sourceFolder.supportsConversion)
    }

    func testConversionJobSpecUsesConvertSourceOperation() {
        let draft = makeDraft(kind: .matroska, extension: "mkv")
        let spec = WorkerJobSpec(draft: draft)
        XCTAssertEqual(spec.operation, "convert_source")
        XCTAssertNotNil(spec.conversionSettings)
    }

    func testConversionJobSpecAlwaysSendsFullMovie() {
        let draft = makeDraft(kind: .transportStream, extension: "m2ts", outputLength: .threeMinutes)
        let spec = WorkerJobSpec(draft: draft)
        XCTAssertEqual(spec.conversionSettings?.outputLength, "full_movie")
    }

    func testConversionJobSpecEncodesSourceAndDestination() {
        let source = ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/src/movie.mkv"))
        let destination = URL(fileURLWithPath: "/Movies", isDirectory: true)
        let draft = ConversionDraft(
            source: source,
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: destination,
            outputLength: .fullMovie,
            samplePosition: .beginning,
            options: ConversionOptions()
        )
        let spec = WorkerJobSpec(draft: draft)
        XCTAssertEqual(spec.source.path, "/src/movie.mkv")
        XCTAssertEqual(spec.conversionSettings?.destination.path, "/Movies")
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
            outputLength: .fullMovie,
            samplePosition: .beginning,
            options: ConversionOptions(encoding: encoding)
        )
        let spec = WorkerJobSpec(draft: draft)
        XCTAssertEqual(spec.conversionSettings?.video.hevcQuality, 80)
        XCTAssertTrue(spec.conversionSettings?.video.upscaleEnabled == true)
        XCTAssertEqual(spec.conversionSettings?.video.fieldOfView, 120)
        XCTAssertEqual(spec.conversionSettings?.audio.handling, "transcode_aac")
        XCTAssertEqual(spec.conversionSettings?.audio.bitrate, 512)
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
            outputLength: .fullMovie,
            samplePosition: .beginning,
            options: ConversionOptions(job: job)
        )
        let spec = WorkerJobSpec(draft: draft)
        XCTAssertEqual(spec.conversionSettings?.job.startStage, ConversionStage.extractMVCAndAudio.rawValue)
        XCTAssertTrue(spec.conversionSettings?.job.keepStageFiles == true)
        XCTAssertTrue(spec.conversionSettings?.job.softwareEncoder == true)
    }

    func testInspectionJobSpecOmitsConversionSettings() throws {
        let spec = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/src/m.mkv"))
        XCTAssertEqual(spec.operation, "inspect_source")
        XCTAssertNil(spec.conversionSettings)
        let data = try JSONEncoder().encode(spec)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertNil(json?["conversion_settings"], "inspection spec must not include conversion_settings key")
    }

    private func makeDraft(
        kind: ConversionSourceKind,
        extension ext: String,
        outputLength: OutputLength = .fullMovie
    ) -> ConversionDraft {
        ConversionDraft(
            source: ConversionSource(kind: kind, url: URL(fileURLWithPath: "/src/movie.\(ext)")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies", isDirectory: true),
            outputLength: outputLength,
            samplePosition: .beginning,
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

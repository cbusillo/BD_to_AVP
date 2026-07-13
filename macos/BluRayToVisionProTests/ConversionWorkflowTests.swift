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
        XCTAssertNotNil(spec.destination)
        XCTAssertNotNil(spec.encoding)
        XCTAssertNotNil(spec.job)
    }

    func testConversionJobSpecAlwaysSendsFullMovie() {
        let draft = makeDraft(kind: .transportStream, extension: "m2ts", outputLength: .threeMinutes)
        let spec = WorkerJobSpec(draft: draft)
        XCTAssertEqual(spec.job?.outputLength, "full_movie")
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
        XCTAssertEqual(spec.destination?.path, "/Movies")
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
            outputLength: .fullMovie,
            samplePosition: .beginning,
            options: ConversionOptions(job: job)
        )
        let spec = WorkerJobSpec(draft: draft)
        XCTAssertEqual(spec.job?.startStage, ConversionStage.extractMVCAndAudio.rawValue)
        XCTAssertTrue(spec.job?.keepFiles == true)
        XCTAssertTrue(spec.job?.softwareEncoder == true)
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

    func testConversionJobSpecWireFormatMatchesWorkerContract() throws {
        let spec = WorkerJobSpec(draft: makeDraft(kind: .matroska, extension: "mkv"))
        let data = try JSONEncoder().encode(spec)
        let json = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        let encoding = try XCTUnwrap(json["encoding"] as? [String: Any])
        let job = try XCTUnwrap(json["job"] as? [String: Any])

        XCTAssertEqual(json["operation"] as? String, "convert_source")
        XCTAssertEqual((json["destination"] as? [String: Any])?["path"] as? String, "/Movies")
        XCTAssertEqual(encoding["mv_hevc_quality"] as? Int, 75)
        XCTAssertEqual(encoding["language_code"] as? String, "eng")
        XCTAssertEqual(job["start_stage"] as? Int, 1)
        XCTAssertEqual(job["output_length"] as? String, "full_movie")
        XCTAssertNil(json["conversion_settings"])
    }

    func testConversionJobSpecMatchesSharedPythonFixture() throws {
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/tmp/movie.mkv")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/output", isDirectory: true),
            outputLength: .fullMovie,
            samplePosition: .beginning,
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
            .appendingPathComponent("tests/fixtures/native_worker_convert_v1.json")
        let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL)) as? NSDictionary

        XCTAssertEqual(encoded, fixture)
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

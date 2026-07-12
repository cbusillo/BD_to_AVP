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
        XCTAssertFalse(AppCapabilities.current.conversionAvailable)
        XCTAssertFalse(AppCapabilities.current.automaticUpdateChecksAvailable)
    }

    private func withTemporaryDirectory(_ operation: (URL) throws -> Void) throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try operation(directoryURL)
    }
}

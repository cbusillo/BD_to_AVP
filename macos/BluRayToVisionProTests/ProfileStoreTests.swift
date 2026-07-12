import Foundation
import XCTest
@testable import BluRayToVisionPro

final class ProfileStoreTests: XCTestCase {
    @MainActor
    func testLegacyBuiltInIdentifiersMigrate() {
        let store = ProfileStore(fileURL: temporaryProfileURL())

        XCTAssertEqual(store.normalizedProfileID("balanced"), BuiltInProfile.balanced.id)
        XCTAssertEqual(
            store.normalizedProfileID("originalResolution"),
            BuiltInProfile.originalResolution.id
        )
        XCTAssertEqual(store.normalizedProfileID("fourKUpscale"), BuiltInProfile.fourKUpscale.id)
        XCTAssertEqual(store.normalizedProfileID("removed-profile"), BuiltInProfile.balanced.id)
    }

    @MainActor
    func testCustomProfilePersistsEveryEncodingSetting() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let identifier = UUID(uuidString: "A4CC523E-72FA-4F36-A38D-1FB0D6A84742")!
        let options = EncodingOptions(
            hevcQuality: 91,
            leftRightBitrate: 35,
            upscaleEnabled: true,
            upscaleQuality: 87,
            linkQuality: false,
            fieldOfView: 100,
            frameRateOverride: "24000/1001",
            resolutionOverride: "3840x2160",
            cropBlackBars: true,
            swapEyes: true,
            audioHandling: .transcodeAAC,
            audioBitrate: 512,
            language: .japanese,
            includeSubtitles: false,
            keepExtraLanguages: false
        )
        let store = ProfileStore(fileURL: fileURL, idGenerator: { identifier })

        let profileID = try store.createProfile(name: "Cinema", options: options)
        let restoredStore = ProfileStore(fileURL: fileURL)

        XCTAssertEqual(profileID, "custom.\(identifier.uuidString.lowercased())")
        XCTAssertEqual(restoredStore.profile(withID: profileID).name, "Cinema")
        XCTAssertEqual(restoredStore.profile(withID: profileID).options, options)
    }

    @MainActor
    func testDuplicateUpdateAndDeleteLifecycle() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        var identifiers = [
            UUID(uuidString: "6C02DFB0-2B6A-4F6D-9335-3703487FB9D7")!,
            UUID(uuidString: "9B58E388-CB38-46ED-ADE4-F690F6A40D81")!,
        ].makeIterator()
        let store = ProfileStore(fileURL: fileURL, idGenerator: { identifiers.next()! })

        let firstID = try store.duplicateProfile(BuiltInProfile.balanced.id)
        var updatedOptions = store.profile(withID: firstID).options
        updatedOptions.hevcQuality = 82
        try store.updateProfile(firstID, name: "Living Room", options: updatedOptions)
        let secondID = try store.duplicateProfile(firstID)

        XCTAssertEqual(store.profile(withID: firstID).name, "Living Room")
        XCTAssertEqual(store.profile(withID: firstID).options.hevcQuality, 82)
        XCTAssertEqual(store.profile(withID: secondID).name, "Living Room Copy")

        XCTAssertThrowsError(
            try store.updateProfile(secondID, name: "living room", options: updatedOptions)
        ) { error in
            XCTAssertEqual(error as? ProfileStoreError, .duplicateName("living room"))
        }

        try store.deleteProfile(firstID)

        XCTAssertFalse(store.customProfiles.contains { $0.id == firstID })
        XCTAssertEqual(store.profile(withID: firstID).id, BuiltInProfile.balanced.id)
    }

    @MainActor
    func testBuiltInProfilesAreReadOnly() {
        let store = ProfileStore(fileURL: temporaryProfileURL())

        XCTAssertThrowsError(
            try store.updateProfile(
                BuiltInProfile.balanced.id,
                name: "Balanced",
                options: BuiltInProfile.balanced.options
            )
        ) { error in
            XCTAssertEqual(error as? ProfileStoreError, .builtInProfileIsReadOnly)
        }
    }

    @MainActor
    func testUnreadableLibraryIsPreservedBeforeCreatingFreshProfiles() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let firstRecoveryURL = fileURL.appendingPathExtension("corrupt")
        let secondRecoveryURL = fileURL.appendingPathExtension("corrupt-2")
        let unreadableData = Data("not-json".utf8)
        let existingRecoveryData = Data("older-recovery".utf8)
        try unreadableData.write(to: fileURL)
        try existingRecoveryData.write(to: firstRecoveryURL)

        let store = ProfileStore(fileURL: fileURL)

        XCTAssertNotNil(store.loadErrorMessage)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fileURL.path))
        XCTAssertEqual(try Data(contentsOf: firstRecoveryURL), existingRecoveryData)
        XCTAssertEqual(try Data(contentsOf: secondRecoveryURL), unreadableData)

        _ = try store.createProfile(name: "Recovered", options: EncodingOptions())

        XCTAssertTrue(FileManager.default.fileExists(atPath: fileURL.path))
        XCTAssertNil(store.loadErrorMessage)
    }

    @MainActor
    func testUnsupportedLibraryVersionIsPreserved() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let document = Data(#"{"version":99,"profiles":[]}"#.utf8)
        try document.write(to: fileURL)

        let store = ProfileStore(fileURL: fileURL)

        XCTAssertTrue(store.customProfiles.isEmpty)
        XCTAssertNotNil(store.loadErrorMessage)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fileURL.path))
        XCTAssertEqual(try Data(contentsOf: fileURL.appendingPathExtension("corrupt")), document)
    }

    private func temporaryDirectoryURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("ProfileStoreTests.\(UUID().uuidString)", isDirectory: true)
    }

    private func temporaryProfileURL() -> URL {
        temporaryDirectoryURL().appendingPathComponent("profiles.json")
    }
}

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
            videoOutputMode: .av1Stereo,
            av1CRF: 24,
            hevcQuality: 91,
            leftRightBitrate: 35,
            upscaleEnabled: false,
            upscaleQuality: 87,
            linkQuality: false,
            fieldOfView: 100,
            frameRateOverride: "24000/1001",
            resolutionOverride: "3840x2160",
            cropBlackBars: true,
            swapEyes: true,
            audioHandling: .convertAAC,
            audioBitrate: 512,
            subtitles: SubtitlePolicy(mode: .off, preferredLanguage: .japanese)
        )
        let store = ProfileStore(fileURL: fileURL, idGenerator: { identifier })

        let profileID = try store.createProfile(name: "Cinema", options: options)
        let restoredStore = ProfileStore(fileURL: fileURL)

        XCTAssertEqual(profileID, "custom.\(identifier.uuidString.lowercased())")
        XCTAssertEqual(restoredStore.profile(withID: profileID).name, "Cinema")
        XCTAssertEqual(restoredStore.profile(withID: profileID).options, options)
    }

    @MainActor
    func testVersionTwoProfilesMigrateAllAudioHandlingRawValuesToVersionThree() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let document: [String: Any] = [
            "version": 2,
            "profiles": [
                [
                    "id": "A4CC523E-72FA-4F36-A38D-1FB0D6A84742",
                    "name": "PCM",
                    "options": try optionsJSON(audioHandlingRawValue: "preserve"),
                ],
                [
                    "id": "6C02DFB0-2B6A-4F6D-9335-3703487FB9D7",
                    "name": "AAC",
                    "options": try optionsJSON(audioHandlingRawValue: "transcodeAAC"),
                ],
                [
                    "id": "9B58E388-CB38-46ED-ADE4-F690F6A40D81",
                    "name": "Automatic",
                    "options": try optionsJSON(audioHandlingRawValue: "automatic"),
                ],
            ],
        ]
        try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys]).write(to: fileURL)

        let store = ProfileStore(fileURL: fileURL)

        XCTAssertNil(store.loadErrorMessage)
        XCTAssertEqual(store.customProfiles.map(\.options.audioHandling), [.pcm, .convertAAC, .automatic])
        XCTAssertTrue(store.customProfiles.allSatisfy { $0.options.videoOutputMode == .mvHEVC })
        XCTAssertTrue(store.customProfiles.allSatisfy { $0.options.av1CRF == 32 })
        for profile in store.customProfiles {
            try store.updateProfile(profile.id, name: profile.name, options: profile.options)
        }

        let persistedDocument = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: Data(contentsOf: fileURL)) as? [String: Any]
        )
        XCTAssertEqual(persistedDocument["version"] as? Int, 3)
        let persistedProfiles = try XCTUnwrap(persistedDocument["profiles"] as? [[String: Any]])
        let persistedRawValues = try persistedProfiles.map { profile in
            let options = try XCTUnwrap(profile["options"] as? [String: Any])
            return try XCTUnwrap(options["audioHandling"] as? String)
        }
        XCTAssertEqual(persistedRawValues, ["preserve", "transcodeAAC", "automatic"])
    }

    @MainActor
    func testVersionOneProfilesMigrateAtomicallyToCanonicalVersionThreeData() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let legacyData = try legacyDocument(
            profiles: [
                legacyProfile(
                    id: "A4CC523E-72FA-4F36-A38D-1FB0D6A84742",
                    name: "Dutch Off",
                    language: "dut",
                    includeSubtitles: false,
                    keepExtraLanguages: false
                ),
                legacyProfile(
                    id: "6C02DFB0-2B6A-4F6D-9335-3703487FB9D7",
                    name: "French Only",
                    language: "fre",
                    includeSubtitles: true,
                    keepExtraLanguages: false
                ),
                legacyProfile(
                    id: "9B58E388-CB38-46ED-ADE4-F690F6A40D81",
                    name: "German Plus",
                    language: "ger",
                    includeSubtitles: true,
                    keepExtraLanguages: true
                ),
                legacyProfile(
                    id: "E27A632D-C9F0-424D-85B0-77E153AE1DA8",
                    name: "Chinese Plus",
                    language: "chi",
                    includeSubtitles: true,
                    keepExtraLanguages: true
                ),
            ]
        )
        try legacyData.write(to: fileURL)

        let store = ProfileStore(fileURL: fileURL)

        XCTAssertNil(store.loadErrorMessage)
        XCTAssertEqual(store.customProfiles.map(\.options.subtitles.preferredLanguage.code), ["nld", "fra", "deu", "zho"])
        XCTAssertEqual(
            store.customProfiles.map(\.options.subtitles.mode),
            [.off, .preferredOnly, .preferredPlusOthers, .preferredPlusOthers]
        )
        let migratedOptions = try XCTUnwrap(store.customProfiles.first?.options)
        XCTAssertEqual(migratedOptions.videoOutputMode, .mvHEVC)
        XCTAssertEqual(migratedOptions.av1CRF, 32)
        XCTAssertEqual(migratedOptions.hevcQuality, 91)
        XCTAssertEqual(migratedOptions.leftRightBitrate, 35)
        XCTAssertTrue(migratedOptions.upscaleEnabled)
        XCTAssertEqual(migratedOptions.upscaleQuality, 87)
        XCTAssertFalse(migratedOptions.linkQuality)
        XCTAssertEqual(migratedOptions.fieldOfView, 100)
        XCTAssertEqual(migratedOptions.frameRateOverride, "24000/1001")
        XCTAssertEqual(migratedOptions.resolutionOverride, "3840x2160")
        XCTAssertTrue(migratedOptions.cropBlackBars)
        XCTAssertTrue(migratedOptions.swapEyes)
        XCTAssertEqual(migratedOptions.audioHandling, .convertAAC)
        XCTAssertEqual(migratedOptions.audioBitrate, 512)

        let migratedJSON = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: Data(contentsOf: fileURL)) as? [String: Any]
        )
        XCTAssertEqual(migratedJSON["version"] as? Int, 3)
        let profiles = try XCTUnwrap(migratedJSON["profiles"] as? [[String: Any]])
        let options = try XCTUnwrap(profiles.first?["options"] as? [String: Any])
        let subtitles = try XCTUnwrap(options["subtitles"] as? [String: Any])
        XCTAssertEqual(subtitles["mode"] as? String, "off")
        XCTAssertEqual(subtitles["preferredLanguage"] as? String, "nld")
        XCTAssertNil(options["language"])
        XCTAssertNil(options["includeSubtitles"])
        XCTAssertNil(options["keepExtraLanguages"])

        let reopenedStore = ProfileStore(fileURL: fileURL)
        XCTAssertEqual(reopenedStore.customProfiles, store.customProfiles)
    }

    @MainActor
    func testMigrationWriteFailureKeepsValidVersionOneLibraryAndLoadsReadOnly() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let legacyData = try legacyDocument(
            profiles: [
                legacyProfile(
                    id: "A4CC523E-72FA-4F36-A38D-1FB0D6A84742",
                    name: "Protected",
                    language: "ger",
                    includeSubtitles: true,
                    keepExtraLanguages: false
                )
            ]
        )
        try legacyData.write(to: fileURL)

        let store = ProfileStore(
            fileURL: fileURL,
            dataWriter: { _, _ in throw TestWriteError.failed }
        )

        XCTAssertEqual(store.customProfiles.first?.options.subtitles.preferredLanguage, .german)
        XCTAssertEqual(try Data(contentsOf: fileURL), legacyData)
        XCTAssertNotNil(store.loadErrorMessage)
        XCTAssertThrowsError(try store.createProfile(name: "Blocked", options: EncodingOptions())) { error in
            XCTAssertEqual(error as? ProfileStoreError, .recoveryRequired)
        }
    }

    @MainActor
    func testVersionOneProfileWithUnsupportedLanguageIsPreservedAsCorrupt() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let legacyData = try legacyDocument(
            profiles: [
                legacyProfile(
                    id: "A4CC523E-72FA-4F36-A38D-1FB0D6A84742",
                    name: "Unsupported Language",
                    language: "xyz",
                    includeSubtitles: true,
                    keepExtraLanguages: false
                )
            ]
        )
        try legacyData.write(to: fileURL)

        let store = ProfileStore(fileURL: fileURL)

        XCTAssertTrue(store.customProfiles.isEmpty)
        XCTAssertNotNil(store.loadErrorMessage)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fileURL.path))
        XCTAssertEqual(try Data(contentsOf: fileURL.appendingPathExtension("corrupt")), legacyData)
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
        XCTAssertEqual(store.normalizedProfileID(firstID), BuiltInProfile.balanced.id)
    }

    @MainActor
    func testCustomProfileOrderAndIdentifiersPersistAfterMove() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        var identifiers = [
            UUID(uuidString: "9A853D24-A398-4801-B537-801C6B7AA566")!,
            UUID(uuidString: "E27A632D-C9F0-424D-85B0-77E153AE1DA8")!,
            UUID(uuidString: "842B69CD-EB21-4779-B4A9-7A411180AA43")!,
        ].makeIterator()
        let store = ProfileStore(fileURL: fileURL, idGenerator: { identifiers.next()! })
        let firstID = try store.createProfile(name: "First", options: EncodingOptions())
        let secondID = try store.createProfile(name: "Second", options: EncodingOptions())
        let thirdID = try store.createProfile(name: "Third", options: EncodingOptions())

        try store.moveCustomProfiles(fromOffsets: IndexSet(integer: 0), toOffset: 3)

        let restoredStore = ProfileStore(fileURL: fileURL)
        XCTAssertEqual(restoredStore.customProfiles.map(\.id), [secondID, thirdID, firstID])
        XCTAssertEqual(restoredStore.customProfiles.map(\.name), ["Second", "Third", "First"])
    }

    @MainActor
    func testDuplicateNamesAdvancePastExistingCopies() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let store = ProfileStore(fileURL: fileURL)
        _ = try store.createProfile(name: "Balanced Copy", options: EncodingOptions())
        _ = try store.createProfile(name: "Balanced Copy 2", options: EncodingOptions())

        let identifier = try store.duplicateProfile(BuiltInProfile.balanced.id)

        XCTAssertEqual(store.profile(withID: identifier).name, "Balanced Copy 3")
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

    private func legacyDocument(profiles: [[String: Any]]) throws -> Data {
        try JSONSerialization.data(withJSONObject: ["version": 1, "profiles": profiles], options: [.sortedKeys])
    }

    private func optionsJSON(audioHandlingRawValue: String) throws -> [String: Any] {
        var options = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: JSONEncoder().encode(EncodingOptions())) as? [String: Any]
        )
        options.removeValue(forKey: "videoOutputMode")
        options.removeValue(forKey: "av1CRF")
        options["audioHandling"] = audioHandlingRawValue
        return options
    }

    private func legacyProfile(
        id: String,
        name: String,
        language: String,
        includeSubtitles: Bool,
        keepExtraLanguages: Bool
    ) -> [String: Any] {
        [
            "id": id,
            "name": name,
            "options": [
                "hevcQuality": 91,
                "leftRightBitrate": 35,
                "upscaleEnabled": true,
                "upscaleQuality": 87,
                "linkQuality": false,
                "fieldOfView": 100,
                "frameRateOverride": "24000/1001",
                "resolutionOverride": "3840x2160",
                "cropBlackBars": true,
                "swapEyes": true,
                "audioHandling": "transcodeAAC",
                "audioBitrate": 512,
                "language": language,
                "includeSubtitles": includeSubtitles,
                "keepExtraLanguages": keepExtraLanguages,
            ],
        ]
    }
}

private enum TestWriteError: Error {
    case failed
}

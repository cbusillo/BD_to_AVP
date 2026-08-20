import Foundation
import XCTest
@testable import BluRayToVisionPro

final class ProfileStoreTests: XCTestCase {
    @MainActor
    func testDeleteRollsBackProfileMemoriesWhenProfileWriteFails() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let fileURL = directory.appendingPathComponent("profiles.json")
        var failWrites = false
        let memoryStore = ResolutionMemoryStore.inMemory()
        let store = ProfileStore(
            fileURL: fileURL,
            dataWriter: { data, url in
                if failWrites {
                    throw TestWriteError.failed
                }
                try data.write(to: url, options: .atomic)
            },
            resolutionMemoryStore: memoryStore
        )
        let identifier = try store.createProfile(name: "Rollback", options: EncodingOptions())
        try memoryStore.store(
            resolutionID: "keep_requested_workflow:v2",
            for: "conflict",
            scope: .profile(identifier),
            mappingVersion: 2
        )
        failWrites = true

        XCTAssertThrowsError(try store.deleteProfile(identifier))

        XCTAssertNotNil(store.customProfiles.first(where: { $0.id == identifier }))
        XCTAssertNotNil(memoryStore.entries.first(where: { $0.scope == .profile(identifier) }))
    }

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
            mvHEVC: MVHEVCOptions(
                directFinalBitrate: BitratePreference(mode: .custom, customMbps: 48),
                generatedEyeBitrate: BitratePreference(mode: .custom, customMbps: 35),
                generatedMergeQuality: 91,
                linkGeneratedAndUpscaleQuality: false
            ),
            upscaleEnabled: false,
            upscaleQuality: 87,
            fieldOfView: 100,
            frameRateOverride: "24000/1001",
            resolutionOverride: "3840x2160",
            cropBlackBars: true,
            swapEyes: true,
            audioHandling: .convertAAC,
            audioBitrate: 512,
            audioLanguages: AudioLanguagePolicy(mode: .preferredOnly, preferredLanguage: .japanese),
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
    func testCustomProfilePersistsPipelineDefaultsOnly() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let identifier = UUID(uuidString: "6C02DFB0-2B6A-4F6D-9335-3703487FB9D7")!
        let pipelineDefaults = ProfilePipelineDefaults(
            intermediatePolicy: .reusable,
            softwareEncoder: true
        )
        let store = ProfileStore(fileURL: fileURL, idGenerator: { identifier })

        let profileID = try store.createProfile(
            name: "Reusable",
            options: EncodingOptions(),
            pipelineDefaults: pipelineDefaults
        )
        let restoredProfile = ProfileStore(fileURL: fileURL).profile(withID: profileID)

        XCTAssertEqual(restoredProfile.pipelineDefaults, pipelineDefaults)
    }

    func testApplyingProfilePipelineDefaultsLeavesRunScopedOptionsUntouched() {
        var job = JobOptions(
            startStage: .extractMVCAndAudio,
            overwriteExisting: true,
            removeOriginalAfterSuccess: true,
            continueOnError: true,
            outputCommands: true,
            keepAwake: false,
            playSound: false
        )
        job.applyProfilePipelineDefaults(
            ProfilePipelineDefaults(
                intermediatePolicy: .reusable,
                softwareEncoder: true
            )
        )

        XCTAssertEqual(job.startStage, .extractMVCAndAudio)
        XCTAssertEqual(job.intermediatePolicy, .reusable)
        XCTAssertTrue(job.overwriteExisting)
        XCTAssertTrue(job.removeOriginalAfterSuccess)
        XCTAssertTrue(job.continueOnError)
        XCTAssertTrue(job.softwareEncoder)
        XCTAssertTrue(job.outputCommands)
        XCTAssertFalse(job.keepAwake)
        XCTAssertFalse(job.playSound)
    }

    @MainActor
    func testUpdatingProfileWithoutPipelineDefaultsPreservesPipelineDefaults() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let identifier = UUID(uuidString: "9B58E388-CB38-46ED-ADE4-F690F6A40D81")!
        let pipelineDefaults = ProfilePipelineDefaults(intermediatePolicy: .reusable, softwareEncoder: true)
        let store = ProfileStore(fileURL: fileURL, idGenerator: { identifier })
        let profileID = try store.createProfile(
            name: "Preserve Pipeline",
            options: EncodingOptions(),
            pipelineDefaults: pipelineDefaults
        )

        try store.updateProfile(profileID, name: "Renamed Pipeline", options: EncodingOptions())

        XCTAssertEqual(store.profile(withID: profileID).pipelineDefaults, pipelineDefaults)
        XCTAssertEqual(ProfileStore(fileURL: fileURL).profile(withID: profileID).pipelineDefaults, pipelineDefaults)
    }

    @MainActor
    func testLegacyVersionFourEyeBitratesMigrateToExplicitIntent() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let document: [String: Any] = [
            "version": 4,
            "profiles": [
                [
                    "id": "A4CC523E-72FA-4F36-A38D-1FB0D6A84742",
                    "name": "Legacy Automatic",
                    "options": try legacyVersionFourOptions(leftRightBitrate: 20),
                ],
                [
                    "id": "6C02DFB0-2B6A-4F6D-9335-3703487FB9D7",
                    "name": "Legacy Custom",
                    "options": try legacyVersionFourOptions(leftRightBitrate: 35),
                ],
            ],
        ]
        try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys]).write(to: fileURL)

        let store = ProfileStore(fileURL: fileURL)

        XCTAssertNil(store.loadErrorMessage)
        XCTAssertEqual(store.customProfiles[0].options.videoQuality.mode, .ladder)
        XCTAssertEqual(store.customProfiles[0].options.videoQuality.selectedStep, .balanced)
        XCTAssertEqual(store.customProfiles[0].options.mvHEVC.generatedEyeBitrate.mode, .automatic)
        XCTAssertEqual(store.customProfiles[0].options.mvHEVC.generatedEyeBitrate.customMbps, 20)
        XCTAssertEqual(store.customProfiles[0].options.mvHEVC.directFinalBitrate.customMbps, 40)
        XCTAssertEqual(store.customProfiles[0].options.videoQuality.custom.directFinalBitrate.customMbps, 40)
        XCTAssertEqual(store.customProfiles[0].options.videoQuality.custom.generatedEyeBitrate.customMbps, 20)
        XCTAssertEqual(store.customProfiles[0].options.videoQuality.custom.directFinalBitrate.mode, .automatic)
        XCTAssertEqual(store.customProfiles[0].options.videoQuality.custom.generatedEyeBitrate.mode, .automatic)
        XCTAssertEqual(store.customProfiles[1].options.videoQuality.mode, .custom)
        XCTAssertEqual(store.customProfiles[1].options.mvHEVC.generatedEyeBitrate.mode, .custom)
        XCTAssertEqual(store.customProfiles[1].options.mvHEVC.generatedEyeBitrate.customMbps, 35)
        XCTAssertTrue(store.customProfiles.allSatisfy { $0.options.mvHEVC.directFinalBitrate.mode == .automatic })
        XCTAssertNil(store.customProfiles[1].options.mvHEVC.directFinalBitrate.customMbps)
    }

    @MainActor
    func testCurrentVersionFourExplicitCustomEqualToBalancedRemainsCustom() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        var options = EncodingOptions()
        options.mvHEVC.generatedEyeBitrate = BitratePreference(mode: .custom, customMbps: 20)
        let document: [String: Any] = [
            "version": 4,
            "profiles": [
                [
                    "id": "A4CC523E-72FA-4F36-A38D-1FB0D6A84742",
                    "name": "Explicit Twenty",
                    "options": try currentVersionFourOptions(options),
                ]
            ],
        ]
        try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys]).write(to: fileURL)

        let store = ProfileStore(fileURL: fileURL)

        let migrated = try XCTUnwrap(store.customProfiles.first?.options)
        XCTAssertEqual(migrated.videoQuality.mode, .custom)
        XCTAssertEqual(migrated.mvHEVC.generatedEyeBitrate.mode, .custom)
        XCTAssertEqual(migrated.mvHEVC.generatedEyeBitrate.customMbps, 20)
    }

    @MainActor
    func testInvalidLegacyQualityValuesUseCorruptionRecovery() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let document: [String: Any] = [
            "version": 4,
            "profiles": [
                [
                    "id": "A4CC523E-72FA-4F36-A38D-1FB0D6A84742",
                    "name": "Invalid Bitrate",
                    "options": try legacyVersionFourOptions(leftRightBitrate: 501),
                ]
            ],
        ]
        let data = try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
        try data.write(to: fileURL)

        let store = ProfileStore(fileURL: fileURL)

        XCTAssertTrue(store.customProfiles.isEmpty)
        XCTAssertNotNil(store.loadErrorMessage)
        XCTAssertEqual(try Data(contentsOf: fileURL.appendingPathExtension("corrupt")), data)
    }

    @MainActor
    func testVersionSixPersistenceWritesCurrentIntentAndStableMirrorKeys() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        var options = EncodingOptions()
        options.mvHEVC.directFinalBitrate = BitratePreference(mode: .custom, customMbps: 48)
        options.mvHEVC.generatedEyeBitrate = BitratePreference(mode: .automatic, customMbps: 37)
        options.mvHEVC.generatedMergeQuality = 84
        options.mvHEVC.linkGeneratedAndUpscaleQuality = false
        let store = ProfileStore(fileURL: fileURL)

        _ = try store.createProfile(name: "Compatibility", options: options)

        let document = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: Data(contentsOf: fileURL)) as? [String: Any]
        )
        XCTAssertEqual(document["version"] as? Int, 6)
        let profiles = try XCTUnwrap(document["profiles"] as? [[String: Any]])
        let persistedOptions = try XCTUnwrap(profiles.first?["options"] as? [String: Any])
        let currentMVHEVC = try XCTUnwrap(persistedOptions["mvHEVC"] as? [String: Any])
        let videoQuality = try XCTUnwrap(persistedOptions["videoQuality"] as? [String: Any])
        let directFinalBitrate = try XCTUnwrap(currentMVHEVC["directFinalBitrate"] as? [String: Any])
        let generatedEyeBitrate = try XCTUnwrap(currentMVHEVC["generatedEyeBitrate"] as? [String: Any])

        XCTAssertEqual(directFinalBitrate["mode"] as? String, "custom")
        XCTAssertEqual(directFinalBitrate["customMbps"] as? Int, 48)
        XCTAssertEqual(generatedEyeBitrate["mode"] as? String, "automatic")
        XCTAssertEqual(generatedEyeBitrate["customMbps"] as? Int, 37)
        XCTAssertEqual(persistedOptions["hevcQuality"] as? Int, 84)
        XCTAssertEqual(persistedOptions["leftRightBitrate"] as? Int, 37)
        XCTAssertEqual(persistedOptions["linkQuality"] as? Bool, false)
        XCTAssertEqual(videoQuality["mode"] as? String, "custom")
        XCTAssertEqual(videoQuality["lastLadderStep"] as? String, "balanced")

        let stableOptions = try JSONDecoder().decode(
            StableEncodingOptionsV4.self,
            from: JSONSerialization.data(withJSONObject: persistedOptions, options: [.sortedKeys])
        )
        XCTAssertEqual(stableOptions.hevcQuality, 84)
        XCTAssertEqual(stableOptions.leftRightBitrate, 37)
        XCTAssertFalse(stableOptions.linkQuality)
    }

    @MainActor
    func testVersionFiveRequiresVideoQualityIntent() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let document: [String: Any] = [
            "version": 5,
            "profiles": [
                [
                    "id": "A4CC523E-72FA-4F36-A38D-1FB0D6A84742",
                    "name": "Missing Intent",
                    "options": try legacyVersionFourOptions(leftRightBitrate: 20),
                ]
            ],
        ]
        let data = try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
        try data.write(to: fileURL)

        let store = ProfileStore(fileURL: fileURL)

        XCTAssertTrue(store.customProfiles.isEmpty)
        XCTAssertNotNil(store.loadErrorMessage)
        XCTAssertEqual(try Data(contentsOf: fileURL.appendingPathExtension("corrupt")), data)
    }

    @MainActor
    func testVersionFiveRejectsIntentAndConcreteMirrorMismatch() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        var options = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: JSONEncoder().encode(EncodingOptions())) as? [String: Any]
        )
        var mvHEVC = try XCTUnwrap(options["mvHEVC"] as? [String: Any])
        mvHEVC["generatedMergeQuality"] = 84
        options["mvHEVC"] = mvHEVC
        options["hevcQuality"] = 84
        let document: [String: Any] = [
            "version": 5,
            "profiles": [
                [
                    "id": "A4CC523E-72FA-4F36-A38D-1FB0D6A84742",
                    "name": "Mismatched Intent",
                    "options": options,
                ]
            ],
        ]
        let data = try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
        try data.write(to: fileURL)

        let store = ProfileStore(fileURL: fileURL)

        XCTAssertTrue(store.customProfiles.isEmpty)
        XCTAssertNotNil(store.loadErrorMessage)
        XCTAssertEqual(try Data(contentsOf: fileURL.appendingPathExtension("corrupt")), data)
    }

    @MainActor
    func testVersionFiveMigrationPreservesEncodingAndPipelineButDropsEveryUnsafeDefault() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        var options = EncodingOptions()
        options.mvHEVC.generatedMergeQuality = 84
        options.audioLanguages = AudioLanguagePolicy(mode: .preferredOnly, preferredLanguage: .japanese)
        let expectedOptions = try options.normalizedQualityState()
        let document: [String: Any] = [
            "version": 5,
            "profiles": [
                [
                    "id": "A4CC523E-72FA-4F36-A38D-1FB0D6A84742",
                    "name": "Risky Library",
                    "options": try encodedOptions(options),
                    "jobDefaults": [
                        "startStage": ConversionStage.extractMVCAndAudio.rawValue,
                        "intermediatePolicy": IntermediatePolicy.reusable.rawValue,
                        "overwriteExisting": true,
                        "removeOriginalAfterSuccess": true,
                        "continueOnError": true,
                        "softwareEncoder": true,
                        "outputCommands": true,
                    ],
                ]
            ],
        ]
        try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys]).write(to: fileURL)

        let store = ProfileStore(fileURL: fileURL)
        let profile = try XCTUnwrap(store.customProfiles.first)

        XCTAssertEqual(profile.id, "custom.a4cc523e-72fa-4f36-a38d-1fb0d6a84742")
        XCTAssertEqual(profile.name, "Risky Library")
        XCTAssertEqual(profile.options, expectedOptions)
        XCTAssertEqual(
            profile.pipelineDefaults,
            ProfilePipelineDefaults(intermediatePolicy: .reusable, softwareEncoder: true)
        )
        XCTAssertTrue(store.migrationNoticeMessage?.contains("Risky Library") == true)

        let migratedDocument = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: Data(contentsOf: fileURL)) as? [String: Any]
        )
        XCTAssertEqual(migratedDocument["version"] as? Int, 6)
        let migratedProfile = try XCTUnwrap(
            (migratedDocument["profiles"] as? [[String: Any]])?.first
        )
        let pipelineDefaults = try XCTUnwrap(migratedProfile["pipelineDefaults"] as? [String: Any])
        XCTAssertEqual(pipelineDefaults["intermediatePolicy"] as? String, "reusable")
        XCTAssertEqual(pipelineDefaults["softwareEncoder"] as? Bool, true)
        XCTAssertNil(migratedProfile["jobDefaults"])
        XCTAssertNil(pipelineDefaults["startStage"])
        XCTAssertNil(pipelineDefaults["overwriteExisting"])
        XCTAssertNil(pipelineDefaults["removeOriginalAfterSuccess"])
        XCTAssertNil(pipelineDefaults["continueOnError"])
        XCTAssertNil(pipelineDefaults["outputCommands"])
    }

    @MainActor
    func testVersionFiveMigrationIsIdempotentAfterWritingVersionSix() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let document: [String: Any] = [
            "version": 5,
            "profiles": [
                [
                    "id": "A4CC523E-72FA-4F36-A38D-1FB0D6A84742",
                    "name": "Idempotent",
                    "options": try encodedOptions(EncodingOptions()),
                    "jobDefaults": [
                        "startStage": ConversionStage.createMKV.rawValue,
                        "intermediatePolicy": IntermediatePolicy.automatic.rawValue,
                        "overwriteExisting": false,
                        "removeOriginalAfterSuccess": false,
                        "continueOnError": false,
                        "softwareEncoder": false,
                        "outputCommands": false,
                    ],
                ]
            ],
        ]
        try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys]).write(to: fileURL)

        let migratedStore = ProfileStore(fileURL: fileURL)
        let migratedData = try Data(contentsOf: fileURL)
        let reopenedStore = ProfileStore(fileURL: fileURL)

        XCTAssertEqual(reopenedStore.customProfiles, migratedStore.customProfiles)
        XCTAssertNil(reopenedStore.migrationNoticeMessage)
        XCTAssertEqual(try Data(contentsOf: fileURL), migratedData)
    }

    @MainActor
    func testQualificationProfileFixtureMigratesToExpectedVersionSixDocument() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        try qualificationFixtureData("profile_library_v5.json").write(to: fileURL)

        let store = ProfileStore(fileURL: fileURL)

        XCTAssertEqual(store.customProfiles.count, 1)
        let actualDocument = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: Data(contentsOf: fileURL)) as? [String: Any]
        )
        let expectedDocument = try XCTUnwrap(
            try JSONSerialization.jsonObject(
                with: qualificationFixtureData("profile_library_v6.json")
            ) as? [String: Any]
        )
        XCTAssertEqual(
            try JSONSerialization.data(withJSONObject: actualDocument, options: [.sortedKeys]),
            try JSONSerialization.data(withJSONObject: expectedDocument, options: [.sortedKeys])
        )
    }

    @MainActor
    func testVersionFiveMigrationWriteFailureKeepsOriginalAndSafeInMemoryProfiles() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let document: [String: Any] = [
            "version": 5,
            "profiles": [
                [
                    "id": "A4CC523E-72FA-4F36-A38D-1FB0D6A84742",
                    "name": "Read Only",
                    "options": try encodedOptions(EncodingOptions()),
                    "jobDefaults": [
                        "startStage": ConversionStage.extractSubtitles.rawValue,
                        "intermediatePolicy": IntermediatePolicy.reusable.rawValue,
                        "overwriteExisting": true,
                        "removeOriginalAfterSuccess": true,
                        "continueOnError": true,
                        "softwareEncoder": true,
                        "outputCommands": true,
                    ],
                ]
            ],
        ]
        let originalData = try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
        try originalData.write(to: fileURL)

        let store = ProfileStore(
            fileURL: fileURL,
            dataWriter: { _, _ in throw TestWriteError.failed }
        )
        let profile = try XCTUnwrap(store.customProfiles.first)

        XCTAssertEqual(
            profile.pipelineDefaults,
            ProfilePipelineDefaults(intermediatePolicy: .reusable, softwareEncoder: true)
        )
        XCTAssertTrue(store.migrationNoticeMessage?.contains("Read Only") == true)
        XCTAssertNotNil(store.loadErrorMessage)
        XCTAssertEqual(try Data(contentsOf: fileURL), originalData)
        XCTAssertThrowsError(try store.createProfile(name: "Blocked", options: EncodingOptions())) { error in
            XCTAssertEqual(error as? ProfileStoreError, .recoveryRequired)
        }
    }

    func testEncodingOptionsRejectMismatchedCompatibilityKeys() throws {
        var encoded = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: JSONEncoder().encode(EncodingOptions())) as? [String: Any]
        )
        var mvHEVC = try XCTUnwrap(encoded["mvHEVC"] as? [String: Any])
        mvHEVC["generatedMergeQuality"] = 82
        encoded["mvHEVC"] = mvHEVC
        let data = try JSONSerialization.data(withJSONObject: encoded, options: [.sortedKeys])

        XCTAssertThrowsError(try JSONDecoder().decode(EncodingOptions.self, from: data)) { error in
            guard case DecodingError.dataCorrupted = error else {
                return XCTFail("Expected dataCorrupted, received \(error)")
            }
        }
    }

    func testEncodingOptionsRejectCustomBitrateWithoutValue() throws {
        var encoded = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: JSONEncoder().encode(EncodingOptions())) as? [String: Any]
        )
        var mvHEVC = try XCTUnwrap(encoded["mvHEVC"] as? [String: Any])
        var generatedEyeBitrate = try XCTUnwrap(mvHEVC["generatedEyeBitrate"] as? [String: Any])
        generatedEyeBitrate["mode"] = "custom"
        generatedEyeBitrate.removeValue(forKey: "customMbps")
        mvHEVC["generatedEyeBitrate"] = generatedEyeBitrate
        encoded["mvHEVC"] = mvHEVC
        let data = try JSONSerialization.data(withJSONObject: encoded, options: [.sortedKeys])

        XCTAssertThrowsError(try JSONDecoder().decode(EncodingOptions.self, from: data)) { error in
            guard case DecodingError.dataCorrupted = error else {
                return XCTFail("Expected dataCorrupted, received \(error)")
            }
        }
    }

    @MainActor
    func testExplicitPCMAndAACProfilesSurviveAutomaticDefault() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        var pcmOptions = EncodingOptions()
        pcmOptions.audioHandling = .pcm
        var aacOptions = EncodingOptions()
        aacOptions.audioHandling = .convertAAC
        let store = ProfileStore(fileURL: fileURL)

        _ = try store.createProfile(name: "PCM", options: pcmOptions)
        _ = try store.createProfile(name: "AAC", options: aacOptions)

        let restoredStore = ProfileStore(fileURL: fileURL)
        XCTAssertEqual(restoredStore.customProfiles.map(\.options.audioHandling), [.pcm, .convertAAC])
    }

    @MainActor
    func testVersionTwoProfilesMigrateAllAudioHandlingRawValuesToVersionSix() throws {
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
        XCTAssertTrue(store.customProfiles.allSatisfy { $0.options.audioLanguages.mode == .allLanguages })
        XCTAssertTrue(store.customProfiles.allSatisfy { $0.options.videoOutputMode == .mvHEVC })
        XCTAssertTrue(store.customProfiles.allSatisfy { $0.options.av1CRF == 32 })
        for profile in store.customProfiles {
            try store.updateProfile(profile.id, name: profile.name, options: profile.options)
        }

        let persistedDocument = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: Data(contentsOf: fileURL)) as? [String: Any]
        )
        XCTAssertEqual(persistedDocument["version"] as? Int, 6)
        let persistedProfiles = try XCTUnwrap(persistedDocument["profiles"] as? [[String: Any]])
        let persistedRawValues = try persistedProfiles.map { profile in
            let options = try XCTUnwrap(profile["options"] as? [String: Any])
            return try XCTUnwrap(options["audioHandling"] as? String)
        }
        XCTAssertEqual(persistedRawValues, ["preserve", "transcodeAAC", "automatic"])
    }

    @MainActor
    func testVersionThreeProfilesMigrateToAllAudioLanguagesInVersionSix() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        var legacyOptions = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: JSONEncoder().encode(EncodingOptions())) as? [String: Any]
        )
        legacyOptions.removeValue(forKey: "audioLanguages")
        let document: [String: Any] = [
            "version": 3,
            "profiles": [
                [
                    "id": "A4CC523E-72FA-4F36-A38D-1FB0D6A84742",
                    "name": "Version Three",
                    "options": legacyOptions,
                ]
            ],
        ]
        try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys]).write(to: fileURL)

        let store = ProfileStore(fileURL: fileURL)

        let profile = try XCTUnwrap(store.customProfiles.first)
        XCTAssertNil(store.loadErrorMessage)
        XCTAssertEqual(profile.options.audioLanguages.mode, .allLanguages)
        XCTAssertEqual(profile.options.audioLanguages.preferredLanguage, .english)
        let persistedDocument = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: Data(contentsOf: fileURL)) as? [String: Any]
        )
        XCTAssertEqual(persistedDocument["version"] as? Int, 6)
        let persistedProfiles = try XCTUnwrap(persistedDocument["profiles"] as? [[String: Any]])
        let persistedOptions = try XCTUnwrap(persistedProfiles.first?["options"] as? [String: Any])
        let audioLanguages = try XCTUnwrap(persistedOptions["audioLanguages"] as? [String: Any])
        XCTAssertEqual(audioLanguages["mode"] as? String, "all_languages")
        XCTAssertEqual(audioLanguages["preferredLanguage"] as? String, "eng")
    }

    @MainActor
    func testVersionOneProfilesMigrateAtomicallyToCanonicalVersionSixData() throws {
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
        XCTAssertEqual(migratedOptions.mvHEVC.generatedMergeQuality, 91)
        XCTAssertEqual(migratedOptions.mvHEVC.generatedEyeBitrate.mode, .custom)
        XCTAssertEqual(migratedOptions.mvHEVC.generatedEyeBitrate.customMbps, 35)
        XCTAssertEqual(migratedOptions.mvHEVC.directFinalBitrate.mode, .automatic)
        XCTAssertNil(migratedOptions.mvHEVC.directFinalBitrate.customMbps)
        XCTAssertTrue(migratedOptions.upscaleEnabled)
        XCTAssertEqual(migratedOptions.upscaleQuality, 87)
        XCTAssertFalse(migratedOptions.mvHEVC.linkGeneratedAndUpscaleQuality)
        XCTAssertEqual(migratedOptions.fieldOfView, 100)
        XCTAssertEqual(migratedOptions.frameRateOverride, "24000/1001")
        XCTAssertEqual(migratedOptions.resolutionOverride, "3840x2160")
        XCTAssertTrue(migratedOptions.cropBlackBars)
        XCTAssertTrue(migratedOptions.swapEyes)
        XCTAssertEqual(migratedOptions.audioHandling, .convertAAC)
        XCTAssertEqual(migratedOptions.audioBitrate, 512)
        XCTAssertEqual(migratedOptions.audioLanguages.mode, .allLanguages)

        let migratedJSON = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: Data(contentsOf: fileURL)) as? [String: Any]
        )
        XCTAssertEqual(migratedJSON["version"] as? Int, 6)
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
        updatedOptions.mvHEVC.generatedMergeQuality = 82
        try store.updateProfile(firstID, name: "Living Room", options: updatedOptions)
        let secondID = try store.duplicateProfile(firstID)

        XCTAssertEqual(store.profile(withID: firstID).name, "Living Room")
        XCTAssertEqual(store.profile(withID: firstID).options.mvHEVC.generatedMergeQuality, 82)
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
    func testDuplicateProfilePreservesEncodingAndPipelineDefaults() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        var identifiers = [
            UUID(uuidString: "6C02DFB0-2B6A-4F6D-9335-3703487FB9D7")!,
            UUID(uuidString: "9B58E388-CB38-46ED-ADE4-F690F6A40D81")!,
        ].makeIterator()
        let store = ProfileStore(fileURL: fileURL, idGenerator: { identifiers.next()! })
        var options = EncodingOptions()
        options.mvHEVC.generatedMergeQuality = 84
        let pipelineDefaults = ProfilePipelineDefaults(intermediatePolicy: .reusable, softwareEncoder: true)

        let originalID = try store.createProfile(
            name: "Theater",
            options: options,
            pipelineDefaults: pipelineDefaults
        )
        let duplicateID = try store.duplicateProfile(originalID)

        XCTAssertEqual(store.profile(withID: duplicateID).name, "Theater Copy")
        XCTAssertEqual(store.profile(withID: duplicateID).options, store.profile(withID: originalID).options)
        XCTAssertEqual(
            store.profile(withID: duplicateID).pipelineDefaults,
            store.profile(withID: originalID).pipelineDefaults
        )
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
        _ = try store.createProfile(name: "Recommended Copy", options: EncodingOptions())
        _ = try store.createProfile(name: "Recommended Copy 2", options: EncodingOptions())

        let identifier = try store.duplicateProfile(BuiltInProfile.balanced.id)

        XCTAssertEqual(store.profile(withID: identifier).name, "Recommended Copy 3")
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
        XCTAssertEqual(try Data(contentsOf: fileURL), document)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fileURL.appendingPathExtension("corrupt").path))
        XCTAssertThrowsError(try store.createProfile(name: "Blocked", options: EncodingOptions())) { error in
            XCTAssertEqual(error as? ProfileStoreError, .recoveryRequired)
        }
    }

    private func temporaryDirectoryURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("ProfileStoreTests.\(UUID().uuidString)", isDirectory: true)
    }

    private func temporaryProfileURL() -> URL {
        temporaryDirectoryURL().appendingPathComponent("profiles.json")
    }

    private func qualificationFixtureData(_ name: String) throws -> Data {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/\(name)")
        return try Data(contentsOf: fixtureURL)
    }

    private func legacyDocument(profiles: [[String: Any]]) throws -> Data {
        try JSONSerialization.data(withJSONObject: ["version": 1, "profiles": profiles], options: [.sortedKeys])
    }

    private func encodedOptions(_ options: EncodingOptions) throws -> [String: Any] {
        try XCTUnwrap(
            try JSONSerialization.jsonObject(with: JSONEncoder().encode(options)) as? [String: Any]
        )
    }

    private func optionsJSON(audioHandlingRawValue: String) throws -> [String: Any] {
        var options = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: JSONEncoder().encode(EncodingOptions())) as? [String: Any]
        )
        options.removeValue(forKey: "videoOutputMode")
        options.removeValue(forKey: "av1CRF")
        options.removeValue(forKey: "audioLanguages")
        options["audioHandling"] = audioHandlingRawValue
        return options
    }

    private func legacyVersionFourOptions(leftRightBitrate: Int) throws -> [String: Any] {
        var options = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: JSONEncoder().encode(EncodingOptions())) as? [String: Any]
        )
        options.removeValue(forKey: "mvHEVC")
        options.removeValue(forKey: "videoQuality")
        options["leftRightBitrate"] = leftRightBitrate
        return options
    }

    private func currentVersionFourOptions(_ options: EncodingOptions) throws -> [String: Any] {
        var encoded = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: JSONEncoder().encode(options)) as? [String: Any]
        )
        encoded.removeValue(forKey: "videoQuality")
        return encoded
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

private struct StableEncodingOptionsV4: Decodable {
    let videoOutputMode: VideoOutputMode
    let av1CRF: Int
    let hevcQuality: Int
    let leftRightBitrate: Int
    let upscaleEnabled: Bool
    let upscaleQuality: Int
    let linkQuality: Bool
    let fieldOfView: Int
    let frameRateOverride: String
    let resolutionOverride: String
    let cropBlackBars: Bool
    let swapEyes: Bool
    let audioHandling: AudioHandling
    let audioBitrate: Int
    let audioLanguages: AudioLanguagePolicy
    let subtitles: SubtitlePolicy
}

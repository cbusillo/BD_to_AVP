import Foundation
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class SetupEditSessionTests: XCTestCase {
    func testCancelRestoresExactPreEditState() {
        let profile = BuiltInProfile.balanced.profile
        var session = SetupEditSession(profile: profile, options: ConversionOptions())
        var editedOptions = session.draftOptions
        editedOptions.encoding.audioBitrate = 512
        editedOptions.job.overwriteExisting = true
        session.updateName("Temporary Name")
        session.updateOptions(editedOptions)

        XCTAssertTrue(session.isDirty)
        session.discard()

        XCTAssertFalse(session.isDirty)
        XCTAssertEqual(session.draftOptions, session.originalOptions)
        XCTAssertEqual(session.profileName, profile.name)
    }

    func testApplyOnlyLeavesProfileLibraryByteIdentical() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let store = ProfileStore(fileURL: fileURL)
        let profileID = try store.createProfile(name: "Cinema", options: EncodingOptions())
        let before = try Data(contentsOf: fileURL)
        var session = SetupEditSession(
            profile: store.profile(withID: profileID),
            options: ConversionOptions(encoding: store.profile(withID: profileID).options)
        )
        var editedOptions = session.draftOptions
        editedOptions.encoding.audioBitrate = 512
        session.updateOptions(editedOptions)

        XCTAssertNotEqual(session.draftOptions, session.originalOptions)
        XCTAssertEqual(try Data(contentsOf: fileURL), before)
        XCTAssertEqual(store.profile(withID: profileID).options.audioBitrate, 384)
    }

    func testCustomUpdateWritesOneProfileWithRunScopedOptionsExcluded() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let store = ProfileStore(fileURL: fileURL)
        let profileID = try store.createProfile(name: "Cinema", options: EncodingOptions())
        var conversionOptions = ConversionOptions(encoding: store.profile(withID: profileID).options)
        conversionOptions.job.overwriteExisting = true
        var session = SetupEditSession(profile: store.profile(withID: profileID), options: conversionOptions)
        var editedOptions = session.draftOptions
        editedOptions.encoding.audioBitrate = 512
        editedOptions.job.removeOriginalAfterSuccess = true
        session.updateOptions(editedOptions)
        session.updateName("Cinema Updated")

        let values = session.profileWriteValues()
        try store.updateProfile(
            profileID,
            name: values.name,
            options: values.options,
            pipelineDefaults: values.pipelineDefaults
        )

        let restored = store.profile(withID: profileID)
        XCTAssertEqual(restored.name, "Cinema Updated")
        XCTAssertEqual(restored.options.audioBitrate, 512)
        XCTAssertFalse(try String(decoding: Data(contentsOf: fileURL), as: UTF8.self).contains("overwriteExisting"))
    }

    func testSaveAsNewFromBuiltInCreatesCustomProfile() throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("profiles.json")
        let store = ProfileStore(fileURL: fileURL)
        var session = SetupEditSession(
            profile: BuiltInProfile.balanced.profile,
            options: ConversionOptions(encoding: BuiltInProfile.balanced.options)
        )
        var editedOptions = session.draftOptions
        editedOptions.encoding.audioBitrate = 512
        session.updateOptions(editedOptions)

        let values = session.profileWriteValues()
        let newID = try store.createProfile(
            name: "My Standard Movie",
            options: values.options,
            pipelineDefaults: values.pipelineDefaults
        )

        XCTAssertTrue(newID.hasPrefix("custom."))
        XCTAssertEqual(store.profile(withID: newID).options.audioBitrate, 512)
    }

    func testBuiltInProfileIsReadOnly() throws {
        let store = ProfileStore(fileURL: temporaryProfileURL())

        XCTAssertThrowsError(
            try store.updateProfile(
                BuiltInProfile.balanced.id,
                name: "Changed",
                options: EncodingOptions()
            )
        ) { error in
            XCTAssertEqual(error as? ProfileStoreError, .builtInProfileIsReadOnly)
        }
    }

    func testDirtyProfileSwitchRequiresDiscardAndReload() {
        let first = BuiltInProfile.balanced.profile
        let second = BuiltInProfile.originalResolution.profile
        var session = SetupEditSession(profile: first, options: ConversionOptions(encoding: first.options))
        var editedOptions = session.draftOptions
        editedOptions.encoding.audioBitrate = 512
        session.updateOptions(editedOptions)

        XCTAssertTrue(session.isDirty)
        let reloaded = session.switching(
            to: second,
            options: ConversionOptions(encoding: second.options)
        )
        XCTAssertEqual(reloaded.profileID, second.id)
        XCTAssertFalse(reloaded.isDirty)
    }

    func testReusableFileOutcomesAreExplicitAndExplainConsequences() {
        XCTAssertEqual(
            ReusableFileOutcome.finishedMovieOnly.title,
            "Just the finished movie"
        )
        XCTAssertEqual(
            ReusableFileOutcome.finishedMovieAndReusableFiles.title,
            "The finished movie plus reusable files"
        )
        XCTAssertTrue(ReusableFileOutcome.finishedMovieAndReusableFiles.detail.contains("left- and right-eye"))
        XCTAssertTrue(ReusableFileOutcome.finishedMovieAndReusableFiles.detail.contains("storage"))
        XCTAssertTrue(ReusableFileOutcome.finishedMovieAndReusableFiles.detail.contains("quality"))
        XCTAssertEqual(
            ReusableFileOutcome(policy: .reusable),
            .finishedMovieAndReusableFiles
        )
    }

    func testRecommendedKeepsBalancedIdentifier() {
        XCTAssertEqual(BuiltInProfile.balanced.id, "builtin.balanced")
        XCTAssertEqual(BuiltInProfile.balanced.name, "Recommended")
        XCTAssertEqual(BuiltInProfile.originalResolution.name, "Higher Quality")
    }

    func testUIInventoryKeepsReadyActionsAndAdvancedControlsReachable() throws {
        let rootURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let setupSource = try String(
            contentsOf: rootURL.appendingPathComponent("macos/BluRayToVisionPro/Views/ConversionSetupView.swift"),
            encoding: .utf8
        )
        let sheetSource = try String(
            contentsOf: rootURL.appendingPathComponent("macos/BluRayToVisionPro/Feature/SetupEditSession.swift"),
            encoding: .utf8
        )
        let workflowSource = try String(
            contentsOf: rootURL.appendingPathComponent("macos/BluRayToVisionPro/Models/ConversionWorkflow.swift"),
            encoding: .utf8
        )
        let optionsSource = try String(
            contentsOf: rootURL.appendingPathComponent("macos/BluRayToVisionPro/Models/ConversionOptions.swift"),
            encoding: .utf8
        )
        let inventorySource = setupSource + sheetSource + workflowSource + optionsSource

        for marker in [
            "ready-profile-picker",
            "edit-conversion-settings",
            "setup-editor-section-video",
            "setup-editor-section-audioAndSubtitles",
            "setup-editor-section-filesAndRecovery",
            "Audio & Subtitles",
            "Files & Recovery",
            "Start stage",
            "Overwrite an existing output file",
            "Remove original after success",
            "Keep the Mac awake",
            "Show generated commands in activity",
            "Just the finished movie",
            "The finished movie plus reusable files",
        ] {
            XCTAssertTrue(inventorySource.contains(marker), "Missing UI inventory marker: \(marker)")
        }
        for marker in [
            "Cancel",
            "Apply to This Conversion",
            "Update Profile",
            "Save as New Profile…",
            "Discard and Load",
        ] {
            XCTAssertTrue(inventorySource.contains(marker), "Missing editor action marker: \(marker)")
        }
    }

    private func temporaryDirectoryURL() -> URL {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("bd-to-avp-573-\(UUID().uuidString)", isDirectory: true)
        try! FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        return directoryURL
    }

    private func temporaryProfileURL() -> URL {
        temporaryDirectoryURL().appendingPathComponent("profiles.json")
    }
}

import Foundation
import XCTest
@testable import BluRayToVisionPro

final class AppSettingsTests: XCTestCase {
    @MainActor
    func testPreferencesRoundTripThroughUserDefaults() {
        let suiteName = "AppSettingsTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let homeURL = URL(fileURLWithPath: "/Users/example", isDirectory: true)

        let settings = AppSettings(defaults: defaults, homeDirectoryURL: homeURL)
        settings.selectedProfileID = BuiltInProfile.fourKUpscale.id
        settings.destinationURL = URL(fileURLWithPath: "/Volumes/Media", isDirectory: true)
        settings.showTechnicalDetails = true
        settings.intermediatePolicy = .reusable
        settings.useSoftwareEncoder = true

        let restored = AppSettings(defaults: defaults, homeDirectoryURL: homeURL)

        XCTAssertEqual(restored.selectedProfileID, BuiltInProfile.fourKUpscale.id)
        XCTAssertEqual(restored.destinationURL.path, "/Volumes/Media")
        XCTAssertTrue(restored.showTechnicalDetails)
        XCTAssertEqual(restored.intermediatePolicy, .reusable)
        XCTAssertTrue(restored.useSoftwareEncoder)
        XCTAssertEqual(defaults.object(forKey: "native.keepIntermediateFiles") as? Bool, true)
    }

    @MainActor
    func testProfileIdentifierIsPreservedAndMissingDestinationUsesSafeDefault() {
        let suiteName = "AppSettingsTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }
        defaults.set("removed-profile", forKey: "native.defaultProfile")
        defaults.set(true, forKey: "native.keepIntermediateFiles")
        let homeURL = URL(fileURLWithPath: "/Users/example", isDirectory: true)

        let settings = AppSettings(defaults: defaults, homeDirectoryURL: homeURL)

        XCTAssertEqual(settings.selectedProfileID, "removed-profile")
        XCTAssertEqual(settings.destinationURL.path, "/Users/example/Movies")
        XCTAssertEqual(settings.intermediatePolicy, .reusable)
    }

}

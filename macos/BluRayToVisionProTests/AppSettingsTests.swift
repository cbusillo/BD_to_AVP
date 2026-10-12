import Foundation
import XCTest
@testable import BluRayToVisionPro

final class AppSettingsTests: XCTestCase {
    @MainActor
    func testPreferencesRoundTripThroughUserDefaults() throws {
        let defaults = try isolatedDefaults()
        let homeURL = URL(fileURLWithPath: "/Users/example", isDirectory: true)

        let settings = AppSettings(defaults: defaults, homeDirectoryURL: homeURL)
        settings.selectedProfileID = BuiltInProfile.fourKUpscale.id
        settings.destinationURL = URL(fileURLWithPath: "/Volumes/Media", isDirectory: true)
        settings.showTechnicalDetails = true
        settings.intermediatePolicy = .reusable
        settings.useSoftwareEncoder = true
        settings.notifyWhenQueueFinishes = true
        settings.notifyWhenQueueNeedsAttention = true

        let restored = AppSettings(defaults: defaults, homeDirectoryURL: homeURL)

        XCTAssertEqual(restored.selectedProfileID, BuiltInProfile.fourKUpscale.id)
        XCTAssertEqual(restored.destinationURL.path, "/Volumes/Media")
        XCTAssertTrue(restored.showTechnicalDetails)
        XCTAssertEqual(restored.intermediatePolicy, .reusable)
        XCTAssertTrue(restored.useSoftwareEncoder)
        XCTAssertTrue(restored.notifyWhenQueueFinishes)
        XCTAssertTrue(restored.notifyWhenQueueNeedsAttention)
        XCTAssertTrue(restored.queueNotificationsEnabled)
        XCTAssertEqual(defaults.object(forKey: "native.keepIntermediateFiles") as? Bool, true)
        XCTAssertEqual(defaults.object(forKey: "native.notifyWhenQueueFinishes") as? Bool, true)
        XCTAssertEqual(defaults.object(forKey: "native.notifyWhenQueueNeedsAttention") as? Bool, true)
    }

    @MainActor
    func testQueueNotificationsDefaultOff() throws {
        let defaults = try isolatedDefaults()

        let settings = AppSettings(defaults: defaults)

        XCTAssertFalse(settings.notifyWhenQueueFinishes)
        XCTAssertFalse(settings.notifyWhenQueueNeedsAttention)
        XCTAssertFalse(settings.queueNotificationsEnabled)
    }

    @MainActor
    func testProfileIdentifierIsPreservedAndMissingDestinationUsesSafeDefault() throws {
        let defaults = try isolatedDefaults()
        defaults.set("removed-profile", forKey: "native.defaultProfile")
        defaults.set(true, forKey: "native.keepIntermediateFiles")
        let homeURL = URL(fileURLWithPath: "/Users/example", isDirectory: true)

        let settings = AppSettings(defaults: defaults, homeDirectoryURL: homeURL)

        XCTAssertEqual(settings.selectedProfileID, "removed-profile")
        XCTAssertEqual(settings.destinationURL.path, "/Users/example/Movies")
        XCTAssertEqual(settings.intermediatePolicy, .reusable)
    }

}

import Foundation
import XCTest
@testable import BluRayToVisionPro

final class IsolatedUserDefaultsTests: XCTestCase {
    func testTemporaryDomainPersistsAndTeardownRemovesItsPlist() throws {
        let fixture = try IsolatedUserDefaults()
        defer {
            if FileManager.default.fileExists(atPath: fixture.directory.path) {
                try? fixture.tearDown()
            }
        }
        let key = UUID().uuidString
        let value = UUID().uuidString
        fixture.defaults.set(value, forKey: key)
        XCTAssertTrue(fixture.defaults.synchronize())

        let plist = URL(fileURLWithPath: fixture.domain).appendingPathExtension("plist")
        let stored = try XCTUnwrap(PropertyListSerialization.propertyList(from: Data(contentsOf: plist), format: nil) as? [String: Any])
        XCTAssertEqual(stored[key] as? String, value)
        XCTAssertEqual(UserDefaults(suiteName: fixture.domain)?.string(forKey: key), value)

        try fixture.tearDown()
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.directory.path))
        XCTAssertNil(fixture.defaults.object(forKey: key))
    }

    func testFixturesDoNotShareValues() throws {
        let first = try isolatedDefaults()
        let second = try isolatedDefaults()
        let key = UUID().uuidString
        first.set(true, forKey: key)
        XCTAssertNil(second.object(forKey: key))
    }

    @MainActor
    func testDefaultAppSettingsUseTheTemporaryHostDomain() {
        let settings = AppSettings()
        let original = settings.useSoftwareEncoder
        defer { settings.useSoftwareEncoder = original }
        settings.useSoftwareEncoder.toggle()
        let restored = AppSettings(defaults: AppPreferences.current)
        XCTAssertEqual(restored.useSoftwareEncoder, settings.useSoftwareEncoder)
    }

    @MainActor
    func testMovieSharingConstructionDoesNotReadOrWriteIdentity() throws {
        let trust = MovieLibraryTrustStore(
            read: {
                XCTFail("Constructing the test host must not read its identity")
                return nil
            },
            write: { _ in
                XCTFail("Constructing the test host must not write its identity")
            }
        )
        let controller = MovieSharingController(defaults: try isolatedDefaults(), trust: trust)
        XCTAssertTrue(controller.sources.isEmpty)
        XCTAssertTrue(controller.peers.isEmpty)
        XCTAssertNil(controller.errorMessage)
    }

    func testHostUsesTemporaryHomeForPreferencesAndApplicationSupport() throws {
        let environment = ProcessInfo.processInfo.environment
        let home = try XCTUnwrap(environment["CFFIXED_USER_HOME"])
        XCTAssertFalse(home.isEmpty)
        XCTAssertEqual(environment["HOME"], home)
        XCTAssertEqual(FileManager.default.homeDirectoryForCurrentUser.path, home)
        let support = try XCTUnwrap(FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first)
        XCTAssertTrue(support.path.hasPrefix(home + "/"))

        let key = UUID().uuidString
        let defaults = AppPreferences.current
        defaults.set(true, forKey: key)
        defer { defaults.removeObject(forKey: key) }
        XCTAssertTrue(defaults.synchronize())
        let preferences = URL(fileURLWithPath: home).appendingPathComponent("Library/Preferences")
        let plists = try FileManager.default.contentsOfDirectory(at: preferences, includingPropertiesForKeys: nil)
        let values = try plists.map {
            try PropertyListSerialization.propertyList(from: Data(contentsOf: $0), format: nil) as? [String: Any]
        }
        XCTAssertTrue(values.contains { $0?[key] as? Bool == true })
    }
}

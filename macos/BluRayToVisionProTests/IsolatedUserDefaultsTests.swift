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
        let stored = try XCTUnwrap(
            PropertyListSerialization.propertyList(from: Data(contentsOf: plist), format: nil) as? [String: Any]
        )
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

    @MainActor
    func testMovieSharingReadsAndWritesIdentityOffTheMainThread() async throws {
        // A Keychain call can wait on an access prompt, which would freeze the window.
        let calls = TrustStoreThreadRecorder()
        let trust = MovieLibraryTrustStore(read: { calls.record(); return nil }, write: { _ in calls.record() })
        let controller = MovieSharingController(defaults: try isolatedDefaults(), trust: trust)

        await controller.restoreIfNeeded()
        await controller.forget(MovieTrustedPeer(publicKey: Data(repeating: 7, count: 32), name: "Headset"))

        XCTAssertNil(controller.errorMessage)
        XCTAssertGreaterThan(calls.total, 0)
        XCTAssertEqual(calls.onMainThread, 0)
    }

    @MainActor
    func testUnitTestHostNeverSelectsKeychainEvenWhenTeamSigned() {
        XCTAssertFalse(MovieSharingController.usesKeychain())
        let environment = [AppDelegate.unitTestHomeEnvironmentKey: "/tmp/test-home"]
        XCTAssertFalse(MovieSharingController.usesKeychain(environment: environment, isTeamSigned: { true }))
        XCTAssertTrue(MovieSharingController.usesKeychain(environment: [:], isTeamSigned: { true }))
        XCTAssertFalse(MovieSharingController.usesKeychain(environment: [:], isTeamSigned: { false }))
    }

    func testHostUsesTemporaryHomeForPreferencesAndApplicationSupport() throws {
        let environment = ProcessInfo.processInfo.environment
        let home = try XCTUnwrap(environment["CFFIXED_USER_HOME"])
        XCTAssertFalse(home.isEmpty)
        XCTAssertEqual(environment["HOME"], home)
        XCTAssertEqual(FileManager.default.homeDirectoryForCurrentUser.path, home)
        let support = try XCTUnwrap(
            FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
        )
        XCTAssertTrue(support.path.hasPrefix(home + "/"))
        let domain = try XCTUnwrap(AppPreferences.isolatedDomain)
        let testRoot = URL(fileURLWithPath: home).deletingLastPathComponent().path
        XCTAssertTrue(domain.hasPrefix(testRoot + "/"))
        let fixture = try IsolatedUserDefaults()
        defer { try? fixture.tearDown() }
        XCTAssertTrue(fixture.directory.path.hasPrefix(testRoot + "/"))

        let key = UUID().uuidString
        let defaults = AppPreferences.current
        defaults.set(true, forKey: key)
        defer { defaults.removeObject(forKey: key) }
        XCTAssertTrue(defaults.synchronize())
        let plist = URL(fileURLWithPath: domain).appendingPathExtension("plist")
        let stored = try XCTUnwrap(
            PropertyListSerialization.propertyList(from: Data(contentsOf: plist), format: nil) as? [String: Any]
        )
        XCTAssertEqual(stored[key] as? Bool, true)
    }
}

private final class TrustStoreThreadRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var calls: [Bool] = []
    var total: Int { lock.withLock { calls.count } }
    var onMainThread: Int { lock.withLock { calls.filter { $0 }.count } }
    func record() { let isMain = Thread.isMainThread; lock.withLock { calls.append(isMain) } }
}

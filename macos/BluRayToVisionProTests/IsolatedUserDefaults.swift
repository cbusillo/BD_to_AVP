import Foundation
import XCTest

/// Absolute-path domains persist inside the fixture, never in the user's Preferences.
final class IsolatedUserDefaults {
    let directory: URL
    let domain: String
    let defaults: UserDefaults

    init() throws {
        let root = ProcessInfo.processInfo.environment["CFFIXED_USER_HOME"].map {
            URL(fileURLWithPath: $0, isDirectory: true).deletingLastPathComponent()
        } ?? FileManager.default.temporaryDirectory
        directory = root
            .appendingPathComponent("BDToAVPDefaults-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        domain = directory.appendingPathComponent("preferences").path
        defaults = UserDefaults(suiteName: domain)!
    }

    func tearDown() throws {
        defaults.removePersistentDomain(forName: domain)
        // Flush before removing the directory so cfprefsd cannot recreate an empty plist later.
        defaults.synchronize()
        try FileManager.default.removeItem(at: directory)
    }
}

extension XCTestCase {
    func isolatedDefaults() throws -> UserDefaults {
        let fixture = try IsolatedUserDefaults()
        addTeardownBlock { try fixture.tearDown() }
        return fixture.defaults
    }
}

import Foundation

enum AppPreferences {
    static let isolatedDomain: String? = {
        let environment = ProcessInfo.processInfo.environment
        guard AppDelegate.isUnitTestHost(environment: environment) else {
            return nil
        }
        guard let home = environment["CFFIXED_USER_HOME"], home.hasPrefix("/"),
              environment["HOME"] == home else {
            preconditionFailure("Run unit tests with native_app.py test to supply a disposable home")
        }
        // An explicit path avoids cfprefsd's cached user-domain routing.
        return URL(fileURLWithPath: home)
            .appendingPathComponent("Library/Preferences/test-host").path
    }()

    static let current = isolatedDomain.map { UserDefaults(suiteName: $0)! } ?? .standard
}

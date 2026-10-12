import Foundation

enum AppPreferences {
    static let current: UserDefaults = {
        let environment = ProcessInfo.processInfo.environment
        guard environment["XCTestConfigurationFilePath"] != nil else {
            return .standard
        }
        guard let home = environment["CFFIXED_USER_HOME"], home.hasPrefix("/"),
              environment["HOME"] == home else {
            preconditionFailure("Run unit tests with native_app.py test to supply a disposable home")
        }
        // An explicit path avoids cfprefsd's cached user-domain routing.
        let domain = URL(fileURLWithPath: home)
            .appendingPathComponent("Library/Preferences/test-host").path
        return UserDefaults(suiteName: domain)!
    }()
}

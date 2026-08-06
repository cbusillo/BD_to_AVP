import AppKit
import ApplicationServices
import XCTest

final class InstalledUIAcceptanceTests: XCTestCase {
    private static let profileName = "Tier 3 Accessibility"

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    override func tearDownWithError() throws {
        guard let bundleIdentifier = ProcessInfo.processInfo.environment["BD_TO_AVP_UI_BUNDLE_IDENTIFIER"] else {
            return
        }
        XCUIApplication(bundleIdentifier: bundleIdentifier).terminate()
    }

    func testPriorUpdaterControlsAndReleaseLinks() throws {
        let context = try QualificationContext.load(expectedPhase: "updater")
        let app = try launchInstalledApp(context: context, appearance: .light)
        defer { app.terminate() }

        XCTAssertTrue(app.windows.firstMatch.waitForExistence(timeout: 30))
        openUpdateWindow(in: app)

        let installButton = firstExistingElement(
            in: app.buttons,
            identifiers: ["Install and Relaunch", "Install Update", "Relaunch"],
            timeout: 120
        )
        XCTAssertNotNil(installButton)

        let applicationElement = try AXInspector.applicationElement(bundleIdentifier: context.bundleIdentifier)
        let observedURLs = AXInspector.linkURLs(root: applicationElement)
        XCTAssertTrue(
            observedURLs.contains(context.releaseNotesURL),
            "The Sparkle window did not expose the candidate release-notes URL."
        )

        try writeJSON(
            [
                "install_action": installButton?.label ?? "",
                "release_notes_url": context.releaseNotesURL,
                "release_notes_url_observed": true,
                "schema_version": 1,
                "status": "passed",
            ],
            to: context.outputDirectory.appendingPathComponent("updater-ui.json")
        )
    }

    func testCandidateMainWindowProfileAndSettings() throws {
        let context = try QualificationContext.load(expectedPhase: "candidate")
        let lightApp = try launchInstalledApp(context: context, appearance: .light)
        defer {
            lightApp.terminate()
            try? setAppearance(.light, syntheticHome: context.syntheticHome)
        }

        let mainContent = lightApp.descendants(matching: .any)["main-window-content"]
        XCTAssertTrue(mainContent.waitForExistence(timeout: 30))
        XCTAssertTrue(lightApp.descendants(matching: .any)["main-status"].waitForExistence(timeout: 20))

        let saveAction = lightApp.buttons["save-profile-action"]
        XCTAssertTrue(saveAction.waitForExistence(timeout: 20))
        let applicationElement = try AXInspector.applicationElement(bundleIdentifier: context.bundleIdentifier)
        let saveRecord = try AXInspector.record(identifier: "save-profile-action", root: applicationElement)
        XCTAssertEqual(saveRecord.role, kAXButtonRole as String)
        XCTAssertEqual(saveRecord.label, "Save current settings as new profile")
        XCTAssertEqual(
            saveRecord.help,
            "Opens a form to name and save these settings as a reusable profile"
        )
        XCTAssertTrue(saveRecord.enabled)
        XCTAssertTrue(saveRecord.actions.contains(kAXPressAction as String))

        saveAction.click()
        let nameField = lightApp.textFields["save-profile-name-field"]
        XCTAssertTrue(nameField.waitForExistence(timeout: 20))
        nameField.click()
        nameField.typeText(Self.profileName)
        let confirmButton = lightApp.buttons["save-profile-confirm"]
        XCTAssertTrue(confirmButton.isEnabled)
        confirmButton.click()
        XCTAssertTrue(waitUntil(timeout: 20) { !nameField.exists })

        let profileSummary = try readProfileSummary(syntheticHome: context.syntheticHome)
        XCTAssertEqual(profileSummary.version, 5)
        XCTAssertEqual(profileSummary.count, 1)
        XCTAssertEqual(profileSummary.name, Self.profileName)

        let lightWindow = lightApp.windows.firstMatch
        XCTAssertTrue(lightWindow.exists)
        try lightWindow.screenshot().pngRepresentation.write(
            to: context.outputDirectory.appendingPathComponent("screenshot-light.png"),
            options: .atomic
        )

        openUpdatesSettings(in: lightApp)
        let updateAction = lightApp.buttons["update-action"]
        let routePicker = lightApp.descendants(matching: .any)["update-route-picker"]
        let releasesLink = lightApp.links["all-releases-link"]
        XCTAssertTrue(updateAction.waitForExistence(timeout: 20))
        XCTAssertTrue(routePicker.waitForExistence(timeout: 20))
        XCTAssertTrue(releasesLink.waitForExistence(timeout: 20))

        let updatedApplicationElement = try AXInspector.applicationElement(bundleIdentifier: context.bundleIdentifier)
        let releasesRecord = try AXInspector.record(identifier: "all-releases-link", root: updatedApplicationElement)
        XCTAssertEqual(releasesRecord.url, context.releasesURL)

        let updateRecord = try AXInspector.record(identifier: "update-action", root: updatedApplicationElement)
        let routeRecord = try AXInspector.record(identifier: "update-route-picker", root: updatedApplicationElement)
        let statusRecord = try AXInspector.record(identifier: "main-status", root: updatedApplicationElement)

        try writeJSON(
            [
                "elements": [statusRecord.jsonValue, saveRecord.jsonValue, updateRecord.jsonValue, routeRecord.jsonValue,
                             releasesRecord.jsonValue],
                "schema_version": 1,
            ],
            to: context.outputDirectory.appendingPathComponent("accessibility-tree.json")
        )

        try writeJSON(
            [
                "main_window_ready": true,
                "profile_document_version": profileSummary.version,
                "profile_save_accessible": true,
                "profile_save_succeeded": true,
                "profiles_after": profileSummary.count,
                "release_page_url": context.releasesURL,
                "release_page_url_observed": true,
                "schema_version": 1,
                "status": "passed",
                "updater_controls_accessible": true,
            ],
            to: context.outputDirectory.appendingPathComponent("candidate-ui.json")
        )

        lightApp.terminate()
        let darkApp = try launchInstalledApp(context: context, appearance: .dark)
        defer { darkApp.terminate() }
        let darkWindow = darkApp.windows.firstMatch
        XCTAssertTrue(darkWindow.waitForExistence(timeout: 30))
        try darkWindow.screenshot().pngRepresentation.write(
            to: context.outputDirectory.appendingPathComponent("screenshot-dark.png"),
            options: .atomic
        )
    }

    private func launchInstalledApp(
        context: QualificationContext,
        appearance: QualificationAppearance
    ) throws -> XCUIApplication {
        try setAppearance(appearance, syntheticHome: context.syntheticHome)
        XCUIApplication(bundleIdentifier: context.bundleIdentifier).terminate()

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        process.arguments = [
            "-n", "-F",
            "--env", "HOME=\(context.syntheticHome.path)",
            "--env", "CFFIXED_USER_HOME=\(context.syntheticHome.path)",
            context.appURL.path,
        ]
        try process.run()
        process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, 0)

        let app = XCUIApplication(bundleIdentifier: context.bundleIdentifier)
        XCTAssertTrue(app.windows.firstMatch.waitForExistence(timeout: 30))
        let matches = NSRunningApplication.runningApplications(withBundleIdentifier: context.bundleIdentifier)
        XCTAssertEqual(matches.count, 1)
        XCTAssertEqual(matches.first?.bundleURL?.standardizedFileURL, context.appURL.standardizedFileURL)
        return app
    }

    private func openUpdateWindow(in app: XCUIApplication) {
        let helpItem = app.menuBars.menuBarItems["Help"]
        XCTAssertTrue(helpItem.waitForExistence(timeout: 20))
        helpItem.click()
        let checkItem = app.menuItems["Check for Updates…"]
        XCTAssertTrue(checkItem.waitForExistence(timeout: 20))
        checkItem.click()
    }

    private func openUpdatesSettings(in app: XCUIApplication) {
        app.typeKey(",", modifierFlags: .command)
        let identifierTab = app.descendants(matching: .any)["updates-settings-tab"]
        if identifierTab.waitForExistence(timeout: 10) {
            identifierTab.click()
        } else {
            let titledTab = app.buttons["Updates"]
            XCTAssertTrue(titledTab.waitForExistence(timeout: 10))
            titledTab.click()
        }
        XCTAssertTrue(app.descendants(matching: .any)["updates-settings-pane"].waitForExistence(timeout: 20))
    }

    private func firstExistingElement(
        in query: XCUIElementQuery,
        identifiers: [String],
        timeout: TimeInterval
    ) -> XCUIElement? {
        let deadline = Date().addingTimeInterval(timeout)
        repeat {
            for identifier in identifiers {
                let element = query[identifier]
                if element.exists {
                    return element
                }
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        } while Date() < deadline
        return nil
    }

    private func waitUntil(timeout: TimeInterval, predicate: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        repeat {
            if predicate() {
                return true
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        } while Date() < deadline
        return predicate()
    }
}

private enum QualificationAppearance: Equatable {
    case light
    case dark
}

private struct QualificationContext {
    let appURL: URL
    let bundleIdentifier: String
    let outputDirectory: URL
    let phase: String
    let releaseNotesURL: String
    let releasesURL: String
    let syntheticHome: URL

    static func load(expectedPhase: String) throws -> QualificationContext {
        let environment = ProcessInfo.processInfo.environment
        guard environment["BD_TO_AVP_UI_PHASE"] != nil else {
            throw XCTSkip("Installed UI qualification runs only through the Tier 3 clean-machine runner.")
        }
        func required(_ key: String) throws -> String {
            guard let value = environment[key], !value.isEmpty else {
                throw QualificationError.missingEnvironment(key)
            }
            return value
        }

        let phase = try required("BD_TO_AVP_UI_PHASE")
        guard phase == expectedPhase else {
            throw QualificationError.invalidPhase(expected: expectedPhase, actual: phase)
        }
        let outputDirectory = URL(fileURLWithPath: try required("BD_TO_AVP_UI_OUTPUT_DIRECTORY"), isDirectory: true)
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        return QualificationContext(
            appURL: URL(fileURLWithPath: try required("BD_TO_AVP_UI_APP_PATH"), isDirectory: true),
            bundleIdentifier: try required("BD_TO_AVP_UI_BUNDLE_IDENTIFIER"),
            outputDirectory: outputDirectory,
            phase: phase,
            releaseNotesURL: try required("BD_TO_AVP_UI_RELEASE_NOTES_URL"),
            releasesURL: try required("BD_TO_AVP_UI_RELEASES_URL"),
            syntheticHome: URL(fileURLWithPath: try required("BD_TO_AVP_UI_HOME"), isDirectory: true)
        )
    }
}

private enum QualificationError: LocalizedError {
    case invalidPhase(expected: String, actual: String)
    case missingEnvironment(String)
    case missingProfileDocument
    case missingAccessibilityElement(String)

    var errorDescription: String? {
        switch self {
        case let .invalidPhase(expected, actual):
            "Expected qualification phase \(expected), found \(actual)."
        case let .missingEnvironment(key):
            "Missing required qualification environment variable \(key)."
        case .missingProfileDocument:
            "The profile save did not produce the expected profile document."
        case let .missingAccessibilityElement(identifier):
            "Unable to find accessibility element \(identifier)."
        }
    }
}

private struct ProfileSummary {
    let count: Int
    let name: String
    let version: Int
}

private func readProfileSummary(syntheticHome: URL) throws -> ProfileSummary {
    let profileURL = syntheticHome
        .appendingPathComponent("Library/Application Support/3D Blu-ray to Vision Pro", isDirectory: true)
        .appendingPathComponent("profiles.json")
    let data = try Data(contentsOf: profileURL)
    guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
          let version = root["version"] as? Int,
          let profiles = root["profiles"] as? [[String: Any]],
          let first = profiles.first,
          let name = first["name"] as? String
    else {
        throw QualificationError.missingProfileDocument
    }
    return ProfileSummary(count: profiles.count, name: name, version: version)
}

private func setAppearance(_ appearance: QualificationAppearance, syntheticHome: URL) throws {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/defaults")
    process.environment = [
        "CFFIXED_USER_HOME": syntheticHome.path,
        "HOME": syntheticHome.path,
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    ]
    switch appearance {
    case .light:
        process.arguments = ["delete", "NSGlobalDomain", "AppleInterfaceStyle"]
    case .dark:
        process.arguments = ["write", "NSGlobalDomain", "AppleInterfaceStyle", "Dark"]
    }
    try process.run()
    process.waitUntilExit()
    if appearance == .dark, process.terminationStatus != 0 {
        throw QualificationError.missingEnvironment("synthetic appearance preference")
    }
}

private func writeJSON(_ value: [String: Any], to url: URL) throws {
    guard JSONSerialization.isValidJSONObject(value) else {
        throw QualificationError.missingEnvironment("valid JSON evidence")
    }
    let data = try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys])
    try (data + Data("\n".utf8)).write(to: url, options: .atomic)
}

private struct AXRecord {
    let actions: [String]
    let enabled: Bool
    let help: String
    let identifier: String
    let label: String
    let role: String
    let url: String?

    var jsonValue: [String: Any] {
        var result: [String: Any] = [
            "actions": actions,
            "enabled": enabled,
            "help": help,
            "identifier": identifier,
            "label": label,
            "role": role,
        ]
        if let url {
            result["url"] = url
        }
        return result
    }
}

private enum AXInspector {
    static func applicationElement(bundleIdentifier: String) throws -> AXUIElement {
        let matches = NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier)
        guard matches.count == 1, let processIdentifier = matches.first?.processIdentifier else {
            throw QualificationError.missingAccessibilityElement(bundleIdentifier)
        }
        return AXUIElementCreateApplication(processIdentifier)
    }

    static func record(identifier: String, root: AXUIElement) throws -> AXRecord {
        guard let element = find(identifier: identifier, root: root) else {
            throw QualificationError.missingAccessibilityElement(identifier)
        }
        let actions = actionNames(element).sorted()
        return AXRecord(
            actions: actions,
            enabled: attribute(element, kAXEnabledAttribute) ?? false,
            help: attribute(element, kAXHelpAttribute) ?? "",
            identifier: identifier,
            label: attribute(element, kAXTitleAttribute) ?? attribute(element, kAXDescriptionAttribute) ?? "",
            role: attribute(element, kAXRoleAttribute) ?? "",
            url: normalizedURL(attributeValue(element, kAXURLAttribute))
        )
    }

    static func linkURLs(root: AXUIElement) -> Set<String> {
        var urls = Set<String>()
        walk(root: root) { element in
            let role: String? = attribute(element, kAXRoleAttribute)
            guard role == "AXLink",
                  let url = normalizedURL(attributeValue(element, kAXURLAttribute))
            else {
                return
            }
            urls.insert(url)
        }
        return urls
    }

    private static func find(identifier: String, root: AXUIElement) -> AXUIElement? {
        var result: AXUIElement?
        walk(root: root) { element in
            guard result == nil else {
                return
            }
            let observed: String? = attribute(element, kAXIdentifierAttribute)
            if observed == identifier {
                result = element
            }
        }
        return result
    }

    private static func walk(root: AXUIElement, visit: (AXUIElement) -> Void) {
        var pending = [root]
        var visited = 0
        while let element = pending.popLast(), visited < 2_000 {
            visited += 1
            visit(element)
            let children: [AXUIElement] = attribute(element, kAXChildrenAttribute) ?? []
            pending.append(contentsOf: children.reversed())
        }
    }

    private static func attribute<T>(_ element: AXUIElement, _ name: String) -> T? {
        attributeValue(element, name) as? T
    }

    private static func attributeValue(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else {
            return nil
        }
        return value
    }

    private static func actionNames(_ element: AXUIElement) -> [String] {
        var names: CFArray?
        guard AXUIElementCopyActionNames(element, &names) == .success else {
            return []
        }
        return names as? [String] ?? []
    }

    private static func normalizedURL(_ value: CFTypeRef?) -> String? {
        if let url = value as? URL {
            return url.absoluteString
        }
        if let string = value as? String {
            return string
        }
        return nil
    }
}

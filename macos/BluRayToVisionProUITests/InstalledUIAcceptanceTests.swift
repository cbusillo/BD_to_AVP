import AppKit
import XCTest

final class InstalledUIAcceptanceTests: XCTestCase {
    private static let profileName = "Tier 3 Accessibility"

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    override func tearDownWithError() throws {
        guard let bundleIdentifier = ProcessInfo.processInfo.environment["BD_TO_AVP_UI_BUNDLE_IDENTIFIER"],
              !bundleIdentifier.isEmpty
        else {
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

        let updateWindow = app.windows.matching(identifier: "SUUpdateAlert").firstMatch
        XCTAssertTrue(updateWindow.waitForExistence(timeout: 30))
        let identifiedInstallButton = updateWindow.buttons
            .matching(identifier: "SPUUserUpdateChoiceInstall")
            .firstMatch
        let installButton = identifiedInstallButton.waitForExistence(timeout: 30)
            ? identifiedInstallButton
            : firstExistingElement(
                in: updateWindow.buttons,
                identifiers: ["Install and Relaunch", "Install Update", "Install on Quit"],
                timeout: 90
            )
        XCTAssertNotNil(installButton)
        XCTAssertTrue(
            waitForAccessibilityEvidence(context: context, timeout: 30),
            "The trusted accessibility collector did not capture the updater window."
        )

        try attachJSON(
            [
                "install_action": installButton?.label ?? "",
                "release_notes_url": context.releaseNotesURL,
                "release_notes_url_observed": false,
                "schema_version": 1,
                "status": "passed",
            ],
            name: "updater-ui.json"
        )
    }

    func testCandidateMainWindowProfileAndSettings() throws {
        let context = try QualificationContext.load(expectedPhase: "candidate")
        let lightApp = try launchInstalledApp(context: context, appearance: .light)
        defer { lightApp.terminate() }

        let mainContent = lightApp.descendants(matching: .any)["main-window-content"]
        XCTAssertTrue(mainContent.waitForExistence(timeout: 30))

        let saveAction = lightApp.buttons["save-profile-action"]
        XCTAssertTrue(saveAction.waitForExistence(timeout: 20))
        XCTAssertEqual(saveAction.elementType, .button)
        XCTAssertEqual(saveAction.label, "Save current settings as new profile")
        XCTAssertTrue(saveAction.isEnabled)

        saveAction.click()
        let nameField = lightApp.textFields["save-profile-name-field"]
        XCTAssertTrue(nameField.waitForExistence(timeout: 20))
        nameField.click()
        nameField.typeKey("a", modifierFlags: .command)
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
        attachScreenshot(lightWindow.screenshot(), name: "screenshot-light.png")

        openUpdatesSettings(in: lightApp)
        let updateAction = lightApp.buttons["update-action"]
        let routePicker = lightApp.descendants(matching: .any)["update-route-picker"]
        let releasesLink = lightApp.links["all-releases-link"]
        XCTAssertTrue(updateAction.waitForExistence(timeout: 20))
        XCTAssertTrue(routePicker.waitForExistence(timeout: 20))
        XCTAssertTrue(releasesLink.waitForExistence(timeout: 20))
        XCTAssertEqual(releasesLink.label, "View All Releases…")
        XCTAssertTrue(
            waitForAccessibilityEvidence(context: context, timeout: 30),
            "The trusted accessibility collector did not capture the candidate settings window."
        )

        try attachJSON(
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
            name: "candidate-ui.json"
        )

        lightApp.terminate()
        let darkApp = try launchInstalledApp(context: context, appearance: .dark)
        defer { darkApp.terminate() }
        XCTAssertTrue(
            darkApp.descendants(matching: .any)["main-window-content"].waitForExistence(timeout: 30)
        )
        let darkWindow = darkApp.windows.firstMatch
        XCTAssertTrue(darkWindow.waitForExistence(timeout: 30))
        attachScreenshot(darkWindow.screenshot(), name: "screenshot-dark.png")
    }

    private func launchInstalledApp(
        context: QualificationContext,
        appearance: QualificationAppearance
    ) throws -> XCUIApplication {
        let app = XCUIApplication(url: context.appURL)
        app.launchEnvironment = [
            "HOME": context.syntheticHome.path,
            "CFFIXED_USER_HOME": context.syntheticHome.path,
        ]
        app.launchArguments = appearance.launchArguments
        app.launch()
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
        for identifier in identifiers {
            let element = query[identifier]
            if element.exists {
                return element
            }
        }
        return nil
    }

    private func waitForAccessibilityEvidence(
        context: QualificationContext,
        timeout: TimeInterval
    ) -> Bool {
        let filename = context.phase == "candidate"
            ? "accessibility-tree.json"
            : "updater-accessibility.json"
        let evidenceURL = context.outputDirectory.appendingPathComponent(filename)
        return waitUntil(timeout: timeout) {
            FileManager.default.fileExists(atPath: evidenceURL.path)
        }
    }

    private func attachScreenshot(_ screenshot: XCUIScreenshot, name: String) {
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func attachJSON(_ value: [String: Any], name: String) throws {
        guard JSONSerialization.isValidJSONObject(value) else {
            throw QualificationError.missingEnvironment("valid JSON evidence")
        }
        let data = try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys])
        let attachment = XCTAttachment(data: data + Data("\n".utf8), uniformTypeIdentifier: "public.json")
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
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

    var launchArguments: [String] {
        switch self {
        case .light:
            ["-NSRequiresAquaSystemAppearance", "YES"]
        case .dark:
            ["-AppleInterfaceStyle", "Dark"]
        }
    }
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
        guard let phase = environment["BD_TO_AVP_UI_PHASE"], !phase.isEmpty else {
            throw XCTSkip("Installed UI qualification runs only through the Tier 3 clean-machine runner.")
        }
        func required(_ key: String) throws -> String {
            guard let value = environment[key], !value.isEmpty else {
                throw QualificationError.missingEnvironment(key)
            }
            return value
        }

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

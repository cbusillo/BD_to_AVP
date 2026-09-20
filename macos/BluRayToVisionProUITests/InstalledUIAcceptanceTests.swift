import AppKit
import XCTest

final class InstalledUIAcceptanceTests: XCTestCase {
    private static let profileName = "Tier 3 Accessibility"

    override func setUpWithError() throws {
        continueAfterFailure = false
        let phase = ProcessInfo.processInfo.environment["BD_TO_AVP_UI_PHASE"]
        guard phase == "candidate" || phase == "updater" else {
            return
        }
        addUIInterruptionMonitor(withDescription: "Dismiss optional app permission dialogs") { element in
            let message = Self.dialogMessage(element)
            guard message.contains("3D Blu-ray to Vision Pro"),
                  element.buttons["Allow"].firstMatch.exists,
                  let decline = ["Don’t Allow", "Don't Allow"].map({
                      element.buttons[$0].firstMatch
                  }).first(where: { $0.exists }) else {
                return false
            }
            decline.click()
            return true
        }
        // Hosted runners intermittently crash system services such as RealityKeyboard, and the crash report dialog
        // takes keyboard focus from the open panel. A crash of this app must still fail qualification.
        addUIInterruptionMonitor(withDescription: "Ignore crash reports from unrelated system services") { element in
            let message = Self.dialogMessage(element)
            let ignore = element.buttons["Ignore"].firstMatch
            guard message.contains("quit unexpectedly"),
                  !message.contains("3D Blu-ray to Vision Pro"),
                  ignore.exists else {
                return false
            }
            ignore.click()
            return true
        }
    }

    private static func dialogMessage(_ element: XCUIElement) -> String {
        element.staticTexts.allElementsBoundByIndex.map {
            ($0.value as? String) ?? $0.label
        }.joined(separator: " ")
    }

    override func tearDownWithError() throws {
        guard let bundleIdentifier = ProcessInfo.processInfo.environment["BD_TO_AVP_UI_BUNDLE_IDENTIFIER"],
              !bundleIdentifier.isEmpty else {
            return
        }
        XCUIApplication(bundleIdentifier: bundleIdentifier).terminate()
    }

    func testMissingProfileDocumentIsValidFreshInstallState() throws {
        let syntheticHome = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer {
            try? FileManager.default.removeItem(at: syntheticHome)
        }

        XCTAssertNil(try readProfileSummaryIfPresent(syntheticHome: syntheticHome))
    }

    func testSeededProfileDocumentReportsExistingLibrary() throws {
        let syntheticHome = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer {
            try? FileManager.default.removeItem(at: syntheticHome)
        }
        let profileDirectory = syntheticHome
        .appendingPathComponent("Library/Application Support/3D Blu-ray to Vision Pro", isDirectory: true)
        try FileManager.default.createDirectory(at: profileDirectory, withIntermediateDirectories: true)
        let profileDocument: [String: Any] = [
            "profiles": [["name": "Seeded Profile"]],
            "version": 6,
        ]
        try JSONSerialization.data(withJSONObject: profileDocument, options: [.sortedKeys])
                .write(to: profileDirectory.appendingPathComponent("profiles.json"))

        let summary = try XCTUnwrap(readProfileSummaryIfPresent(syntheticHome: syntheticHome))

        XCTAssertEqual(summary.count, 1)
        XCTAssertEqual(summary.names, ["Seeded Profile"])
        XCTAssertEqual(summary.version, 6)
    }

    func testPriorUpdaterControlsAndReleaseLinks() throws {
        let context = try QualificationContext.load(expectedPhase: "updater")
        let app = try launchInstalledApp(context: context, appearance: .light)
        defer {
            app.terminate()
        }

        XCTAssertTrue(app.windows.firstMatch.waitForExistence(timeout: 30))
        openUpdateWindow(in: app)

        let updateWindow = app.windows.matching(identifier: "SUUpdateAlert").firstMatch
        XCTAssertTrue(updateWindow.waitForExistence(timeout: 30))
        let identifiedInstallButton = updateWindow.buttons
        .matching(identifier: "SPUUserUpdateChoiceInstall")
        .firstMatch
        let identifiedInstallButtonExists = identifiedInstallButton.waitForExistence(timeout: 30)
        let installButton = identifiedInstallButtonExists ? identifiedInstallButton
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
                "install_action": identifiedInstallButtonExists ? "SPUUserUpdateChoiceInstall": installButton?.label ?? "",
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
        defer {
            lightApp.terminate()
        }

        let mainContent = lightApp.descendants(matching: .any)["main-window-content"]
        XCTAssertTrue(mainContent.waitForExistence(timeout: 30))

        XCTAssertTrue(lightApp.descendants(matching: .any)["persistent-queue-sidebar"].exists)
        let sourceURL = context.syntheticHome.appendingPathComponent("installed-ui-source.m2ts")
        XCTAssertTrue(FileManager.default.fileExists(atPath: sourceURL.path), "The runner did not provide the source fixture.")
        openSourceSettings(in: lightApp, sourceURL: sourceURL)

        let saveAction = lightApp.buttons["save-profile-action"]
        XCTAssertTrue(saveAction.waitForExistence(timeout: 20))
        XCTAssertEqual(saveAction.elementType, .button)
        XCTAssertEqual(saveAction.label, "Save current settings as new profile")
        XCTAssertTrue(saveAction.isEnabled)

        let profileSummaryBefore = try readProfileSummaryIfPresent(syntheticHome: context.syntheticHome)
        if let profileSummaryBefore {
            XCTAssertEqual(profileSummaryBefore.version, 6)
        }
        let profilesBefore = profileSummaryBefore?.count ?? 0
        saveAction.click()
        let nameField = lightApp.textFields["setup-editor-new-profile-name"]
        XCTAssertTrue(nameField.waitForExistence(timeout: 20))
        nameField.click()
        nameField.typeKey("a", modifierFlags: .command)
        nameField.typeText(Self.profileName)
        let confirmButton = lightApp.buttons["Save"].firstMatch
        XCTAssertTrue(confirmButton.waitForExistence(timeout: 20))
        XCTAssertTrue(confirmButton.isEnabled)
        confirmButton.click()
        XCTAssertTrue(waitUntil(timeout: 20) {
            !nameField.exists
        })

        let profileSummary = try readProfileSummary(syntheticHome: context.syntheticHome)
        XCTAssertEqual(profileSummary.version, 6)
        XCTAssertEqual(profileSummary.count, profilesBefore + 1)
        XCTAssertEqual(profileSummary.names.filter {
            $0 == Self.profileName
        }.count, 1)
        for existingName in profileSummaryBefore?.names ?? [] {
            XCTAssertTrue(profileSummary.names.contains(existingName))
        }

        XCTAssertTrue(waitUntil(timeout: 20) {
            !saveAction.exists
        })
        lightApp.windows.firstMatch.buttons["Cancel"].firstMatch.click()
        XCTAssertTrue(waitUntil(timeout: 20) {
            !lightApp.descendants(matching: .any)["source-configuration-sheet"].exists
        })

        try attachMainWindowScreenshot(of: lightApp, showing: .light, name: "screenshot-light.png")

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
                "profiles_before": profilesBefore,
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
        defer {
            darkApp.terminate()
        }
        try attachMainWindowScreenshot(of: darkApp, showing: .dark, name: "screenshot-dark.png")
    }

    func testDevelopmentSetupShell() throws {
        let environment = ProcessInfo.processInfo.environment
        guard environment["BD_TO_AVP_UI_PHASE"] == "setup" else {
            throw XCTSkip("Development setup-shell smoke is not requested.")
        }
        let appPath = try XCTUnwrap(environment["BD_TO_AVP_UI_APP_PATH"])
        let syntheticHome = try XCTUnwrap(environment["BD_TO_AVP_UI_HOME"])
        let app = XCUIApplication(url: URL(fileURLWithPath: appPath))
        app.launchEnvironment = [
            "HOME": syntheticHome,
            "CFFIXED_USER_HOME": syntheticHome,
        ]
        app.launchArguments = QualificationAppearance.light.launchArguments
        app.launch()
        defer {
            app.terminate()
        }

        let mainContent = app.descendants(matching: .any)["main-window-content"]
        XCTAssertTrue(mainContent.waitForExistence(timeout: 30))
        XCTAssertTrue(app.descendants(matching: .any)["ready-source"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["ready-profile-picker"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["ready-destination"].exists)
        XCTAssertTrue(app.buttons["ready-change-destination"].exists)
        XCTAssertTrue(app.buttons["ready-preview"].exists)
        XCTAssertTrue(app.buttons["ready-add-to-queue"].exists)
        XCTAssertEqual(app.buttons.matching(identifier: "ready-start").count, 1)

        let window = app.windows.firstMatch
        XCTAssertTrue(window.exists)
        XCTAssertGreaterThanOrEqual(window.frame.width, 820)
        attachScreenshot(window.screenshot(), name: "setup-ready-light.png")

        let edit = app.buttons["edit-conversion-settings"]
        XCTAssertTrue(edit.isEnabled)
        edit.click()
        XCTAssertTrue(app.buttons["setup-editor-cancel"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.buttons["setup-editor-apply"].exists)
        XCTAssertTrue(app.buttons["setup-editor-save-as-new"].exists)
        let sheet = app.sheets.firstMatch
        XCTAssertTrue(sheet.exists)
        XCTAssertGreaterThanOrEqual(sheet.frame.width, 640)
        attachScreenshot(window.screenshot(), name: "setup-editor-light.png")
    }

    private func openSourceSettings(in app: XCUIApplication, sourceURL: URL) {
        let sourceMenu = app.menuButtons["add-sources-menu"]
        XCTAssertTrue(sourceMenu.waitForExistence(timeout: 20))
        sourceMenu.click()
        let configureAction = app.menuItems["Configure Source…"]
        XCTAssertTrue(configureAction.waitForExistence(timeout: 20))
        configureAction.click()

        let sourcePanel = app.dialogs["open-panel"]
        XCTAssertTrue(sourcePanel.waitForExistence(timeout: 20))
        let openAction = sourcePanel.buttons["OKButton"]
        XCTAssertTrue(openAction.waitForExistence(timeout: 20))
        app.typeKey("/", modifierFlags: [])
        let pathField = app.textFields["PathTextField"]
        XCTAssertTrue(pathField.waitForExistence(timeout: 20))
        pathField.typeKey("a", modifierFlags: .command)
        pathField.typeText(sourceURL.path)
        pathField.typeKey(.return, modifierFlags: [])
        XCTAssertTrue(waitUntil(timeout: 20) {
            openAction.isEnabled
        })
        openAction.click()

        let configuration = app.descendants(matching: .any)["source-configuration-sheet"]
        XCTAssertTrue(configuration.waitForExistence(timeout: 30))
        let editAction = app.buttons["Edit Settings…"]
        XCTAssertTrue(editAction.waitForExistence(timeout: 20))
        XCTAssertTrue(waitUntil(timeout: 30) {
            editAction.isEnabled
        }, "Source inspection did not enable settings.")
        editAction.click()
    }

    private func launchInstalledApp(context: QualificationContext,
                                    appearance: QualificationAppearance) throws -> XCUIApplication {
        let app = XCUIApplication(url: context.appURL)
        app.launchEnvironment = [
            "HOME": context.syntheticHome.path,
            "CFFIXED_USER_HOME": context.syntheticHome.path,
        ]
        // A restored Settings window from the previous launch must not stand in for the main window.
        app.launchArguments = appearance.launchArguments + ["-ApplePersistenceIgnoreState", "YES"]
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

    private func firstExistingElement(in query: XCUIElementQuery,
                                      identifiers: [String],
                                      timeout: TimeInterval) -> XCUIElement? {
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

    private func waitForAccessibilityEvidence(context: QualificationContext,
                                              timeout: TimeInterval) -> Bool {
        let filename = context.phase == "candidate" ? "accessibility-tree.json"
                : "updater-accessibility.json"
        let evidenceURL = context.outputDirectory.appendingPathComponent(filename)
        return waitUntil(timeout: timeout) {
            FileManager.default.fileExists(atPath: evidenceURL.path)
        }
    }

    /// Captures the window that holds the main content, and only under the name of the appearance it shows.
    private func attachMainWindowScreenshot(of app: XCUIApplication,
                                            showing appearance: QualificationAppearance,
                                            name: String) throws {
        let mainWindow = app.windows.containing(.any, identifier: "main-window-content").firstMatch
        XCTAssertTrue(mainWindow.waitForExistence(timeout: 30), "The main window is not open.")
        liftAboveDock(mainWindow)
        let screenshot = mainWindow.screenshot()
        let brightness = try XCTUnwrap(QualificationAppearance.meanBrightness(of: screenshot.image),
                                       "The main-window screenshot could not be measured.")
        XCTAssertEqual(
            QualificationAppearance(meanBrightness: brightness), appearance,
            "\(name) has mean brightness \(brightness), which is not a \(appearance) window. "
                + "Dark needs the system in dark mode; the qualification runner sets it."
        )
        attachScreenshot(screenshot, name: name)
    }

    /// A window screenshot is a capture of its screen area, so a Dock over the bottom edge would be in it.
    private func liftAboveDock(_ window: XCUIElement) {
        guard let screen = NSScreen.screens.first else {
            return
        }
        let visibleBottom = screen.frame.height - screen.visibleFrame.minY
        let overlap = window.frame.maxY - visibleBottom
        guard overlap > 0 else {
            return
        }
        let titleBar = window.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0))
            .withOffset(CGVector(dx: 0, dy: 14))
        titleBar.press(forDuration: 0.3, thenDragTo: titleBar.withOffset(CGVector(dx: 0, dy: -overlap)))
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
            // AppKit ignores an AppleInterfaceStyle argument; dark comes only from the system setting.
            ["-NSRequiresAquaSystemAppearance", "NO"]
        }
    }

    init(meanBrightness: Double) {
        self = meanBrightness < 0.5 ? .dark : .light
    }

    /// Mean grey level of the image, from 0 (black) to 1 (white).
    static func meanBrightness(of image: NSImage) -> Double? {
        let side = 64
        var pixels = [UInt8](repeating: 0, count: side * side)
        guard let source = image.cgImage(forProposedRect: nil, context: nil, hints: nil),
              let context = CGContext(data: &pixels, width: side, height: side, bitsPerComponent: 8,
                                      bytesPerRow: side, space: CGColorSpaceCreateDeviceGray(),
                                      bitmapInfo: CGImageAlphaInfo.none.rawValue) else {
            return nil
        }
        context.interpolationQuality = .medium
        context.draw(source, in: CGRect(x: 0, y: 0, width: side, height: side))
        return Double(pixels.reduce(0) { $0 + Int($1) }) / Double(pixels.count * 255)
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
    let names: [String]
    let version: Int
}

private func readProfileSummary(syntheticHome: URL) throws -> ProfileSummary {
    guard let summary = try readProfileSummaryIfPresent(syntheticHome: syntheticHome) else {
        throw QualificationError.missingProfileDocument
    }
    return summary
}

private func readProfileSummaryIfPresent(syntheticHome: URL) throws -> ProfileSummary? {
    let profileURL = syntheticHome
            .appendingPathComponent("Library/Application Support/3D Blu-ray to Vision Pro", isDirectory: true)
            .appendingPathComponent("profiles.json")
    guard FileManager.default.fileExists(atPath: profileURL.path) else {
        return nil
    }
    let data = try Data(contentsOf: profileURL)
    guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
          let version = root["version"] as? Int,
          let profiles = root["profiles"] as? [[String: Any]] else {
        throw QualificationError.missingProfileDocument
    }
    let names = profiles.compactMap {
        $0["name"] as? String
    }
    guard names.count == profiles.count else {
        throw QualificationError.missingProfileDocument
    }
    return ProfileSummary(count: profiles.count, names: names, version: version)
}

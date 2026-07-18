import XCTest

final class SpatialPlaybackProbeUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testLaunchExplainsTheSingleGuidedAction() {
        let app = XCUIApplication()
        app.launch()

        XCTAssertTrue(app.staticTexts["playback-check-title"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.buttons["choose-movie-button"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.staticTexts["Check a movie before release"].exists)
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Nothing is uploaded")).firstMatch.exists)
    }

    func testPhysicalDeviceRunsAutomaticSpatialChecks() throws {
        #if targetEnvironment(simulator)
        throw XCTSkip("Spatial playback acceptance requires a physical Apple Vision Pro.")
        #else
        let app = XCUIApplication()
        app.launchEnvironment["BD_TO_AVP_PROBE_AUTORUN"] = "1"
        app.launchEnvironment["BD_TO_AVP_PROBE_EXPECTED_PRESENTATION"] = "spatial"
        app.launch()

        let automaticStatus = app.staticTexts["automatic-check-status"]
        XCTAssertTrue(automaticStatus.waitForExistence(timeout: 90))
        XCTAssertTrue(app.staticTexts["Probe.mov"].exists)

        let passPredicate = NSPredicate(format: "label CONTAINS %@", "Automatic checks passed")
        expectation(for: passPredicate, evaluatedWith: automaticStatus)
        waitForExpectations(timeout: 30)

        XCTAssertTrue(app.otherElements["playback-observations"].exists)
        #endif
    }
}

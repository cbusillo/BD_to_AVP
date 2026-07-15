import XCTest

final class SpatialPlaybackProbeUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testPhysicalDeviceOpensSpatialPortal() throws {
        #if targetEnvironment(simulator)
        throw XCTSkip("Spatial portal acceptance requires a physical Apple Vision Pro.")
        #else
        let app = XCUIApplication()
        app.launchEnvironment["BD_TO_AVP_PROBE_AUTORUN"] = "1"
        app.launch()

        XCTAssertTrue(app.staticTexts["Probe.mov"].waitForExistence(timeout: 20))

        let openSpatialView = app.buttons["Open Spatial View"]
        XCTAssertTrue(openSpatialView.waitForExistence(timeout: 20))
        openSpatialView.tap()

        XCTAssertTrue(app.buttons["Close Spatial View"].waitForExistence(timeout: 20))

        let presentationStatus = app.descendants(matching: .any)["actual-presentation-status"]
        XCTAssertTrue(presentationStatus.waitForExistence(timeout: 20))

        let spatialPredicate = NSPredicate(format: "label CONTAINS %@", "Stereo · Spatial · Portal")
        expectation(for: spatialPredicate, evaluatedWith: presentationStatus)
        waitForExpectations(timeout: 30)
        #endif
    }
}

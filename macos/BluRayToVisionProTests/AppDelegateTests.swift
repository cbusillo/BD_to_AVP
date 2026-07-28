import XCTest
@testable import BluRayToVisionPro

final class AppDelegateTests: XCTestCase {
    func testStartupSmokeArgumentIsExplicit() {
        XCTAssertTrue(AppDelegate.isStartupSmoke(arguments: ["app", AppDelegate.startupSmokeArgument]))
        XCTAssertFalse(AppDelegate.isStartupSmoke(arguments: ["app"]))
    }

    func testPreviewPresentationSmokeArgumentIsExplicit() {
        XCTAssertTrue(
            AppDelegate.isPreviewPresentationSmoke(
                arguments: ["app", AppDelegate.previewPresentationSmokeArgument, "/tmp/media.mov", "/tmp/result.json"]
            )
        )
        XCTAssertFalse(AppDelegate.isPreviewPresentationSmoke(arguments: ["app"]))
    }

    func testAutomationSmokeIncludesStartupAndPreviewPresentation() {
        XCTAssertTrue(AppDelegate.isAutomationSmoke(arguments: ["app", AppDelegate.startupSmokeArgument]))
        XCTAssertTrue(
            AppDelegate.isAutomationSmoke(arguments: ["app", AppDelegate.previewPresentationSmokeArgument])
        )
        XCTAssertFalse(AppDelegate.isAutomationSmoke(arguments: ["app"]))
    }
}

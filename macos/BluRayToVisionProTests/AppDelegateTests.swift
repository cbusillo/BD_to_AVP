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

    func testWorkerCancellationSmokeArgumentIsExplicit() {
        XCTAssertTrue(
            AppDelegate.isWorkerCancellationSmoke(
                arguments: ["app", AppDelegate.workerCancellationSmokeArgument, "/tmp/worker", "/tmp/destination", "/tmp/result.json"]
            )
        )
        XCTAssertFalse(AppDelegate.isWorkerCancellationSmoke(arguments: ["app"]))
    }

    func testAutomationSmokeIncludesStartupPreviewAndWorkerCancellation() {
        XCTAssertTrue(AppDelegate.isAutomationSmoke(arguments: ["app", AppDelegate.startupSmokeArgument]))
        XCTAssertTrue(
            AppDelegate.isAutomationSmoke(arguments: ["app", AppDelegate.previewPresentationSmokeArgument])
        )
        XCTAssertTrue(
            AppDelegate.isAutomationSmoke(arguments: ["app", AppDelegate.workerCancellationSmokeArgument])
        )
        XCTAssertFalse(AppDelegate.isAutomationSmoke(arguments: ["app"]))
    }
}

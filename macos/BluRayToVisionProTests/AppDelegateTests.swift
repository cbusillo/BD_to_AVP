import XCTest
import UserNotifications
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

    func testDefaultLaunchIsSuppressedForSmokeRunsAndUnitTestHostsOnly() {
        let hosted = ["XCTestConfigurationFilePath": "/tmp/session.xctestconfiguration"]
        XCTAssertTrue(AppDelegate.suppressesDefaultLaunch(arguments: ["app"], environment: hosted))
        XCTAssertTrue(
            AppDelegate.suppressesDefaultLaunch(arguments: ["app", AppDelegate.startupSmokeArgument], environment: [:])
        )
        XCTAssertFalse(AppDelegate.suppressesDefaultLaunch(arguments: ["app"], environment: [:]))
        XCTAssertFalse(AppDelegate.suppressesDefaultLaunch(arguments: ["app"], environment: ["HOME": "/Users/someone"]))
    }

    func testTheAppHostingTheseTestsSuppressesDefaultLaunch() {
        // The real process: fails if Xcode stops marking test hosts the way the app detects them.
        XCTAssertTrue(
            AppDelegate.suppressesDefaultLaunch(
                arguments: ProcessInfo.processInfo.arguments,
                environment: ProcessInfo.processInfo.environment
            )
        )
    }

    func testNotificationPresentationPolicyMatchesAppActivity() {
        XCTAssertEqual(AppDelegate.notificationPresentationOptions(isActive: true), [.list])
        XCTAssertEqual(AppDelegate.notificationPresentationOptions(isActive: false), [.banner, .list, .sound])
    }

}

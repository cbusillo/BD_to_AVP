import XCTest
@testable import BluRayToVisionPro

final class AppDelegateTests: XCTestCase {
    func testStartupSmokeArgumentIsExplicit() {
        XCTAssertTrue(AppDelegate.isStartupSmoke(arguments: ["app", AppDelegate.startupSmokeArgument]))
        XCTAssertFalse(AppDelegate.isStartupSmoke(arguments: ["app"]))
    }
}

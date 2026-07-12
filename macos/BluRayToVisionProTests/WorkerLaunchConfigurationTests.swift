import XCTest
@testable import BluRayToVisionPro

final class WorkerLaunchConfigurationTests: XCTestCase {
    func testSanitizedEnvironmentRemovesDevelopmentOverrides() {
        let environment = WorkerLaunchConfiguration.sanitizedEnvironment(
            from: [
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": "/tmp/python",
                "DYLD_LIBRARY_PATH": "/tmp/libraries",
                "BD_TO_AVP_REPO_ROOT": "/tmp/repository",
                "BD_TO_AVP_FFPROBE_PATH": "/tmp/ffprobe",
            ]
        )

        XCTAssertEqual(environment["PATH"], "/usr/bin:/bin")
        XCTAssertEqual(environment["PYTHONUNBUFFERED"], "1")
        XCTAssertNil(environment["PYTHONPATH"])
        XCTAssertNil(environment["DYLD_LIBRARY_PATH"])
        XCTAssertNil(environment["BD_TO_AVP_REPO_ROOT"])
        XCTAssertNil(environment["BD_TO_AVP_FFPROBE_PATH"])
    }
}

import XCTest
@testable import BluRayToVisionPro

final class WorkerLaunchConfigurationTests: XCTestCase {
    func testSanitizedEnvironmentUsesMinimalToolSafeAllowlist() {
        let environment = WorkerLaunchConfiguration.sanitizedEnvironment(
            from: [
                "HOME": "/Users/tester",
                "LANG": "en_US.UTF-8",
                "LC_CTYPE": "UTF-8",
                "PATH": "/tmp/untrusted-tools:/usr/bin",
                "TMPDIR": "/private/tmp/tester",
                "PYTHONPATH": "/tmp/python",
                "DYLD_LIBRARY_PATH": "/tmp/libraries",
                "BD_TO_AVP_REPO_ROOT": "/tmp/repository",
                "BD_TO_AVP_FFPROBE_PATH": "/tmp/ffprobe",
                "OPENAI_API_KEY": "not-for-the-worker",
                "SSH_AUTH_SOCK": "/private/tmp/ssh-agent.sock",
                "USER": "tester",
            ]
        )

        XCTAssertEqual(
            environment,
            [
                "HOME": "/Users/tester",
                "LANG": "en_US.UTF-8",
                "LC_CTYPE": "UTF-8",
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
                "PYTHONUNBUFFERED": "1",
                "TMPDIR": "/private/tmp/tester",
            ]
        )
    }

    func testSanitizedEnvironmentProvidesTrustedToolSearchPathWhenMissing() {
        let environment = WorkerLaunchConfiguration.sanitizedEnvironment(from: [:])

        XCTAssertEqual(
            environment["PATH"],
            "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
        )
        XCTAssertEqual(environment["PYTHONUNBUFFERED"], "1")
    }
}

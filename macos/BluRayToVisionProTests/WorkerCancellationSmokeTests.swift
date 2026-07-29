import XCTest
@testable import BluRayToVisionPro

final class WorkerCancellationSmokeTests: XCTestCase {
    func testParsesWorkerCancellationSmokeArguments() throws {
        let configuration = try XCTUnwrap(
            WorkerCancellationSmokeConfiguration.parse(
                arguments: [
                    "app",
                    AppDelegate.workerCancellationSmokeArgument,
                    "/tmp/worker",
                    "/tmp/destination",
                    "/tmp/result.json",
                ]
            )
        )

        XCTAssertEqual(configuration.workerURL.path, "/tmp/worker")
        XCTAssertEqual(configuration.destinationURL.path, "/tmp/destination")
        XCTAssertEqual(configuration.resultURL.path, "/tmp/result.json")
    }

    func testRejectsIncompleteWorkerCancellationSmokeArguments() {
        XCTAssertNil(
            WorkerCancellationSmokeConfiguration.parse(
                arguments: ["app", AppDelegate.workerCancellationSmokeArgument, "/tmp/worker"]
            )
        )
    }
}

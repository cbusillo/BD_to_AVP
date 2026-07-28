import XCTest
@testable import BluRayToVisionPro

final class PreviewPresentationSmokeTests: XCTestCase {
    func testParsesPreviewPresentationSmokeArguments() throws {
        let configuration = try XCTUnwrap(
            PreviewPresentationSmokeConfiguration.parse(
                arguments: [
                    "app",
                    AppDelegate.previewPresentationSmokeArgument,
                    "/tmp/smoke/job/preview.mov",
                    "/tmp/smoke/result.json",
                ]
            )
        )

        XCTAssertEqual(configuration.mediaURL.path, "/tmp/smoke/job/preview.mov")
        XCTAssertEqual(configuration.resultURL.path, "/tmp/smoke/result.json")
    }

    func testRejectsIncompletePreviewPresentationSmokeArguments() {
        XCTAssertNil(
            PreviewPresentationSmokeConfiguration.parse(
                arguments: ["app", AppDelegate.previewPresentationSmokeArgument, "/tmp/preview.mov"]
            )
        )
    }
}

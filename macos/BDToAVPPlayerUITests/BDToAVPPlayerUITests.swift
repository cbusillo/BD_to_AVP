import XCTest

final class BDToAVPPlayerUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testSeededMovieDetailsAndPlaybackFlow() throws {
        let app = XCUIApplication()
        app.launch()
        app.buttons["source-on-my-vision-pro"].tap()

        // Decide whether the fixture is present only once the library has rendered, and look the
        // tile up as the button it is. Snapshotting every descendant of a just-launched app can
        // time out while evaluating the query on a slow simulator, which fails the test instead
        // of skipping it.
        XCTAssertTrue(app.staticTexts["built-in-stereo-checks-title"].waitForExistence(timeout: 30))

        let movieID = "documents:playerlongfixture.mov"
        let movieTile = app.buttons["movie-tile-\(movieID)"]
        guard movieTile.waitForExistence(timeout: 10) else {
            throw XCTSkip("Requires PlayerLongFixture.mov in the app Documents directory.")
        }
        let libraryPlayButton = app.buttons["play-library-\(movieID)"]
        XCTAssertTrue(libraryPlayButton.isHittable)

        attachScreenshot(named: "library-populated")
        libraryPlayButton.tap()

        let playPauseButton = app.buttons["player-play-pause"]
        XCTAssertTrue(playPauseButton.waitForExistence(timeout: 30))
        app.buttons["player-done"].tap()
        XCTAssertTrue(waitForHittable(movieTile, timeout: 10))

        movieTile.tap()

        let playButton = app.buttons["play-movie-\(movieID)"]
        XCTAssertTrue(playButton.waitForExistence(timeout: 10))
        XCTAssertTrue(playButton.isHittable, "Play should be visible without scrolling the Details sheet.")
        attachScreenshot(named: "movie-details")

        let detailsDoneButton = app.buttons["details-done"]
        XCTAssertTrue(detailsDoneButton.isHittable)
        detailsDoneButton.tap()
        XCTAssertTrue(waitForHittable(movieTile, timeout: 10))

        movieTile.tap()
        XCTAssertTrue(waitForHittable(playButton, timeout: 10))
        playButton.tap()

        XCTAssertTrue(playPauseButton.waitForExistence(timeout: 30))
        attachScreenshot(named: "player-controls-visible")

        let doneButton = app.buttons["player-done"]
        XCTAssertTrue(doneButton.exists)
        doneButton.tap()
        XCTAssertTrue(playButton.waitForExistence(timeout: 10))

        playButton.tap()
        XCTAssertTrue(playPauseButton.waitForExistence(timeout: 30))

        let hiddenExpectation = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "exists == false"),
            object: playPauseButton
        )
        XCTAssertEqual(XCTWaiter.wait(for: [hiddenExpectation], timeout: 6), .completed)
        attachScreenshot(named: "player-controls-hidden")
        XCTAssertTrue(app.buttons["player-surface"].exists)

        let showControlsButton = app.buttons["player-show-controls"]
        XCTAssertTrue(waitForHittable(showControlsButton, timeout: 5))
        showControlsButton.tap()
        XCTAssertTrue(playPauseButton.waitForExistence(timeout: 5))
    }

    func testBuiltInStereoCheckExposesNativeEyeOrderAndDoneActions() throws {
        let app = XCUIApplication()
        app.launch()
        app.buttons["source-on-my-vision-pro"].tap()

        XCTAssertTrue(app.staticTexts["built-in-stereo-checks-title"].waitForExistence(timeout: 10))

        let startButton = app.buttons["play-builtin:stereo-check-sbs"]
        XCTAssertTrue(startButton.waitForExistence(timeout: 10))
        XCTAssertTrue(startButton.isHittable)
        startButton.tap()

        guard waitForHittableButton(labeled: "Eye Order: Normal", in: app, timeout: 30) else {
            XCTFail(app.debugDescription)
            return
        }
        guard waitForHittableButton(labeled: "Done", in: app, timeout: 5) else {
            XCTFail(app.debugDescription)
            return
        }
        XCTAssertFalse(app.buttons["player-play-pause"].exists)
        XCTAssertFalse(app.buttons["player-show-controls"].exists)

        sleep(5)

        XCTAssertTrue(waitForHittableButton(labeled: "Eye Order: Normal", in: app, timeout: 5))
        XCTAssertTrue(waitForHittableButton(labeled: "Done", in: app, timeout: 5))
    }

    private func attachScreenshot(named name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func waitForHittable(_ element: XCUIElement, timeout: TimeInterval) -> Bool {
        let expectation = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "exists == true AND hittable == true"),
            object: element
        )
        return XCTWaiter.wait(for: [expectation], timeout: timeout) == .completed
    }

    /// AVKit can present its action window twice, so a label may match a hidden copy as well as
    /// the visible button. Wait for either of the first two matches to become hittable. Each check
    /// resolves one element; listing every match and resolving each again could stall on one
    /// whole-tree accessibility snapshot well past this timeout. CI's per-test execution time
    /// allowance bounds any single query that still hangs.
    private func waitForHittableButton(
        labeled label: String,
        in app: XCUIApplication,
        timeout: TimeInterval
    ) -> Bool {
        let matches = app.buttons.matching(NSPredicate(format: "label == %@", label))
        let candidates = [matches.firstMatch, matches.element(boundBy: 1)]
        let anyHittable = NSPredicate { _, _ in
            candidates.contains { $0.exists && $0.isHittable }
        }
        let expectation = XCTNSPredicateExpectation(predicate: anyHittable, object: nil)
        return XCTWaiter.wait(for: [expectation], timeout: timeout) == .completed
    }
}

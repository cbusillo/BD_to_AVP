import XCTest

final class BDToAVPPlayerUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testSeededMovieDetailsAndPlaybackFlow() throws {
        let app = XCUIApplication()
        app.launch()

        let movieID = "documents:playerlongfixture.mov"
        let movieTile = app.descendants(matching: .any)
            .matching(identifier: "movie-tile-\(movieID)")
            .firstMatch
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

        XCTAssertTrue(app.staticTexts["built-in-stereo-checks-title"].waitForExistence(timeout: 10))

        let startButton = app.buttons["play-builtin:stereo-check-sbs"]
        XCTAssertTrue(startButton.waitForExistence(timeout: 10))
        XCTAssertTrue(startButton.isHittable)
        startButton.tap()

        guard firstHittableButton(labeled: "Eye Order: Normal", in: app, timeout: 30) != nil else {
            XCTFail(app.debugDescription)
            return
        }
        guard firstHittableButton(labeled: "Done", in: app, timeout: 5) != nil else {
            XCTFail(app.debugDescription)
            return
        }

        sleep(5)

        XCTAssertNotNil(firstHittableButton(labeled: "Eye Order: Normal", in: app, timeout: 5))
        XCTAssertNotNil(firstHittableButton(labeled: "Done", in: app, timeout: 5))
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

    private func firstHittableButton(
        labeled label: String,
        in app: XCUIApplication,
        timeout: TimeInterval
    ) -> XCUIElement? {
        let buttons = app.buttons.matching(NSPredicate(format: "label == %@", label))
        let deadline = Date().addingTimeInterval(timeout)
        repeat {
            if let button = buttons.allElementsBoundByIndex.first(where: \.isHittable) {
                return button
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        } while Date() < deadline
        return nil
    }

}

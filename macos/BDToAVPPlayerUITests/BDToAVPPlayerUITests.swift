import XCTest

final class BDToAVPPlayerUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testLocationsReplaceAddMovieInTheSidebar() throws {
        let app = XCUIApplication()
        app.launch()

        let addLocation = app.buttons["add-location"]
        XCTAssertTrue(addLocation.waitForExistence(timeout: 30))
        XCTAssertTrue(addLocation.isHittable)
        app.buttons["source-on-my-vision-pro"].tap()
        XCTAssertTrue(app.staticTexts["built-in-stereo-checks-title"].waitForExistence(timeout: 30))
        XCTAssertFalse(app.buttons["Add Movie"].exists)
        attachScreenshot(named: "locations-sidebar")
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

        // The scrub bar spans the movie's full length from the start.
        let scrubBar = app.descendants(matching: .any)["player-scrub-bar"]
        XCTAssertTrue(scrubBar.waitForExistence(timeout: 5))
        XCTAssertGreaterThan(scrubBar.frame.width, 400)
        XCTAssertTrue((scrubBar.value as? String)?.contains("/ 0:24") == true, String(describing: scrubBar.value))
        scrubBar.coordinate(withNormalizedOffset: CGVector(dx: 0.75, dy: 0.5)).tap()
        // Seeks land on a nearby sync sample, so accept anything well past natural progress.
        let seekedExpectation = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "value MATCHES '^0:(1[2-9]|2[0-4]) .*'"),
            object: scrubBar
        )
        XCTAssertEqual(XCTWaiter.wait(for: [seekedExpectation], timeout: 5), .completed, String(describing: scrubBar.value))

        let doneButton = app.buttons["player-done"]
        XCTAssertTrue(doneButton.exists)
        doneButton.tap()
        XCTAssertTrue(playButton.waitForExistence(timeout: 10))

        playButton.tap()
        XCTAssertTrue(playPauseButton.waitForExistence(timeout: 30))

        // Past the auto-hide delay the controls fade but stay in place and interactive, so a
        // look and pinch still works; there is no separate show-controls button.
        // Start near the beginning so playback is still running once the controls fade.
        let replayScrubBar = app.descendants(matching: .any)["player-scrub-bar"]
        XCTAssertTrue(replayScrubBar.waitForExistence(timeout: 5))
        replayScrubBar.coordinate(withNormalizedOffset: CGVector(dx: 0.02, dy: 0.5)).tap()
        XCTAssertEqual(playPauseButton.label, "Pause playback")
        sleep(UInt32(5))
        attachScreenshot(named: "player-controls-faded")
        XCTAssertFalse(app.buttons["player-show-controls"].exists)
        XCTAssertTrue(playPauseButton.isHittable)
        playPauseButton.tap()
        let pausedExpectation = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "label == 'Play movie'"),
            object: playPauseButton
        )
        XCTAssertEqual(XCTWaiter.wait(for: [pausedExpectation], timeout: 5), .completed)
    }

    func testTrackListOpensFromTheControlsAndHoldsThemUp() throws {
        let app = XCUIApplication()
        app.launch()
        app.buttons["source-on-my-vision-pro"].tap()
        XCTAssertTrue(app.staticTexts["built-in-stereo-checks-title"].waitForExistence(timeout: 30))

        let movieID = "documents:playerlongfixture.mov"
        let libraryPlayButton = app.buttons["play-library-\(movieID)"]
        guard libraryPlayButton.waitForExistence(timeout: 10) else {
            throw XCTSkip("Requires PlayerLongFixture.mov in the app Documents directory.")
        }
        libraryPlayButton.tap()

        let playPauseButton = app.buttons["player-play-pause"]
        XCTAssertTrue(playPauseButton.waitForExistence(timeout: 30))

        let audioButton = app.buttons["player-audio-menu"]
        guard waitForHittable(audioButton, timeout: 10), audioButton.isEnabled else {
            throw XCTSkip("Requires a PlayerLongFixture.mov with selectable audio tracks.")
        }

        // The list itself is a RealityView attachment over the video, which the simulator's
        // accessibility snapshot does not include; its height and scrolling rule is covered by
        // the unit tests. Here the button reports the open list and the list holds the controls.
        audioButton.tap()
        XCTAssertTrue(audioButton.isSelected)
        attachScreenshot(named: "player-audio-tracks")

        // An open track list holds the controls up past the auto-hide delay.
        sleep(UInt32(5))
        XCTAssertTrue(playPauseButton.isHittable)
        XCTAssertTrue(audioButton.isSelected)

        audioButton.tap()
        XCTAssertFalse(audioButton.isSelected)

        let cinemaButton = app.buttons["player-cinema-button"]
        XCTAssertTrue(cinemaButton.isHittable)
        cinemaButton.tap()
        let enteredCinema = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "label == 'Leave Cinema'"), object: cinemaButton
        )
        XCTAssertEqual(XCTWaiter.wait(for: [enteredCinema], timeout: 10), .completed)
        audioButton.tap()
        XCTAssertTrue(audioButton.isSelected)
        attachScreenshot(named: "cinema-audio-tracks")
        sleep(2)
        audioButton.tap()
        cinemaButton.tap()
        let leftCinema = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "label == 'Watch in Cinema'"), object: cinemaButton
        )
        XCTAssertEqual(XCTWaiter.wait(for: [leftCinema], timeout: 10), .completed)

        app.buttons["player-done"].tap()
    }

    func testPackedStereoKeepsOurDoneControlOutsideAVKitAndReturnsToTheLibrary() throws {
        let app = XCUIApplication()
        app.launch()
        app.buttons["source-on-my-vision-pro"].tap()

        XCTAssertTrue(app.staticTexts["built-in-stereo-checks-title"].waitForExistence(timeout: 10))

        let startButton = app.buttons["play-builtin:stereo-check-sbs"]
        XCTAssertTrue(startButton.waitForExistence(timeout: 10))
        XCTAssertTrue(startButton.isHittable)
        startButton.tap()

        let done = app.buttons["player-done"]
        let controlsAppeared = done.waitForExistence(timeout: 30)
        attachScreenshot(named: "packed-stereo-controls-after-opening")
        XCTAssertTrue(controlsAppeared, app.debugDescription)
        XCTAssertTrue(waitForHittable(done, timeout: 10))
        XCTAssertTrue(app.buttons["player-surface"].exists)
        sleep(5)
        XCTAssertTrue(done.isHittable)
        done.tap()
        XCTAssertTrue(startButton.waitForExistence(timeout: 10))
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


}

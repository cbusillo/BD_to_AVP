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
        XCTAssertTrue(app.buttons["player-surface"].exists)
        XCTAssertFalse(app.buttons["player-show-controls"].exists)
        XCTAssertTrue(playPauseButton.isHittable)
        playPauseButton.tap()
        let pausedExpectation = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "label == 'Play movie'"),
            object: playPauseButton
        )
        XCTAssertEqual(XCTWaiter.wait(for: [pausedExpectation], timeout: 5), .completed)
    }

    func testTrackListsOpenInsideTheControlsAndHoldThemUp() throws {
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
        audioButton.tap()
        let audioTracks = app.descendants(matching: .any)["player-audio-tracks"]
        XCTAssertTrue(audioTracks.waitForExistence(timeout: 5))
        attachScreenshot(named: "player-audio-tracks")

        // An open track list holds the controls up past the auto-hide delay.
        sleep(UInt32(5))
        XCTAssertTrue(playPauseButton.exists)
        XCTAssertTrue(audioTracks.exists)

        let subtitleButton = app.buttons["player-subtitle-menu"]
        if subtitleButton.isEnabled {
            subtitleButton.tap()
            let subtitleTracks = app.descendants(matching: .any)["player-subtitle-tracks"]
            XCTAssertTrue(subtitleTracks.waitForExistence(timeout: 5))
            XCTAssertFalse(audioTracks.exists)
            attachScreenshot(named: "player-subtitle-tracks")
            subtitleButton.tap()
            XCTAssertFalse(subtitleTracks.exists)
        }

        app.buttons["player-done"].tap()
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

        guard firstHittableButton(labeled: "Eye Order: Normal", in: app, timeout: 30) != nil else {
            XCTFail(app.debugDescription)
            return
        }
        guard firstHittableButton(labeled: "Done", in: app, timeout: 5) != nil else {
            XCTFail(app.debugDescription)
            return
        }
        XCTAssertFalse(app.buttons["player-play-pause"].exists)
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

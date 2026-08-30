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

        attachScreenshot(named: "library-populated")
        movieTile.tap()

        let playButton = app.buttons["play-movie-\(movieID)"]
        XCTAssertTrue(playButton.waitForExistence(timeout: 10))
        attachScreenshot(named: "movie-details")
        playButton.tap()

        let playPauseButton = app.buttons["player-play-pause"]
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
    }

    private func attachScreenshot(named name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}

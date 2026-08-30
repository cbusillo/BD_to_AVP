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

        sleep(4)
        XCTAssertFalse(playPauseButton.exists)
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

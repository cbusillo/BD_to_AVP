import XCTest
@testable import BDToAVPPlayer

final class PlaybackPresentationTests: XCTestCase {
    func testTimeFormatterUsesMinutesAndSeconds() {
        XCTAssertEqual(PlaybackTimeFormatter.string(for: 65.9), "1:05")
    }

    func testTimeFormatterIncludesHoursWhenNeeded() {
        XCTAssertEqual(PlaybackTimeFormatter.string(for: 3_661), "1:01:01")
    }

    func testTimeFormatterHandlesInvalidValues() {
        XCTAssertEqual(PlaybackTimeFormatter.string(for: .nan), "0:00")
        XCTAssertEqual(PlaybackTimeFormatter.string(for: -1), "0:00")
    }

    func testSeekClampsToThePlayableRange() {
        XCTAssertEqual(PlaybackSeekPolicy.clampedTime(-2, duration: 120), 0)
        XCTAssertEqual(PlaybackSeekPolicy.clampedTime(200, duration: 120), 120)
        XCTAssertEqual(PlaybackSeekPolicy.clampedTime(40, duration: 120), 40)
    }

    func testSeekKeepsFinitePositionWhenDurationIsUnavailable() {
        XCTAssertEqual(PlaybackSeekPolicy.clampedTime(40, duration: .nan), 40)
    }

    func testPendingResumeIsClampedAndConsumedOnlyOnce() {
        var pendingResume = PlaybackPendingResumeState()

        pendingResume.store(140, duration: 120)

        XCTAssertEqual(pendingResume.value, 120)
        XCTAssertEqual(pendingResume.consume(), 120)
        XCTAssertNil(pendingResume.consume())
    }

    func testPendingResumeClearDiscardsStoredPosition() {
        var pendingResume = PlaybackPendingResumeState()
        pendingResume.store(42, duration: 120)

        pendingResume.clear()

        XCTAssertNil(pendingResume.value)
        XCTAssertNil(pendingResume.consume())
    }

    func testScrubStateKeepsLocalThumbValueUntilEditingEnds() {
        var scrubState = PlaybackScrubState()
        scrubState.begin(currentTime: 12)
        scrubState.update(requestedTime: 88, duration: 120)

        XCTAssertEqual(scrubState.value, 88)
        XCTAssertEqual(scrubState.finish(), 88)
        XCTAssertNil(scrubState.value)
    }

    func testScrubStateFinishesWithNoSeekWhenCancelled() {
        var scrubState = PlaybackScrubState()
        scrubState.begin(currentTime: 12)
        scrubState.cancel()

        XCTAssertNil(scrubState.finish())
    }

    func testHUDVisibilitySchedulesAutomaticHidingOnlyDuringUnfocusedPlayback() {
        var visibility = PlaybackHUDVisibilityState()

        visibility.reconcile(isPlaying: true)

        XCTAssertTrue(visibility.isVisible)
        XCTAssertTrue(visibility.isAutoHideScheduled)
        XCTAssertEqual(PlaybackHUDVisibilityState.autoHideDelay, 3)
    }

    func testHUDVisibilityHidesOnlyForTheCurrentScheduledTimer() {
        var visibility = PlaybackHUDVisibilityState()
        visibility.reconcile(isPlaying: true)
        let initialGeneration = visibility.autoHideGeneration

        visibility.reveal(isPlaying: true)
        let replacementGeneration = visibility.autoHideGeneration
        visibility.autoHideTimerFired(generation: initialGeneration)

        XCTAssertTrue(visibility.isVisible)
        XCTAssertTrue(visibility.isAutoHideScheduled)

        visibility.autoHideTimerFired(generation: replacementGeneration)

        XCTAssertFalse(visibility.isVisible)
        XCTAssertFalse(visibility.isAutoHideScheduled)
    }

    func testHUDVisibilityRemainsVisibleWhilePausedHoveredOrScrubbing() {
        var visibility = PlaybackHUDVisibilityState()

        visibility.reconcile(isPlaying: false)
        XCTAssertTrue(visibility.isVisible)
        XCTAssertFalse(visibility.isAutoHideScheduled)

        visibility.setHovered(true, isPlaying: true)
        XCTAssertTrue(visibility.isVisible)
        XCTAssertFalse(visibility.isAutoHideScheduled)

        visibility.setHovered(false, isPlaying: true)
        XCTAssertTrue(visibility.isAutoHideScheduled)

        visibility.setInteracting(true, isPlaying: true)
        XCTAssertTrue(visibility.isVisible)
        XCTAssertFalse(visibility.isAutoHideScheduled)

        visibility.setInteracting(false, isPlaying: true)
        XCTAssertTrue(visibility.isAutoHideScheduled)
    }

    func testResumePolicyWritesAnInProgressPosition() {
        XCTAssertEqual(ResumeWritePolicy.decision(currentTime: 42, duration: 120), .write(42))
    }

    func testResumePolicyRemovesCompletedPlayback() {
        XCTAssertEqual(ResumeWritePolicy.decision(currentTime: 117, duration: 120), .remove)
    }

    func testResumePolicySkipsInvalidPosition() {
        XCTAssertEqual(ResumeWritePolicy.decision(currentTime: .infinity, duration: 120), .skip)
    }

    @MainActor
    func testPrepareRejectsNonMVHEVCBeforeOpeningBookmark() async {
        let session = MVHEVCPlayerSession()
        let mediaItem = MediaItem(id: "sbs", title: "SBS", fileName: "sbs.mov", format: .sideBySide)
        let temporaryDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests-\(UUID().uuidString)", isDirectory: true)
        let bookmarkStore = BookmarkStore(storageURL: temporaryDirectory.appendingPathComponent("bookmarks.json"))
        let resumeStore = ResumeStore(storageURL: temporaryDirectory.appendingPathComponent("resume.json"))

        await session.prepare(mediaItem: mediaItem, bookmarkStore: bookmarkStore, resumeStore: resumeStore)

        XCTAssertEqual(session.state, .failed)
        XCTAssertEqual(
            session.failureMessage,
            "SBS playback is not supported here. Choose an MV-HEVC spatial video."
        )
    }

    @MainActor
    func testFinishResetsFailedPlaybackSession() async {
        let session = MVHEVCPlayerSession()
        let mediaItem = MediaItem(id: "sbs", title: "SBS", fileName: "sbs.mov", format: .sideBySide)
        let temporaryDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests-\(UUID().uuidString)", isDirectory: true)
        let bookmarkStore = BookmarkStore(storageURL: temporaryDirectory.appendingPathComponent("bookmarks.json"))
        let resumeStore = ResumeStore(storageURL: temporaryDirectory.appendingPathComponent("resume.json"))

        await session.prepare(mediaItem: mediaItem, bookmarkStore: bookmarkStore, resumeStore: resumeStore)
        session.finish()

        XCTAssertEqual(session.state, .idle)
        XCTAssertNil(session.mediaItem)
        XCTAssertNil(session.failureMessage)
        XCTAssertEqual(session.currentTime, 0)
        XCTAssertEqual(session.duration, 0)
    }
}

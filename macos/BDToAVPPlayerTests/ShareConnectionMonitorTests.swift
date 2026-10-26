import os
import XCTest
@testable import BDToAVPPlayer

final class ShareConnectionMonitorTests: XCTestCase {
    /// A clock the test moves by hand.
    private final class Clock: @unchecked Sendable {
        private let time = OSAllocatedUnfairLock(initialState: 100.0)
        var now: TimeInterval { time.withLock { $0 } }
        func advance(_ seconds: TimeInterval) { time.withLock { $0 += seconds } }
    }

    private struct ReadFailed: Error, Equatable {}

    private let fastRetry = ShareConnectionMonitor.Timing(lostAfter: 2, retryInterval: 0.01)

    func testAFailedReadIsTriedAgainUntilTheShareAnswers() throws {
        let monitor = ShareConnectionMonitor(timing: fastRetry)
        var attempts = 0

        let value = try monitor.read { () throws -> String in
            attempts += 1
            guard attempts > 3 else { throw ReadFailed() }
            return "bytes"
        }

        XCTAssertEqual(value, "bytes")
        XCTAssertEqual(attempts, 4)
        XCTAssertFalse(monitor.isConnectionLost)
    }

    func testAReadUnansweredPastTheLimitCountsAsLostUntilItFinishes() {
        let clock = Clock()
        let monitor = ShareConnectionMonitor(timing: fastRetry) { clock.now }

        let read = monitor.beginRead()
        clock.advance(fastRetry.lostAfter / 2)
        XCTAssertFalse(monitor.isConnectionLost)
        clock.advance(fastRetry.lostAfter)
        XCTAssertTrue(monitor.isConnectionLost)

        monitor.endRead(read)
        XCTAssertFalse(monitor.isConnectionLost)
    }

    func testRetriesOfADroppedReadCountTowardTheLimit() {
        let clock = Clock()
        let monitor = ShareConnectionMonitor(timing: fastRetry) { clock.now }
        var failures = 0

        // Each attempt fails half a second after the last, until the monitor reports the loss.
        let value = try? monitor.read { () throws -> Int in
            clock.advance(0.5)
            guard monitor.isConnectionLost else {
                failures += 1
                throw ReadFailed()
            }
            return 7
        }

        XCTAssertEqual(value, 7)
        XCTAssertEqual(Double(failures + 1) * 0.5, fastRetry.lostAfter)
        XCTAssertFalse(monitor.isConnectionLost)
    }

    func testCancellingStopsRetryingWithTheReadsError() {
        let monitor = ShareConnectionMonitor(timing: fastRetry)
        var attempts = 0

        XCTAssertThrowsError(
            try monitor.read { () throws -> Int in
                attempts += 1
                if attempts == 2 { monitor.cancel() }
                throw ReadFailed()
            }
        ) { error in
            XCTAssertEqual(error as? ReadFailed, ReadFailed())
        }
        XCTAssertEqual(attempts, 2)
        XCTAssertFalse(monitor.isConnectionLost)
    }

    /// Runs `body` through the monitor, cancelling it after `limit` attempts so a read that would
    /// retry forever ends the test instead of hanging it.
    private func attempts(
        _ monitor: ShareConnectionMonitor,
        limit: Int,
        while isWanted: @escaping () -> Bool = { true },
        _ body: @escaping (Int) throws -> Int
    ) -> (count: Int, result: Result<Int, Error>) {
        var count = 0
        let result = Result {
            try monitor.read(while: isWanted) { () throws -> Int in
                count += 1
                if count >= limit { monitor.cancel() }
                return try body(count)
            }
        }
        return (count, result)
    }

    func testErrorsAReconnectCannotCureFailAtOnce() {
        for error in [POSIXError(.EACCES), POSIXError(.ENOENT), CocoaError(.fileReadNoPermission)] as [Error] {
            let monitor = ShareConnectionMonitor(timing: fastRetry)

            let (count, result) = attempts(monitor, limit: 5) { _ in throw error }

            XCTAssertEqual(count, 1, "\(error)")
            XCTAssertThrowsError(try result.get())
        }
    }

    func testAShareThatKeepsFailingWithTheNetworkUpGivesUpWithAPlainMessage() {
        let clock = Clock()
        let timing = ShareConnectionMonitor.Timing(lostAfter: 2, retryInterval: 0.001, giveUpAfter: 10)
        let monitor = ShareConnectionMonitor(timing: timing) { clock.now }

        let (count, result) = attempts(monitor, limit: 500) { _ in
            clock.advance(1)
            throw POSIXError(.ESTALE)
        }

        XCTAssertLessThan(count, 500)
        XCTAssertThrowsError(try result.get()) { error in
            XCTAssertEqual(error as? ShareConnectionMonitor.ReadError, .stoppedAnswering)
            XCTAssertNotNil((error as? LocalizedError)?.errorDescription)
        }
    }

    func testTimeWithNoNetworkDoesNotCountTowardGivingUp() {
        let clock = Clock()
        let timing = ShareConnectionMonitor.Timing(lostAfter: 2, retryInterval: 0.001, giveUpAfter: 10)
        let monitor = ShareConnectionMonitor(timing: timing) { clock.now }
        monitor.setNetworkAvailable(false)

        // A hundred seconds with Wi-Fi off, then it comes back and the read works.
        let (count, result) = attempts(monitor, limit: 500) { attempt in
            clock.advance(1)
            if attempt == 100 { monitor.setNetworkAvailable(true) }
            guard attempt > 101 else { throw POSIXError(.ENOTCONN) }
            return attempt
        }

        XCTAssertEqual(try result.get(), 102)
        XCTAssertEqual(count, 102)
    }

    func testAReadNoLongerWantedStopsRetryingAndNoLongerCountsAsLost() {
        let clock = Clock()
        let monitor = ShareConnectionMonitor(timing: fastRetry) { clock.now }
        var isWanted = true

        let (count, result) = attempts(monitor, limit: 50, while: { isWanted }) { attempt in
            clock.advance(1)
            if attempt == 3 { isWanted = false }
            throw POSIXError(.ENOTCONN)
        }

        XCTAssertEqual(count, 3)
        XCTAssertThrowsError(try result.get())
        XCTAssertFalse(monitor.isCancelled)
        XCTAssertFalse(monitor.isConnectionLost)
    }

    func testNoNetworkCountsAsLostEvenWithNoReadOpen() {
        let monitor = ShareConnectionMonitor(timing: fastRetry)

        monitor.setNetworkAvailable(false)
        XCTAssertTrue(monitor.isConnectionLost)

        monitor.setNetworkAvailable(true)
        XCTAssertFalse(monitor.isConnectionLost)
    }

    @MainActor
    func testLeavingThePlayerStopsShareRetriesStartedWhileTheSourceOpens() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("ShareConnectionMonitorTests-\(UUID().uuidString)", isDirectory: true)
        let shareURL = URL(fileURLWithPath: "/Volumes/share/Movie.mkv")
        let bookmarkStore = BookmarkStore(
            storageURL: directory.appendingPathComponent("bookmarks.json"),
            resolveBookmark: { _ in (shareURL, false) },
            makeLease: { SecurityScopedResourceLease(url: $0, startAccessing: { true }, stopAccessing: {}) },
            resourceExists: { _ in true }
        )
        let item = MediaItem(id: "share-mkv", title: "Movie", fileName: "Movie.mkv", format: .bluray3D)
        try bookmarkStore.save(bookmarkData: Data([1]), for: item.id)
        let resumeStore = ResumeStore(storageURL: directory.appendingPathComponent("resume.json"))
        let reading = expectation(description: "opening reads the share")
        let stopped = expectation(description: "its retries stop")
        let session = MVHEVCPlayerSession()
        // Opening an MKV reads its header; here the share has dropped during that read.
        session.nativeSourceOpener = { _, reads in
            guard let reads else { throw Bluray3DSourceError.openFailed }
            reading.fulfill()
            defer { stopped.fulfill() }
            return try reads.read { () throws -> Bluray3DSource in throw POSIXError(.ENOTCONN) }
        }

        let preparing = Task { await session.prepare(mediaItem: item, bookmarkStore: bookmarkStore, resumeStore: resumeStore) }
        await fulfillment(of: [reading], timeout: 5)
        session.finish()

        await fulfillment(of: [stopped], timeout: 5)
        await preparing.value
        XCTAssertEqual(session.state, .idle)
        XCTAssertFalse(session.isConnectionLost)
    }

    func testOnlyMoviesOutsideTheAppAreWatched() throws {
        let home = URL(fileURLWithPath: NSHomeDirectory())
        let inside = home.appendingPathComponent("Documents/Movie.mov")
        let outside = URL(fileURLWithPath: "/private/var/mobile/Library/LiveFiles/share/Movie.mov")

        XCTAssertNil(ShareConnectionMonitor.forMovie(at: inside))
        XCTAssertNotNil(ShareConnectionMonitor.forMovie(at: outside))
        XCTAssertFalse(ShareConnectionMonitor.isInsideAppStorage(URL(fileURLWithPath: home.path + "-other/Movie.mov")))
    }
}

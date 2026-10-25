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

    func testNoNetworkCountsAsLostEvenWithNoReadOpen() {
        let monitor = ShareConnectionMonitor(timing: fastRetry)

        monitor.setNetworkAvailable(false)
        XCTAssertTrue(monitor.isConnectionLost)

        monitor.setNetworkAvailable(true)
        XCTAssertFalse(monitor.isConnectionLost)
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

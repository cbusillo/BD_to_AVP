import AVFoundation
import XCTest
@testable import BDToAVPPlayer

/// Plays the bundled side-by-side check movie through `ShareFileResourceLoader` with a real
/// `AVPlayer`, from a share that goes away mid-movie, as when Wi-Fi goes off.
final class ShareFileResourceLoaderTests: XCTestCase {
    /// Serves the movie a little faster than its average bitrate of about 53 KB/s, so the player
    /// never gets far ahead of the playhead, until `begin()`; then, until `end()`, every read
    /// either hangs (a stalled share) or fails (a dropped one).
    private final class Share: @unchecked Sendable {
        static let bytesPerRead = 16_384
        static let secondsPerRead = 0.25

        let data: Data
        let drops: Bool
        private let condition = NSCondition()
        private var isOut = false

        init(data: Data, drops: Bool) {
            self.data = data
            self.drops = drops
        }

        func begin() { condition.withLock { isOut = true } }

        func end() {
            condition.withLock {
                isOut = false
                condition.broadcast()
            }
        }

        func read(at offset: Int64, count: Int) throws -> Data {
            condition.lock()
            while isOut {
                if drops {
                    condition.unlock()
                    throw POSIXError(.ENOTCONN)
                }
                condition.wait()
            }
            condition.unlock()
            Thread.sleep(forTimeInterval: Self.secondsPerRead)
            let start = Int(offset)
            return data.subdata(in: start..<min(start + min(count, Self.bytesPerRead), data.count))
        }
    }

    private func waitUntil(_ timeout: TimeInterval, _ condition: () -> Bool) -> Bool {
        let deadline = Date(timeIntervalSinceNow: timeout)
        while Date() < deadline {
            if condition() { return true }
            RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.02))
        }
        return condition()
    }

    private func playThroughOutage(drops: Bool) throws {
        let url = try XCTUnwrap(Bundle.main.url(forResource: "Stereo-Check-SBS", withExtension: "mov"))
        let share = Share(data: try Data(contentsOf: url), drops: drops)
        let connection = ShareConnectionMonitor(timing: .init(lostAfter: 0.5, retryInterval: 0.05))
        let loader = ShareFileResourceLoader(
            fileName: url.lastPathComponent, byteCount: Int64(share.data.count), connection: connection
        ) { offset, count in
            try share.read(at: offset, count: count)
        }
        let player = AVPlayer(playerItem: AVPlayerItem(asset: loader.makeAsset()))
        player.isMuted = true
        addTeardownBlock {
            player.pause()
            share.end()
            loader.cancel()
        }

        player.play()
        XCTAssertTrue(
            waitUntil(40) { player.timeControlStatus == .playing && player.currentTime().seconds > 1 },
            "status \(player.timeControlStatus.rawValue) at \(player.currentTime().seconds) s, item \(String(describing: player.currentItem?.error))"
        )

        // Wi-Fi goes off: the player plays what it has, then waits with its clock stopped.
        share.begin()
        XCTAssertTrue(waitUntil(3) { connection.isConnectionLost })
        XCTAssertTrue(
            waitUntil(60) { PlayerTimeControl(player).isWaitingForData },
            "status \(player.timeControlStatus.rawValue) at \(player.currentTime().seconds) s"
        )
        XCTAssertTrue(PlayerTimeControl(player).wantsPlayback)
        let stoppedAt = player.currentTime().seconds
        _ = waitUntil(2) { false }
        XCTAssertEqual(player.currentTime().seconds, stoppedAt, accuracy: 0.1)

        // When it comes back, playback continues from the same place instead of jumping ahead.
        share.end()
        XCTAssertTrue(waitUntil(15) { player.timeControlStatus == .playing })
        XCTAssertEqual(player.currentTime().seconds, stoppedAt, accuracy: 0.5)
        XCTAssertTrue(waitUntil(3) { !connection.isConnectionLost })
        XCTAssertNil(player.currentItem?.error)
    }

    func testAStalledShareHoldsThePictureAndResumesWhereItStopped() throws {
        try playThroughOutage(drops: false)
    }

    func testADroppedShareHoldsThePictureAndResumesWhereItStopped() throws {
        try playThroughOutage(drops: true)
    }
}

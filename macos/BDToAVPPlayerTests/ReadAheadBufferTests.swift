import XCTest
@testable import BDToAVPPlayer

final class ReadAheadBufferTests: XCTestCase {
    private static let second: Int64 = 90_000
    /// One video unit per second keeps the arithmetic readable; every fourth is an IDR.
    private static func unit(_ second: Int64, origin: UInt64 = 0) -> Bluray3DPacket {
        .video(MVCAccessUnit(
            pts: (origin + UInt64(second * Self.second)) & ((1 << 33) - 1),
            dts: nil,
            isRandomAccess: second % 4 == 0,
            data: Data(count: 10)
        ))
    }

    private static func audio(_ pts: UInt64?, pid: UInt16 = 0x1100) -> Bluray3DPacket {
        .audio(BluRayPESPacket(pid: pid, streamID: 0xBD, streamIDExtension: nil, pts: pts, payload: Data(count: 4)))
    }

    private func take(_ buffer: ReadAheadBuffer) -> Int64? {
        guard case let .unit(entry) = buffer.nextVideo(timeout: 1) else { return nil }
        return entry.ticks
    }

    func testReaderWaitsWhenFarEnoughAheadAndContinuesAsTheDecoderTakesUnits() {
        let buffer = ReadAheadBuffer(limits: .init(aheadTarget: 3, behindLimit: 100, maximumBytes: 1 << 20))
        let delivered = OSAllocatedUnfairLockCounter()
        let reader = Thread {
            for second in 0..<10 {
                guard buffer.receive(Self.unit(Int64(second))) else { return }
                delivered.increment()
            }
            buffer.finish()
        }
        reader.start()
        // Units 0...3 fill three seconds ahead of the decoder, so the reader blocks inside unit 3.
        XCTAssertTrue(waitUntil { delivered.value == 3 })
        Thread.sleep(forTimeInterval: 0.1)
        XCTAssertEqual(delivered.value, 3)
        XCTAssertEqual(buffer.secondsAhead, 3)

        XCTAssertEqual(take(buffer), 0)
        XCTAssertTrue(waitUntil { delivered.value == 4 })
        var taken: [Int64] = [0]
        while case let .unit(entry) = buffer.nextVideo(timeout: 1) {
            taken.append(entry.ticks / Self.second)
        }
        XCTAssertEqual(taken, Array(0..<10))
        XCTAssertEqual(buffer.nextVideo(timeout: 0), .ended(error: nil))
    }

    func testTimestampsAreOnTheTitleTimelineFromZero() {
        let buffer = ReadAheadBuffer()
        XCTAssertTrue(buffer.receive(Self.audio(100)))
        XCTAssertTrue(buffer.receive(Self.audio(nil)))
        XCTAssertTrue(buffer.receive(Self.unit(1)))
        XCTAssertTrue(buffer.receive(Self.audio(45_000)))
        XCTAssertTrue(buffer.receive(Self.audio(180_000)))

        XCTAssertEqual(buffer.nextAudio(through: 45_000).map(\.ticks), [100, nil, 45_000])
        XCTAssertEqual(buffer.nextAudio(through: 100_000), [])
        XCTAssertEqual(buffer.nextAudio(through: 180_000).map(\.ticks), [180_000])
        XCTAssertEqual(take(buffer), Self.second)
    }

    func testRestartDropsTheOldReadAndHoldsTheDecoderUntilItsNewPass() {
        let buffer = ReadAheadBuffer()
        let oldFeed = buffer.feed()
        XCTAssertTrue(oldFeed.receive(Self.unit(0)))
        XCTAssertTrue(oldFeed.receive(Self.unit(1)))

        buffer.restart(at: 40 * Self.second)
        let newFeed = buffer.feed()

        // The read that was replaced is told to stop, and its finish is ignored.
        XCTAssertFalse(oldFeed.receive(Self.unit(2)))
        buffer.finish(feed: oldFeed)
        XCTAssertFalse(buffer.isFinished)
        // Audio from before the target, read from the random access unit before it, is dropped.
        XCTAssertTrue(newFeed.receive(Self.audio(UInt64(38 * Self.second))))
        XCTAssertTrue(newFeed.receive(Self.audio(nil)))
        XCTAssertTrue(newFeed.receive(Self.unit(36)))
        XCTAssertTrue(newFeed.receive(Self.audio(UInt64(40 * Self.second))))
        XCTAssertTrue(newFeed.receive(Self.unit(37)))
        XCTAssertEqual(buffer.nextVideo(timeout: 0.05), .waiting, "the decoder has not begun its new pass")
        XCTAssertEqual(buffer.nextAudio(through: 50 * Self.second), [])

        buffer.beginRestartedPass()
        XCTAssertEqual(take(buffer), 36 * Self.second)
        XCTAssertEqual(buffer.nextAudio(through: 50 * Self.second).map(\.ticks), [40 * Self.second])
        XCTAssertEqual(buffer.loadedTicks, (36 * Self.second)...(37 * Self.second))
    }

    func testSeekRestartsAtTheLastRandomAccessUnitAndRefusesTargetsOutsideTheLoadedPart() {
        let buffer = ReadAheadBuffer()
        for second in 0..<10 {
            XCTAssertTrue(buffer.receive(Self.unit(Int64(second))))
            XCTAssertTrue(buffer.receive(Self.audio(UInt64(Int64(second) * Self.second))))
        }
        for _ in 0..<6 { _ = take(buffer) }

        XCTAssertEqual(buffer.seek(to: 7 * Self.second + 10), .moved(startTicks: 4 * Self.second))
        XCTAssertEqual(take(buffer), 4 * Self.second)
        XCTAssertEqual(buffer.nextAudio(through: 7 * Self.second).compactMap(\.ticks), [7 * Self.second])

        XCTAssertEqual(buffer.seek(to: 2 * Self.second), .moved(startTicks: 0))
        XCTAssertEqual(buffer.seek(to: 12 * Self.second), .outside(loaded: 0...(9 * Self.second)))
    }

    func testHistoryBehindTheDecoderIsDroppedFromARandomAccessUnit() {
        let buffer = ReadAheadBuffer(limits: .init(aheadTarget: 100, behindLimit: 5, maximumBytes: 1 << 20))
        for second in 0..<20 {
            XCTAssertTrue(buffer.receive(Self.unit(Int64(second))))
        }
        for _ in 0..<15 { _ = take(buffer) }
        // The decoder is at 14 s; 5 s back is 9 s, and the IDR at or before that is 8 s.
        XCTAssertEqual(buffer.loadedTicks, (8 * Self.second)...(19 * Self.second))
        XCTAssertEqual(buffer.seek(to: 7 * Self.second), .outside(loaded: (8 * Self.second)...(19 * Self.second)))
    }

    func testAnAcceptedSeekKeepsItsUnitWhileTheDecoderMovesOn() {
        // Units 0...7 with IDRs at 0 and 4; history beyond 2 s behind the decoder is dropped.
        func filled() -> ReadAheadBuffer {
            let buffer = ReadAheadBuffer(limits: .init(aheadTarget: 100, behindLimit: 2, maximumBytes: 1 << 20))
            for second in 0..<12 {
                XCTAssertTrue(buffer.receive(Self.unit(Int64(second))))
            }
            for _ in 0..<5 { _ = take(buffer) }
            return buffer
        }
        // Without a reservation, the decoder taking two more units trims the IDR at 0 s, so a
        // seek checked before that can no longer be carried out.
        let unreserved = filled()
        XCTAssertEqual(unreserved.loadedTicks?.lowerBound, 0)
        _ = take(unreserved)
        _ = take(unreserved)
        XCTAssertEqual(unreserved.seek(to: 1 * Self.second), .outside(loaded: (4 * Self.second)...(11 * Self.second)))

        // A reserved seek keeps that IDR until it is carried out.
        let reserved = filled()
        XCTAssertEqual(reserved.reserveSeek(to: 1 * Self.second), .moved(startTicks: 0))
        _ = take(reserved)
        _ = take(reserved)
        XCTAssertEqual(reserved.loadedTicks?.lowerBound, 0)
        XCTAssertEqual(reserved.seek(to: 1 * Self.second), .moved(startTicks: 0))
        XCTAssertEqual(take(reserved), 0)
    }

    func testByteLimitDropsHistoryBeforeMakingTheReaderWait() {
        // Ten bytes per unit and a 100-byte limit: ten units fit.
        let buffer = ReadAheadBuffer(limits: .init(aheadTarget: 100, behindLimit: 100, maximumBytes: 100))
        for second in 0..<9 {
            XCTAssertTrue(buffer.receive(Self.unit(Int64(second))))
        }
        for _ in 0..<6 { _ = take(buffer) }
        // Unit 9 reaches the limit; history before the decoder's last IDR (4 s) goes instead.
        XCTAssertTrue(buffer.receive(Self.unit(9)))
        XCTAssertEqual(buffer.loadedTicks, (4 * Self.second)...(9 * Self.second))
    }

    func testTimestampsKeepCountingAcrossThe33BitWrap() {
        // A timeline that starts two seconds before the 33-bit counter wraps.
        let origin = (UInt64(1) << 33) - UInt64(2 * Self.second)
        let buffer = ReadAheadBuffer()
        for second in 0..<5 {
            XCTAssertTrue(buffer.receive(Self.unit(Int64(second), origin: origin)))
        }
        XCTAssertEqual(buffer.loadedTicks, (-2 * Self.second)...(2 * Self.second))
    }

    func testCancelUnblocksTheReaderAndTheDecoder() {
        let buffer = ReadAheadBuffer(limits: .init(aheadTarget: 1, behindLimit: 1, maximumBytes: 1 << 20))
        XCTAssertTrue(buffer.receive(Self.unit(0)))
        let stopped = expectation(description: "reader stopped")
        Thread {
            if !buffer.receive(Self.unit(1)) { stopped.fulfill() }
        }.start()
        Thread.sleep(forTimeInterval: 0.05)
        buffer.cancel()
        wait(for: [stopped], timeout: 1)
        XCTAssertEqual(buffer.nextVideo(timeout: 0), .ended(error: nil))
    }

    func testFinishErrorIsReportedAfterTheLastUnit() {
        let buffer = ReadAheadBuffer()
        XCTAssertTrue(buffer.receive(Self.unit(0)))
        buffer.finish(error: "The source stopped responding while reading.")
        XCTAssertEqual(take(buffer), 0)
        XCTAssertEqual(buffer.nextVideo(timeout: 0), .ended(error: "The source stopped responding while reading."))
    }

    private func waitUntil(timeout: TimeInterval = 2, _ condition: () -> Bool) -> Bool {
        let deadline = Date(timeIntervalSinceNow: timeout)
        while Date() < deadline {
            if condition() { return true }
            Thread.sleep(forTimeInterval: 0.005)
        }
        return condition()
    }
}

private final class OSAllocatedUnfairLockCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    var value: Int { lock.withLock { count } }
    func increment() { lock.withLock { count += 1 } }
}

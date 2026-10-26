import Foundation

/// Holds a Blu-ray 3D title's packets between the source reader and the decoders, so short
/// stalls in a Files or SMB read do not reach the screen. Measured SMB reads of the Rainforest
/// ISO averaged 11.9 MB/s with a slowest second of 0.81 MB/s against about 5 MB/s needed (#1110),
/// so the reader runs up to `aheadTarget` seconds past the decoder.
///
/// The reader thread calls `receive`, which blocks while the buffer is full. The decode thread
/// takes video with `nextVideo` and the audio feeder takes audio with `nextAudio`. Packets behind
/// the decoder are kept for `behindLimit` seconds, so a seek inside that history or the loaded
/// part ahead needs no new read. A seek anywhere else starts a new read with `restart(at:)`.
/// Thread-safe.
///
/// Timestamps map onto the title's timeline, zero at the title's start: a disc reader delivers
/// playlist time and a Matroska reader the file's own timestamps, so a read that starts mid-title
/// lands on the same clock.
final class ReadAheadBuffer: Bluray3DPacketSink, @unchecked Sendable {
    struct VideoEntry: Equatable {
        let ticks: Int64
        let isRandomAccess: Bool
        let data: Data
    }

    struct AudioEntry: Equatable {
        /// Nil for a packet without a PTS; it follows the packet before it.
        let ticks: Int64?
        let packet: BluRayPESPacket
    }

    enum VideoResult: Equatable {
        case unit(VideoEntry)
        /// Nothing ready yet; the reader is still going.
        case waiting
        /// The reader finished and every unit was taken. `error` says why it stopped early.
        case ended(error: String?)
    }

    struct Limits: Equatable {
        /// Seconds of video the reader may get ahead of the decoder.
        var aheadTarget: Double = 15
        /// Seconds of video kept behind the decoder for seeking back.
        var behindLimit: Double = 20
        /// Bytes held in all; history is dropped first, then the reader waits.
        var maximumBytes = 256 << 20
    }

    let limits: Limits
    let presentationTimeline = BluRayPresentationTimeline(originPTS: 0)

    private let condition = NSCondition()
    // Guarded by `condition`.
    private var video: [VideoEntry] = []
    private var audio: [AudioEntry] = []
    private var videoCursor = 0
    private var audioCursor = 0
    private var bytes = 0
    private var latestVideoTicks: Int64?
    private var lastAudioTicks: Int64 = 0
    private var finished = false
    private var finishError: String?
    /// The random access unit an accepted seek starts at; history from it on is kept until
    /// the seek is carried out.
    private var reservedSeekTicks: Int64?
    private var cancelled = false
    /// Bumped by `restart(at:)`; packets from an older read are refused.
    private var epoch = 0
    /// Audio timed before this is dropped: a restarted read begins at a random access unit up to
    /// a few seconds before its target, and that audio is never played.
    private var audioFloor: Int64?
    /// Set by `restart(at:)` until the decoder starts its new pass, so it cannot take a unit of
    /// the new read while it is still on the old one.
    private var awaitingDecoder = false

    init(limits: Limits = Limits()) {
        self.limits = limits
    }

    // MARK: Reader side

    func receive(_ packet: Bluray3DPacket) -> Bool {
        receive(packet, epoch: nil)
    }

    /// A sink for one read: it stops taking packets once `restart(at:)` starts another.
    func feed() -> Bluray3DPacketSink {
        condition.lock()
        defer { condition.unlock() }
        return Feed(buffer: self, epoch: epoch)
    }

    private final class Feed: Bluray3DPacketSink {
        let buffer: ReadAheadBuffer
        let epoch: Int
        init(buffer: ReadAheadBuffer, epoch: Int) {
            self.buffer = buffer
            self.epoch = epoch
        }

        func receive(_ packet: Bluray3DPacket) -> Bool { buffer.receive(packet, epoch: epoch) }
    }

    private func receive(_ packet: Bluray3DPacket, epoch packetEpoch: Int?) -> Bool {
        condition.lock()
        defer { condition.unlock() }
        guard !cancelled, packetEpoch.map({ $0 == epoch }) ?? true else { return false }
        switch packet {
        case let .video(unit):
            let ticks = presentationTimeline.ticks(for: unit.pts, near: latestVideoTicks ?? audioFloor ?? 0)
            video.append(VideoEntry(ticks: ticks, isRandomAccess: unit.isRandomAccess, data: unit.data))
            bytes += unit.data.count
            latestVideoTicks = max(latestVideoTicks ?? ticks, ticks)
        case let .audio(packet):
            appendAudio(packet)
        case .subtitle:
            return true
        }
        condition.broadcast()
        while !cancelled, packetEpoch.map({ $0 == epoch }) ?? true, isFull {
            trimHistory(force: true)
            guard isFull else { break }
            condition.wait()
        }
        return !cancelled && (packetEpoch.map { $0 == epoch } ?? true)
    }

    /// Drops everything for a read that starts at `ticks`, and refuses packets from the read
    /// before. The decoder takes nothing until it calls `beginRestartedPass()`.
    func restart(at ticks: Int64) {
        condition.lock()
        epoch += 1
        video.removeAll()
        audio.removeAll()
        videoCursor = 0
        audioCursor = 0
        bytes = 0
        latestVideoTicks = nil
        lastAudioTicks = ticks
        finished = false
        finishError = nil
        reservedSeekTicks = nil
        audioFloor = ticks - Self.audioPacketSlackTicks
        awaitingDecoder = true
        condition.broadcast()
        condition.unlock()
    }

    /// The decoder is ready for the read `restart(at:)` started.
    func beginRestartedPass() {
        condition.lock()
        awaitingDecoder = false
        condition.broadcast()
        condition.unlock()
    }

    /// The reader stopped: at the end of the title, or with an error. A finish from a read that
    /// `restart(at:)` replaced is ignored.
    func finish(error: String? = nil, feed: Bluray3DPacketSink? = nil) {
        condition.lock()
        if let feed = feed as? Feed, feed.epoch != epoch {
            condition.unlock()
            return
        }
        finished = true
        finishError = error
        condition.broadcast()
        condition.unlock()
    }

    /// Unblocks every waiter; the reader stops at its next packet.
    func cancel() {
        condition.lock()
        cancelled = true
        condition.broadcast()
        condition.unlock()
    }

    // MARK: Decoder side

    /// The next video unit in decode order, waiting up to `timeout` seconds for one.
    func nextVideo(timeout: TimeInterval) -> VideoResult {
        condition.lock()
        defer { condition.unlock() }
        let deadline = Date(timeIntervalSinceNow: timeout)
        while !cancelled, awaitingDecoder || videoCursor >= video.count && !finished {
            if !condition.wait(until: deadline) { return .waiting }
        }
        guard !cancelled else { return .ended(error: nil) }
        guard videoCursor < video.count else { return .ended(error: finishError) }
        let entry = video[videoCursor]
        videoCursor += 1
        trimHistory(force: false)
        condition.broadcast()
        return .unit(entry)
    }

    /// Audio packets, in stream order, up to the first one timed after `ticks`.
    func nextAudio(through ticks: Int64) -> [AudioEntry] {
        condition.lock()
        defer { condition.unlock() }
        var taken: [AudioEntry] = []
        guard !awaitingDecoder else { return taken }
        while audioCursor < audio.count, (audio[audioCursor].ticks ?? .min) <= ticks {
            taken.append(audio[audioCursor])
            audioCursor += 1
        }
        return taken
    }

    /// Seconds of video read past the decoder.
    var secondsAhead: Double {
        condition.lock()
        defer { condition.unlock() }
        return secondsAheadLocked
    }

    /// Whether the reader has finished, so no more data is coming.
    var isFinished: Bool {
        condition.lock()
        defer { condition.unlock() }
        return finished
    }

    /// The span of the title the buffer can play without reading again, in timeline ticks.
    var loadedTicks: ClosedRange<Int64>? {
        condition.lock()
        defer { condition.unlock() }
        return loadedTicksLocked
    }

    enum SeekResult: Equatable {
        /// Decoding restarts at the random access unit timed `startTicks`; frames before the
        /// target are decoded but not shown.
        case moved(startTicks: Int64)
        /// The target is outside the loaded part.
        case outside(loaded: ClosedRange<Int64>?)
    }

    /// Accepts a seek to `ticks` without moving yet: the random access unit it starts at, and
    /// everything after it, stay until `seek(to:)` carries it out, however the decoder moves.
    func reserveSeek(to ticks: Int64) -> SeekResult {
        condition.lock()
        defer { condition.unlock() }
        guard let start = seekStart(ticks) else { return .outside(loaded: loadedTicksLocked) }
        reservedSeekTicks = video[start].ticks
        return .moved(startTicks: video[start].ticks)
    }

    /// Moves both cursors so decoding restarts at the last random access unit at or before
    /// `ticks`, and audio at the target, and releases a reservation.
    func seek(to ticks: Int64) -> SeekResult {
        condition.lock()
        defer { condition.unlock() }
        reservedSeekTicks = nil
        guard let start = seekStart(ticks) else { return .outside(loaded: loadedTicksLocked) }
        videoCursor = start
        audioCursor = audio.firstIndex { ($0.ticks ?? .min) >= ticks } ?? audio.count
        // Back up to include the packet that covers the target.
        while audioCursor > 0, (audio[audioCursor - 1].ticks ?? .max) > ticks - Self.audioPacketSlackTicks {
            audioCursor -= 1
        }
        condition.broadcast()
        return .moved(startTicks: video[start].ticks)
    }

    /// Audio packets on Blu-ray hold at most a few frames; this much before a seek target is
    /// enough to cover it.
    private static let audioPacketSlackTicks: Int64 = 9_000

    // MARK: Internals

    private func seekStart(_ ticks: Int64) -> Int? {
        guard let first = video.first?.ticks, let latestVideoTicks, ticks >= first, ticks <= latestVideoTicks else {
            return nil
        }
        return video.lastIndex { $0.isRandomAccess && $0.ticks <= ticks }
    }

    private var loadedTicksLocked: ClosedRange<Int64>? {
        guard let first = video.first?.ticks, let latestVideoTicks else { return nil }
        return min(first, latestVideoTicks)...latestVideoTicks
    }

    private var secondsAheadLocked: Double {
        guard let latestVideoTicks else { return 0 }
        let next = videoCursor < video.count ? video[videoCursor].ticks : latestVideoTicks
        return Double(latestVideoTicks - next) / Double(BluRayPresentationTimeline.ticksPerSecond)
    }

    private var isFull: Bool {
        secondsAheadLocked >= limits.aheadTarget || bytes >= limits.maximumBytes
    }

    private func appendAudio(_ packet: BluRayPESPacket) {
        let ticks = packet.pts.map { presentationTimeline.ticks(for: $0, near: lastAudioTicks) }
        if let ticks { lastAudioTicks = ticks }
        // An untimed packet continues the one before it, so it follows that packet's fate.
        let dropped = ticks.map { $0 < (audioFloor ?? .min) } ?? (audio.isEmpty && audioFloor != nil)
        guard !dropped else { return }
        audio.append(AudioEntry(ticks: ticks, packet: packet))
        bytes += packet.payload.count
    }

    /// Drops history behind the decoder: older than `behindLimit`, or, when `force` and the
    /// buffer is over its byte limit, everything before the decoder's last random access unit.
    private func trimHistory(force: Bool) {
        guard videoCursor > 0 else { return }
        let current = video[min(videoCursor, video.count) - 1].ticks
        let cutoff = current - Int64(limits.behindLimit * Double(BluRayPresentationTimeline.ticksPerSecond))
        // Keep from a random access unit so a seek back can always decode.
        var keep = video[..<videoCursor].lastIndex { $0.isRandomAccess && $0.ticks <= cutoff } ?? 0
        if force, bytes >= limits.maximumBytes {
            keep = video[..<videoCursor].lastIndex(where: \.isRandomAccess) ?? keep
        }
        if let reservedSeekTicks, let reserved = video.firstIndex(where: { $0.isRandomAccess && $0.ticks == reservedSeekTicks }) {
            keep = min(keep, reserved)
        }
        guard keep > 0 else { return }
        bytes -= video[..<keep].reduce(0) { $0 + $1.data.count }
        video.removeFirst(keep)
        videoCursor -= keep
        let firstTicks = video.first?.ticks ?? current
        let audioKeep = min(audioCursor, audio.firstIndex { ($0.ticks ?? .max) >= firstTicks - Self.audioPacketSlackTicks } ?? audio.count)
        if audioKeep > 0 {
            bytes -= audio[..<audioKeep].reduce(0) { $0 + $1.packet.payload.count }
            audio.removeFirst(audioKeep)
            audioCursor -= audioKeep
        }
    }
}

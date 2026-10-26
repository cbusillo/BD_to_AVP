import AVFoundation
import CoreMedia
import Edge264MVC
import Foundation
import os

/// Plays one Blu-ray 3D title on the headset: the source reader fills a `ReadAheadBuffer`,
/// edge264 decodes the MVC video into tagged stereo sample buffers for
/// `AVSampleBufferVideoRenderer`, and `HeadsetAudioPlayer` plays the chosen audio track. Both
/// renderers join one `AVSampleBufferRenderSynchronizer`, whose time is the title's timeline
/// (zero at the first video PTS), so pause, buffering and seeking move audio and video together.
///
/// Three threads do the work: the reader, the video decoder, and an audio feeder that hands the
/// audio player packets a little ahead of the clock. The clock only runs while the wearer wants
/// playback and enough is buffered; when the buffer runs dry it stops and `isBuffering` is set.
final class NativeBlurayPlayback: @unchecked Sendable {
    struct Configuration: Equatable {
        var buffer = ReadAheadBuffer.Limits()
        /// Seconds buffered ahead before the clock first starts, and again after a stall.
        var startSeconds: Double = 2
        var resumeSeconds: Double = 4
        /// The clock stops when less than this much video is queued in the renderer.
        var stallSeconds: Double = 0.15
        /// How far video is decoded ahead of the clock.
        var videoLeadSeconds: Double = 0.5
        /// How far audio packets go to the audio player ahead of the clock.
        var audioLeadSeconds: Double = 2.5
        /// Frames queued before the clock starts, unless the renderer stops taking frames sooner:
        /// with its clock stopped it takes six in RealityKit (#1103) and two in a display layer.
        var prerollFrames = 4
        var decoderThreads: Int32 = 0
    }

    struct Status: Equatable {
        var currentTime: TimeInterval = 0
        var wantsPlayback = false
        var isBuffering = true
        /// What can be played without reading again.
        var loadedRange: ClosedRange<TimeInterval>?
        var hasEnded = false
        var failure: String?
        /// Why the chosen audio is not playing while the video goes on, from the audio player.
        var audioFailure: String?
        var statistics = Statistics()
    }

    struct Statistics: Equatable {
        var framesQueued: UInt64 = 0
        /// Frames that reached the renderer after their time; the renderer drops them.
        var lateFrames: UInt64 = 0
        var framesWithoutDependentView: UInt64 = 0
        var corruptUnits: UInt64 = 0
        var stalls = 0
        var secondsAhead: Double = 0
    }

    /// Takes frames only once RealityKit's `VideoPlayerComponent` (or another video target)
    /// shows it, so decoding waits for the player view.
    let renderer: AVSampleBufferVideoRenderer
    let synchronizer = AVSampleBufferRenderSynchronizer()
    let title: Bluray3DTitle
    let audioTracks: [HeadsetAudioTrack]
    /// Called just before the clock's rate changes, with the new rate. Tests set it before
    /// `start()` to act at that moment.
    var willChangeClockRate: (@Sendable (Float) -> Void)?

    private let source: Bluray3DSource
    private let configuration: Configuration
    private let buffer: ReadAheadBuffer
    private let state: OSAllocatedUnfairLock<SharedState>
    /// Held for every clock change, from the state it depends on to the synchronizer write, so
    /// a pause, seek or stop can never be undone by a rate or start time computed before it.
    /// Taken before `state`, never inside it.
    private let clockLock = NSLock()
    /// Held while the feeder hands audio over and while a seek moves the audio cursor, so no
    /// packet from before a seek reaches the audio player after its flush.
    private let audioFeed = NSLock()
    /// Held for each read of the source, so a read started by a seek waits for the one before it
    /// to stop.
    private let readLock = NSLock()
    /// Where playback starts on the title's timeline.
    private let startTicks: Int64
    private static let frameTicks = Int64(
        StereoSampleBufferFactory.defaultFrameDuration.seconds * Double(BluRayPresentationTimeline.ticksPerSecond)
    )

    private struct SharedState {
        var status = Status()
        var started = false
        var cancelled = false
        /// Bumped by every seek; the decode thread restarts when it changes.
        var seekGeneration = 0
        var seekTarget: Int64?
        /// Whether the current seek started a new read rather than moving inside the buffer.
        var seekRestartedRead = false
        var audioPlayer: HeadsetAudioPlayer?
        var selectedAudioTrackID: UInt16?
        var clockStarted = false
        /// Where the clock starts when the preroll is ready: the title start or a seek target.
        var clockOrigin: Int64 = 0
        var lastQueuedEnd: Int64?
        var videoEnded = false
    }

    init(
        source: Bluray3DSource,
        title: Bluray3DTitle,
        initialAudioTrackID: UInt16? = nil,
        startSeconds: TimeInterval = 0,
        configuration: Configuration = Configuration(),
        renderer: AVSampleBufferVideoRenderer = AVSampleBufferVideoRenderer(),
        locale: Locale = .current
    ) {
        self.renderer = renderer
        self.source = source
        self.title = title
        self.configuration = configuration
        buffer = ReadAheadBuffer(limits: configuration.buffer)
        audioTracks = HeadsetAudioTrack.tracks(for: title.audioStreams, locale: locale)
        let initial = audioTracks.first { $0.id == initialAudioTrackID } ?? HeadsetAudioTrack.preferredTrack(in: audioTracks)
        let start = Self.ticks(min(max(0, startSeconds), title.durationSeconds))
        startTicks = start
        state = OSAllocatedUnfairLock(initialState: SharedState(selectedAudioTrackID: initial?.id, clockOrigin: start))
        synchronizer.addRenderer(renderer)
        synchronizer.setRate(0, time: Self.time(start))
    }

    deinit {
        stop()
    }

    var status: Status {
        var status = state.withLock { $0.status }
        status.currentTime = max(0, Self.seconds(clockTicks()))
        status.loadedRange = buffer.loadedTicks.map { Self.seconds($0.lowerBound)...Self.seconds($0.upperBound) }
        status.statistics.secondsAhead = buffer.secondsAhead
        status.audioFailure = audioPlayer?.failure
        return status
    }

    var selectedAudioTrackID: UInt16? { state.withLock { $0.selectedAudioTrackID } }

    /// The audio player, once the audio feeder has started; for diagnostics.
    var audioPlayer: HeadsetAudioPlayer? { state.withLock { $0.audioPlayer } }

    /// Starts reading and decoding. Playback begins once the first seconds are buffered.
    func start() {
        let shouldStart = state.withLock { shared -> Bool in
            guard !shared.started else { return false }
            shared.started = true
            shared.status.wantsPlayback = true
            return true
        }
        guard shouldStart else { return }
        buffer.restart(at: startTicks)
        let feed = buffer.feed()
        spawn("reader", qos: .userInitiated) { [self] in readSource(from: startTicks, into: feed) }
        spawn("video", qos: .userInteractive) { [self] in decodeVideo() }
        spawn("audio", qos: .userInteractive) { [self] in feedAudio() }
    }

    /// Stops playback and flushes the renderers. The threads end on their own without blocking
    /// the caller: a reader inside a slow share read finishes that read first.
    func stop() {
        clockLock.withLock {
            state.withLock { $0.cancelled = true }
            synchronizer.rate = 0
        }
        buffer.cancel()
        renderer.flush()
        state.withLock { $0.audioPlayer }?.discontinuity()
    }

    func setPlaying(_ playing: Bool) {
        clockLock.withLock {
            state.withLock { shared in
                shared.status.wantsPlayback = playing && !shared.status.hasEnded
            }
            applyRateLocked()
        }
    }

    /// Seeks to `seconds`. A target inside what is loaded restarts decoding there; any other
    /// starts a new read at the last random access unit before it, which the disc's entry-point
    /// map or the Matroska cues locate. Accepting an in-buffer seek reserves its random access
    /// unit, so the video thread can always carry it out even if history is trimmed in between.
    func seek(to seconds: TimeInterval) {
        let ticks = Self.ticks(min(max(0, seconds), title.durationSeconds))
        clockLock.withLock {
            let restartsRead: Bool
            if case .outside = buffer.reserveSeek(to: ticks) {
                buffer.restart(at: ticks)
                restartsRead = true
            } else {
                restartsRead = false
            }
            state.withLock { shared in
                shared.seekGeneration += 1
                shared.seekTarget = ticks
                shared.seekRestartedRead = restartsRead
                shared.clockStarted = false
                shared.clockOrigin = ticks
                shared.lastQueuedEnd = nil
                shared.videoEnded = false
                shared.status.hasEnded = false
                shared.status.isBuffering = true
            }
            synchronizer.setRate(0, time: Self.time(ticks))
            if restartsRead {
                let feed = buffer.feed()
                spawn("reader", qos: .userInitiated) { [self] in readSource(from: ticks, into: feed) }
            }
        }
    }

    func selectAudio(trackID: UInt16) {
        let player = state.withLock { shared -> HeadsetAudioPlayer? in
            shared.selectedAudioTrackID = trackID
            return shared.audioPlayer
        }
        player?.select(trackID: trackID)
    }

    // MARK: Reader

    /// Reads the title from `ticks` into one feed of the buffer. Reads run one at a time: one
    /// started by a seek waits here while the read it replaced finishes its current packet.
    private func readSource(from ticks: Int64, into feed: Bluray3DPacketSink) {
        readLock.lock()
        defer { readLock.unlock() }
        guard !isCancelled else { return }
        do {
            _ = try source.read(
                title: title,
                from: ticks,
                audioTrackIDs: audioTracks.map { Int($0.id) },
                subtitleTrackIDs: [],
                into: feed
            )
            buffer.finish(feed: feed)
        } catch {
            buffer.finish(error: (error as? LocalizedError)?.errorDescription ?? "\(error)", feed: feed)
        }
    }

    // MARK: Video

    private func decodeVideo() {
        let decoder: MVCDecoder
        do {
            decoder = try MVCDecoder(threads: configuration.decoderThreads)
        } catch {
            fail("The 3D video decoder could not start.")
            return
        }
        let factory = StereoSampleBufferFactory(baseViewIsRightEye: title.baseViewIsRightEye)
        var generation = 0
        // A start mid-title shows frames from the start time on.
        var skipBefore: Int64? = startTicks > 0 ? startTicks : nil
        buffer.beginRestartedPass()
        while !isCancelled {
            let (currentGeneration, target, restartedRead) = state.withLock {
                ($0.seekGeneration, $0.seekTarget, $0.seekRestartedRead)
            }
            if currentGeneration != generation {
                generation = currentGeneration
                decoder.flush()
                renderer.flush()
                audioFeed.lock()
                var moved = true
                // Also after an in-buffer seek that replaced a restart the decoder never began.
                buffer.beginRestartedPass()
                if restartedRead {
                    skipBefore = target
                } else if let target {
                    if case .moved = buffer.seek(to: target) { skipBefore = target } else { moved = false }
                }
                state.withLock { $0.audioPlayer }?.discontinuity()
                audioFeed.unlock()
                // The reservation made when the seek was accepted keeps its unit; losing it
                // would leave the clock at the target with nothing to show, so say so.
                guard moved else {
                    fail("The seek could not be completed.")
                    return
                }
            }
            switch buffer.nextVideo(timeout: 0.05) {
            case let .unit(entry):
                do {
                    try decoder.decode(entry.data, ticks: entry.ticks) { frame in
                        present(frame, factory: factory, generation: generation, skipBefore: &skipBefore)
                    }
                } catch {
                    fail("The 3D video could not be decoded: \(error).")
                    return
                }
                record(decoder.statistics)
            case .waiting:
                updateClock()
            case let .ended(error):
                _ = try? decoder.finish { frame in
                    present(frame, factory: factory, generation: generation, skipBefore: &skipBefore)
                }
                record(decoder.statistics)
                state.withLock { $0.videoEnded = true }
                playOut(generation: generation, error: error)
            }
        }
    }

    /// Returns false to stop decoding: on cancel, or when a seek replaced this pass.
    private func present(
        _ frame: UnsafePointer<BDAVPMVCFrame>,
        factory: StereoSampleBufferFactory,
        generation: Int,
        skipBefore: inout Int64?
    ) -> Bool {
        let ticks = frame.pointee.pts
        if let skip = skipBefore {
            guard ticks >= skip else { return isCurrent(generation) }
            skipBefore = nil
        }
        let sample: CMSampleBuffer
        do {
            sample = try factory.sampleBuffer(for: frame, presentationTime: Self.time(ticks))
        } catch {
            fail("A 3D video frame could not be prepared: \(error).")
            return false
        }
        let leadTicks = Int64(configuration.videoLeadSeconds * Double(BluRayPresentationTimeline.ticksPerSecond))
        while isCurrent(generation) {
            updateClock()
            let clockStarted = state.withLock { $0.clockStarted }
            if renderer.status == .failed {
                fail("The video renderer stopped: \(renderer.error.map { "\($0)" } ?? "unknown error").")
                return false
            }
            if renderer.isReadyForMoreMediaData, !clockStarted || ticks - clockTicks() <= leadTicks {
                break
            }
            usleep(2_000)
        }
        guard isCurrent(generation) else { return false }
        let late = state.withLock { $0.clockStarted } && ticks < clockTicks()
        renderer.enqueue(sample)
        state.withLock { shared in
            shared.status.statistics.framesQueued += 1
            if late { shared.status.statistics.lateFrames += 1 }
            shared.lastQueuedEnd = max(shared.lastQueuedEnd ?? 0, ticks + Self.frameTicks)
        }
        updateClock()
        return isCurrent(generation)
    }

    /// After the last frame: wait for the clock to pass it, then report the end or the error.
    private func playOut(generation: Int, error: String?) {
        while isCurrent(generation) {
            updateClock()
            let end = state.withLock { $0.lastQueuedEnd } ?? 0
            if clockTicks() >= end || !state.withLock({ $0.clockStarted }) && end == 0 {
                clockLock.withLock {
                    state.withLock { shared in
                        if let error {
                            shared.status.failure = "Playback stopped: \(error)"
                        }
                        shared.status.hasEnded = true
                        shared.status.wantsPlayback = false
                    }
                    applyRateLocked()
                }
                break
            }
            usleep(20_000)
        }
        // Stay until a seek or stop.
        while isCurrent(generation) {
            usleep(20_000)
        }
    }

    // MARK: Audio

    private func feedAudio() {
        let leadTicks = Int64(configuration.audioLeadSeconds * Double(BluRayPresentationTimeline.ticksPerSecond))
        while !isCancelled {
            if state.withLock({ $0.audioPlayer }) == nil, !audioTracks.isEmpty {
                let player = HeadsetAudioPlayer(
                    streams: audioTracks.map(\.stream),
                    timeline: buffer.presentationTimeline,
                    synchronizer: synchronizer,
                    initialTrackID: selectedAudioTrackID
                )
                state.withLock { $0.audioPlayer = player }
            }
            if let player = state.withLock({ $0.audioPlayer }) {
                audioFeed.lock()
                let anchor = state.withLock { $0.clockStarted ? nil : $0.clockOrigin } ?? clockTicks()
                for entry in buffer.nextAudio(through: anchor + leadTicks) {
                    player.receive(entry.packet)
                }
                audioFeed.unlock()
            }
            usleep(20_000)
        }
    }

    // MARK: Clock

    /// Starts the clock once the preroll is queued and enough is buffered, and stops it when the
    /// renderer is about to run dry.
    private func updateClock() {
        let secondsAhead = buffer.secondsAhead
        let finished = buffer.isFinished
        let rendererIsFull = !renderer.isReadyForMoreMediaData
        clockLock.lock()
        defer { clockLock.unlock() }
        let now = clockTicks()
        let decision = state.withLock { shared -> (start: Int64?, changed: Bool) in
            let queuedSeconds = Self.seconds((shared.lastQueuedEnd ?? shared.clockOrigin) - now)
            let enoughToStart = finished || shared.videoEnded || secondsAhead >= configuration.startSeconds
            if !shared.clockStarted {
                let queued = shared.lastQueuedEnd.map { $0 - shared.clockOrigin } ?? 0
                let prerolled = queued >= Int64(configuration.prerollFrames) * Self.frameTicks
                    || queued > 0 && rendererIsFull
                guard prerolled || shared.videoEnded, enoughToStart else { return (nil, false) }
                shared.clockStarted = true
                shared.status.isBuffering = false
                return (shared.clockOrigin, true)
            }
            if !shared.status.isBuffering, shared.status.wantsPlayback, !finished, !shared.videoEnded,
               queuedSeconds < configuration.stallSeconds, secondsAhead < configuration.stallSeconds {
                shared.status.isBuffering = true
                shared.status.statistics.stalls += 1
                return (nil, true)
            }
            if shared.status.isBuffering, finished || shared.videoEnded || secondsAhead >= configuration.resumeSeconds {
                shared.status.isBuffering = false
                return (nil, true)
            }
            return (nil, false)
        }
        if let origin = decision.start {
            synchronizer.setRate(0, time: Self.time(origin))
        }
        if decision.changed {
            applyRateLocked()
        }
    }

    /// Sets the clock's rate from the current state. Call with `clockLock` held.
    private func applyRateLocked() {
        let rate: Float = state.withLock { shared in
            shared.clockStarted && shared.status.wantsPlayback && !shared.status.isBuffering && !shared.cancelled ? 1 : 0
        }
        if synchronizer.rate != rate {
            willChangeClockRate?(rate)
            synchronizer.rate = rate
        }
    }

    // MARK: Helpers

    private var isCancelled: Bool { state.withLock { $0.cancelled } }

    private func isCurrent(_ generation: Int) -> Bool {
        state.withLock { !$0.cancelled && $0.seekGeneration == generation }
    }

    private func record(_ statistics: BDAVPMVCStatistics) {
        state.withLock { shared in
            shared.status.statistics.framesWithoutDependentView = statistics.frames_without_dependent
            shared.status.statistics.corruptUnits = statistics.corrupt_units
        }
    }

    private func fail(_ message: String) {
        clockLock.withLock {
            state.withLock { shared in
                shared.status.failure = message
                shared.status.wantsPlayback = false
                shared.cancelled = true
            }
            synchronizer.rate = 0
        }
        buffer.cancel()
    }

    private func clockTicks() -> Int64 {
        let time = synchronizer.currentTime()
        guard time.isNumeric else { return 0 }
        return Int64((time.seconds * Double(BluRayPresentationTimeline.ticksPerSecond)).rounded())
    }

    private func spawn(_ name: String, qos: QualityOfService, _ body: @escaping @Sendable () -> Void) {
        let thread = Thread(block: body)
        thread.name = "com.shinycomputers.bd-to-avp.native-\(name)"
        thread.qualityOfService = qos
        thread.stackSize = 8 << 20
        thread.start()
    }

    static func ticks(_ seconds: TimeInterval) -> Int64 {
        Int64((seconds * Double(BluRayPresentationTimeline.ticksPerSecond)).rounded())
    }

    static func seconds(_ ticks: Int64) -> TimeInterval {
        Double(ticks) / Double(BluRayPresentationTimeline.ticksPerSecond)
    }

    static func time(_ ticks: Int64) -> CMTime {
        CMTime(value: ticks, timescale: CMTimeScale(BluRayPresentationTimeline.ticksPerSecond))
    }
}

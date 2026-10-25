import AVFoundation
import CoreMedia
import Foundation
import os

/// One entry in the headset audio list.
struct HeadsetAudioTrack: Identifiable, Equatable, Sendable {
    var id: UInt16 { stream.pid }
    let stream: BluRayAudioStreamDescriptor
    let playback: HeadsetAudioPlayback
    /// What plays, for example "English (DTS core)".
    let label: String

    var mediaOption: PlaybackMediaOption {
        PlaybackMediaOption(id: "pid-\(id)", displayName: label)
    }

    /// The tracks the headset can play, in disc order, with labels that name the decoded format.
    /// Tracks that would decode the same way and share a language get the usual track-number
    /// detail from `PlaybackAudioOptionLabelPolicy`.
    static func tracks(for streams: [BluRayAudioStreamDescriptor], locale: Locale = .current) -> [HeadsetAudioTrack] {
        let playable = streams.compactMap { stream in
            HeadsetAudioPlayback(coding: stream.coding).map { (stream, $0) }
        }
        let baseNames = playable.map { stream, playback in
            "\(languageName(stream.languageCode, locale: locale)) (\(playback.formatName))"
        }
        let labels = PlaybackAudioOptionLabelPolicy.labels(
            for: baseNames.enumerated().map { index, name in
                PlaybackAudioOptionLabelMetadata(baseName: name, role: nil, index: index)
            }
        )
        return zip(playable, labels).map { entry, label in
            HeadsetAudioTrack(stream: entry.0, playback: entry.1, label: label)
        }
    }

    static func languageName(_ code: String?, locale: Locale) -> String {
        guard let code = code?.trimmingCharacters(in: .whitespaces), !code.isEmpty, code.lowercased() != "und" else {
            return "Unknown language"
        }
        if let name = locale.localizedString(forLanguageCode: code), name.lowercased() != code.lowercased() {
            return name.prefix(1).uppercased() + name.dropFirst()
        }
        return code.uppercased()
    }

    /// The first track in a preferred language, or the first track.
    static func preferredTrack(in tracks: [HeadsetAudioTrack], preferredLanguages: [String] = Locale.preferredLanguages) -> HeadsetAudioTrack? {
        for language in preferredLanguages {
            let wanted = Locale(identifier: language).language.languageCode
            if let match = tracks.first(where: { track in
                track.stream.languageCode.map { Locale.LanguageCode($0) == wanted || Locale(identifier: $0).language.languageCode == wanted } ?? false
            }) {
                return match
            }
        }
        return tracks.first
    }
}

/// Recent PES packets of every audio PID, kept so a track switch can start the new track at the
/// current playback time instead of waiting for the reader to come back around.
struct AudioPacketHistory {
    struct Entry {
        let ticks: Int64?
        let packet: BluRayPESPacket
    }

    private(set) var entries: [UInt16: [Entry]] = [:]

    mutating func append(_ packet: BluRayPESPacket, ticks: Int64?) {
        entries[packet.pid, default: []].append(Entry(ticks: ticks, packet: packet))
    }

    /// Drops packets that start before `ticks`, keeping the last timed packet before it so its
    /// frames can still cover `ticks`.
    mutating func trim(before ticks: Int64) {
        for pid in entries.keys {
            guard var list = entries[pid] else {
                continue
            }
            if let keep = list.lastIndex(where: { ($0.ticks ?? .max) <= ticks }), keep > 0 {
                list.removeFirst(keep)
            }
            entries[pid] = list
        }
    }

    /// Packets of `pid` from the last timed packet at or before `ticks` onward.
    func packets(for pid: UInt16, from ticks: Int64) -> [Entry] {
        guard let list = entries[pid] else {
            return []
        }
        let start = list.lastIndex(where: { ($0.ticks ?? .max) <= ticks }) ?? list.firstIndex(where: { $0.ticks != nil }) ?? list.endIndex
        return Array(list[start...])
    }

    mutating func removeAll() {
        entries.removeAll()
    }
}

/// Plays one Blu-ray audio track through AVSampleBufferAudioRenderer, on the render
/// synchronizer that the native video renderer (#1111) also joins.
///
/// Feed it every audio PES packet in stream order through `BluRayPESPacketSink`. It decodes
/// the selected track and keeps a few seconds of the others, so `select(trackID:)` switches
/// without restarting playback.
final class HeadsetAudioPlayer: BluRayPESPacketSink, @unchecked Sendable {
    struct EnqueuedRange: Equatable {
        let pid: UInt16
        let startTicks: Int64
        let endTicks: Int64
    }

    /// How far past the playhead a switch takes effect, so the old track's flush lands before
    /// the renderer reaches it.
    static let switchLeadTicks: Int64 = 9_000 // 100 ms
    /// How much decoded audio the player wants queued ahead of the playhead.
    static let targetLeadTicks: Int64 = 180_000 // 2 s
    /// How long other tracks' packets are kept behind the playhead.
    static let historyTicks: Int64 = 90_000

    let renderer = AVSampleBufferAudioRenderer()
    let tracks: [HeadsetAudioTrack]
    let timeline: BluRayPresentationTimeline

    private let synchronizer: AVSampleBufferRenderSynchronizer
    private let queue = DispatchQueue(label: "com.shinycomputers.bd-to-avp.player.headset-audio")
    private let state = OSAllocatedUnfairLock(initialState: SharedState())
    private let audioPIDs: Set<UInt16>

    // Queue-confined.
    private var selected: HeadsetAudioTrack?
    private var decoder: BluRayAudioTrackDecoder?
    private var factory = PCMSampleBufferFactory()
    private var history = AudioPacketHistory()
    private var pending: [(range: EnqueuedRange, buffer: CMSampleBuffer)] = []
    private var lastTicks: Int64 = 0
    private var awaitingFlush = false
    private var generation = 0

    private struct SharedState {
        var selectedTrackID: UInt16?
        var queuedEndTicks: Int64?
        var enqueued: [EnqueuedRange] = []
        var lastError: String?
    }

    init(
        streams: [BluRayAudioStreamDescriptor],
        timeline: BluRayPresentationTimeline,
        synchronizer: AVSampleBufferRenderSynchronizer,
        initialTrackID: UInt16? = nil,
        locale: Locale = .current
    ) {
        tracks = HeadsetAudioTrack.tracks(for: streams, locale: locale)
        audioPIDs = Set(tracks.map(\.id))
        self.timeline = timeline
        self.synchronizer = synchronizer
        synchronizer.addRenderer(renderer)
        let initial = tracks.first { $0.id == initialTrackID } ?? HeadsetAudioTrack.preferredTrack(in: tracks)
        queue.sync {
            install(initial)
        }
        renderer.requestMediaDataWhenReady(on: queue) { [weak self] in
            self?.drain()
        }
    }

    deinit {
        renderer.stopRequestingMediaData()
    }

    var selectedTrackID: UInt16? { state.withLock { $0.selectedTrackID } }
    var lastError: String? { state.withLock { $0.lastError } }

    /// The ranges handed to the renderer, oldest first, for diagnostics and tests.
    var enqueuedRanges: [EnqueuedRange] { state.withLock { $0.enqueued } }

    /// Seconds of decoded audio queued past the playhead. The reader can pause while this is
    /// above `targetLeadTicks`.
    var queuedLeadSeconds: Double {
        guard let end = state.withLock({ $0.queuedEndTicks }) else {
            return 0
        }
        return max(0, Double(end - clockTicks()) / Double(BluRayPresentationTimeline.ticksPerSecond))
    }

    var wantsMoreData: Bool {
        queuedLeadSeconds * Double(BluRayPresentationTimeline.ticksPerSecond) < Double(Self.targetLeadTicks)
    }

    func receive(_ packet: BluRayPESPacket) {
        guard audioPIDs.contains(packet.pid) else {
            return
        }
        queue.async { [self] in
            let ticks = packet.pts.map { timeline.ticks(for: $0, near: lastTicks) }
            if let ticks {
                lastTicks = ticks
            }
            history.append(packet, ticks: ticks)
            history.trim(before: clockTicks() - Self.historyTicks)
            if packet.pid == selected?.id {
                decodeAndQueue(packet, ticks: ticks, notBefore: nil)
            }
            drain()
        }
    }

    func discontinuity() {
        queue.async { [self] in
            generation += 1
            renderer.flush()
            history.removeAll()
            pending.removeAll()
            decoder?.reset()
            awaitingFlush = false
            state.withLock { $0.queuedEndTicks = nil }
        }
    }

    /// Switches to another track at the current playback time, without stopping the clock.
    func select(trackID: UInt16) {
        queue.async { [self] in
            guard let track = tracks.first(where: { $0.id == trackID }), track.id != selected?.id else {
                return
            }
            generation += 1
            let switchGeneration = generation
            install(track)
            pending.removeAll()
            let boundary = clockTicks() + Self.switchLeadTicks
            for entry in history.packets(for: track.id, from: boundary) {
                decodeAndQueue(entry.packet, ticks: entry.ticks, notBefore: boundary)
            }
            let flushTicks = pending.first?.range.startTicks ?? boundary
            awaitingFlush = true
            renderer.flush(fromSourceTime: CMTime(value: flushTicks, timescale: CMTimeScale(BluRayPresentationTimeline.ticksPerSecond))) { [weak self] succeeded in
                guard let self else {
                    return
                }
                queue.async {
                    guard switchGeneration == self.generation else {
                        return
                    }
                    if !succeeded {
                        // The playhead already passed the flush point: start the new track now.
                        self.renderer.flush()
                        let now = self.clockTicks()
                        self.pending.removeAll { $0.range.endTicks <= now }
                    }
                    self.awaitingFlush = false
                    self.drain()
                }
            }
        }
    }

    /// Ticks of the shared clock on the presentation timeline.
    private func clockTicks() -> Int64 {
        let time = synchronizer.currentTime()
        guard time.isNumeric else {
            return 0
        }
        return Int64((time.seconds * Double(BluRayPresentationTimeline.ticksPerSecond)).rounded())
    }

    private func install(_ track: HeadsetAudioTrack?) {
        selected = track
        factory = PCMSampleBufferFactory()
        do {
            decoder = try track?.playback.makeDecoder()
        } catch {
            decoder = nil
            record(error: "\(error)")
        }
        state.withLock {
            $0.selectedTrackID = track?.id
            $0.queuedEndTicks = nil
        }
    }

    private func decodeAndQueue(_ packet: BluRayPESPacket, ticks: Int64?, notBefore: Int64?) {
        guard let decoder else {
            return
        }
        do {
            for chunk in try decoder.decode(packet, ticks: ticks) {
                if let notBefore, chunk.startTicks < notBefore {
                    continue
                }
                let range = EnqueuedRange(pid: packet.pid, startTicks: chunk.startTicks, endTicks: chunk.endTicks)
                pending.append((range, try factory.sampleBuffer(for: chunk)))
            }
        } catch {
            record(error: "\(error)")
        }
    }

    private func drain() {
        guard !awaitingFlush else {
            return
        }
        var enqueued: [EnqueuedRange] = []
        while !pending.isEmpty, renderer.isReadyForMoreMediaData {
            let next = pending.removeFirst()
            renderer.enqueue(next.buffer)
            enqueued.append(next.range)
        }
        guard !enqueued.isEmpty else {
            return
        }
        let ranges = enqueued
        state.withLock { shared in
            shared.enqueued.append(contentsOf: ranges)
            if shared.enqueued.count > 4_096 {
                shared.enqueued.removeFirst(shared.enqueued.count - 4_096)
            }
            shared.queuedEndTicks = ranges.last?.endTicks
        }
        if renderer.status == .failed {
            record(error: renderer.error.map { "\($0)" } ?? "renderer failed")
        }
    }

    private func record(error: String) {
        state.withLock { $0.lastError = error }
    }
}

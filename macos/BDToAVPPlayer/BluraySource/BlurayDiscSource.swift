import Foundation

/// A Blu-ray 3D ISO image or disc folder, read through libbluray.
final class BlurayDiscSource: Bluray3DSource {
    let info: Bluray3DSourceInfo

    private let disc: OpaquePointer
    private let lock = NSLock()
    private let reads: ShareConnectionMonitor?

    /// With `reads`, a failed stream read waits and tries again instead of ending playback.
    init(path: String, container: Bluray3DSourceInfo.Container, reads: ShareConnectionMonitor? = nil) throws {
        self.reads = reads
        guard let disc = bd3d_disc_open(path) else { throw Bluray3DSourceError.openFailed }
        let discInfo = bd3d_disc_get_info(disc)
        let discFacts = BlurayDiscFacts(
            isStereo3D: discInfo.content_3d,
            isEncrypted: discInfo.aacs_detected || discInfo.bdplus_detected
        )
        var facts: [BlurayTitleFacts] = []
        for index in 0..<discInfo.title_count {
            var title = bd3d_title()
            if bd3d_disc_title(disc, index, &title) {
                facts.append(Self.facts(title))
            }
        }
        self.disc = disc
        info = Bluray3DSourceInfo(container: container, titles: BlurayTitleSelection.ordered(facts, disc: discFacts))
    }

    deinit {
        bd3d_disc_close(disc)
    }

    /// Every timestamp is on the playlist timeline, whatever clip it is in.
    func read(
        title: Bluray3DTitle,
        from startTicks: Int64,
        audioTrackIDs: [Int],
        subtitleTrackIDs: [Int],
        into sink: Bluray3DPacketSink
    ) throws -> Bluray3DReadStatistics {
        if let reason = title.unsupportedReason { throw Bluray3DSourceError.unsupported(reason) }
        guard let playlist = title.playlist, info.titles.contains(where: { $0.id == title.id }) else {
            throw Bluray3DSourceError.titleUnavailable
        }
        lock.lock()
        defer { lock.unlock() }
        let audio = audioTrackIDs.map { UInt16(clamping: $0) }
        let subtitles = subtitleTrackIDs.map { UInt16(clamping: $0) }
        return try BlurayDemuxCallback.run(sink: sink, reads: reads) { emit, context, monitor, monitorContext, stats in
            bd3d_disc_stream(
                disc, playlist, UInt64(max(0, startTicks)), monitor, monitorContext, audio, audio.count, subtitles,
                subtitles.count, emit, context, stats
            )
        }
    }

    private static func facts(_ title: bd3d_title) -> BlurayTitleFacts {
        var title = title
        let audio = withUnsafeBytes(of: &title.audio) { streams(Array($0.bindMemory(to: bd3d_stream.self)), count: title.audio_count) }
        let subtitles = withUnsafeBytes(of: &title.subtitles) {
            streams(Array($0.bindMemory(to: bd3d_stream.self)), count: title.subtitle_count)
        }
        return BlurayTitleFacts(
            playlist: title.playlist,
            durationTicks: title.duration_ticks,
            angleCount: title.angle_count,
            isMainFeature: title.main_feature,
            clipsReadable: title.clips_readable,
            hasStereoStream: title.ssif_available,
            baseViewIsRightEye: title.base_view_right,
            audio: audio,
            subtitles: subtitles
        )
    }

    private static func streams(_ streams: [bd3d_stream], count: UInt8) -> [BlurayTitleFacts.Stream] {
        streams.prefix(Int(count)).map { stream in
            var stream = stream
            let language = withUnsafeBytes(of: &stream.language) { String(decoding: $0.prefix { $0 != 0 }, as: UTF8.self) }
            return BlurayTitleFacts.Stream(pid: stream.pid, codingType: stream.coding_type, language: language)
        }
    }
}

/// Bridges the C demuxer's packet callback to a `Bluray3DPacketSink`.
enum BlurayDemuxCallback {
    private final class Context {
        let sink: Bluray3DPacketSink
        init(sink: Bluray3DPacketSink) { self.sink = sink }
    }

    /// The demuxer's reads as `ShareConnectionMonitor` sees them; one reader thread at a time.
    private final class ReadWatch {
        let monitor: ShareConnectionMonitor
        let sink: Bluray3DPacketSink
        var readID: Int?
        var retry = ShareConnectionMonitor.Retry()
        init(monitor: ShareConnectionMonitor, sink: Bluray3DPacketSink) {
            self.monitor = monitor
            self.sink = sink
        }
    }

    typealias Demux = (
        _ emit: bd3d_packet_fn,
        _ context: UnsafeMutableRawPointer,
        _ monitor: bd3d_read_monitor_fn?,
        _ monitorContext: UnsafeMutableRawPointer?,
        _ stats: UnsafeMutablePointer<bd3d_stats>
    ) -> bd3d_result

    static func run(sink: Bluray3DPacketSink, reads: ShareConnectionMonitor? = nil, demux: Demux) throws -> Bluray3DReadStatistics {
        let context = Context(sink: sink)
        let watch = reads.map { ReadWatch(monitor: $0, sink: sink) }
        var stats = bd3d_stats()
        let result = withExtendedLifetime((context, watch)) {
            demux(
                emit,
                Unmanaged.passUnretained(context).toOpaque(),
                watch == nil ? nil : watchRead,
                watch.map { Unmanaged.passUnretained($0).toOpaque() },
                &stats
            )
        }
        let statistics = Bluray3DReadStatistics(
            bytes: stats.bytes,
            videoAccessUnits: stats.video_pairs,
            audioPackets: stats.audio_packets,
            subtitlePackets: stats.subtitle_packets
        )
        switch result {
        case BD3D_OK, BD3D_STOPPED:
            return statistics
        case BD3D_READ_FAILED:
            throw Bluray3DSourceError.readFailed
        case BD3D_SSIF_UNAVAILABLE:
            throw Bluray3DSourceError.unsupported(.stereoStreamMissing)
        case BD3D_TITLE_UNAVAILABLE:
            throw Bluray3DSourceError.titleUnavailable
        default:
            throw Bluray3DSourceError.invalidStream(description(result))
        }
    }

    private static let watchRead: bd3d_read_monitor_fn = { context, event, error in
        guard let context else { return 0 }
        let watch = Unmanaged<ReadWatch>.fromOpaque(context).takeUnretainedValue()
        switch event {
        case BD3D_READ_EVENT_STARTED:
            watch.readID = watch.monitor.beginRead()
            watch.retry = ShareConnectionMonitor.Retry()
        case BD3D_READ_EVENT_FAILED:
            let code = POSIXErrorCode(rawValue: error) ?? .EIO
            // A read that a seek replaced stops instead of waiting for the share.
            guard watch.sink.isWanted, watch.monitor.waitToRetry(after: POSIXError(code), &watch.retry) else { return 0 }
            return watch.sink.isWanted ? 1 : 0
        default:
            if let id = watch.readID { watch.monitor.endRead(id) }
            watch.readID = nil
        }
        return 0
    }

    private static let emit: bd3d_packet_fn = { context, packet in
        guard let context, let packet else { return 1 }
        let sink = Unmanaged<Context>.fromOpaque(context).takeUnretainedValue().sink
        let value = packet.pointee
        // A clip start carries no data.
        guard value.kind != BD3D_PACKET_CLIP_START else { return sink.receive(.clipStart(pts: value.pts)) ? 0 : 1 }
        let packetData = Data(bytes: value.data, count: value.length)
        let delivered: Bluray3DPacket
        if value.kind == BD3D_PACKET_VIDEO {
            var data = packetData
            data.append(value.dependent, count: value.dependent_length)
            delivered = .video(
                MVCAccessUnit(pts: value.pts, dts: value.dts, isRandomAccess: value.random_access, data: data)
            )
        } else {
            // The audio player's own PES parser, so headers are read one way for every source.
            guard let pes = TransportStreamPESReader.parse(pid: value.pid, packetData) else { return 0 }
            delivered = value.kind == BD3D_PACKET_AUDIO ? .audio(pes) : .subtitle(pes)
        }
        return sink.receive(delivered) ? 0 : 1
    }

    static func description(_ result: bd3d_result) -> String {
        switch result {
        case BD3D_INVALID_PACKET: return "invalid transport packet"
        case BD3D_INVALID_PES: return "invalid PES header"
        case BD3D_OUT_OF_MEMORY: return "out of memory"
        case BD3D_PENDING_LIMIT: return "too much unpaired video"
        case BD3D_NON_MONOTONIC: return "video decode times go backward"
        case BD3D_UNMATCHED_PES: return "a base view has no dependent view"
        case BD3D_NO_MVC: return "no MVC video"
        case BD3D_TOO_MANY_PIDS: return "too many tracks selected"
        case BD3D_INDEX_UNAVAILABLE: return "the clip's entry-point map could not be read"
        default: return "demux error \(result.rawValue)"
        }
    }
}

/// Demuxes Blu-ray M2TS bytes held in memory, as the disc reader does for an SSIF file.
enum BlurayTransportStream {
    private final class Reader {
        let data: Data
        let chunkSize: Int
        var offset = 0
        var reads = 0
        var failingReads: (after: Int, count: Int, error: Int32)?
        init(data: Data, chunkSize: Int) {
            self.data = data
            self.chunkSize = chunkSize
        }
    }

    /// `chunkSize` sets how many bytes each read returns, so reads can split packets. With a
    /// `segment`, the bytes are one playlist item's part of a clip, delivered on the playlist
    /// timeline as the disc reader delivers them. `failingReads` says how many reads fail, and
    /// with which errno, as a dropped share's do, starting with the one at that position in the
    /// sequence; `reads` decides whether they are tried again.
    static func demux(
        _ data: Data,
        segment: bd3d_segment? = nil,
        audioTrackIDs: [Int] = [],
        subtitleTrackIDs: [Int] = [],
        chunkSize: Int = 6144,
        failingReads: (after: Int, count: Int, error: Int32)? = nil,
        reads: ShareConnectionMonitor? = nil,
        into sink: Bluray3DPacketSink
    ) throws -> Bluray3DReadStatistics {
        let reader = Reader(data: data, chunkSize: max(1, chunkSize))
        reader.failingReads = failingReads
        let audio = audioTrackIDs.map { UInt16(clamping: $0) }
        let subtitles = subtitleTrackIDs.map { UInt16(clamping: $0) }
        let read: bd3d_read_fn = { context, buffer, size in
            guard let context, let buffer else { return -1 }
            let reader = Unmanaged<Reader>.fromOpaque(context).takeUnretainedValue()
            defer { reader.reads += 1 }
            if let failing = reader.failingReads, reader.reads >= failing.after, reader.reads < failing.after + failing.count {
                errno = failing.error
                return -1
            }
            let count = min(Int(size), reader.chunkSize, reader.data.count - reader.offset)
            reader.data.copyBytes(to: buffer, from: reader.offset..<(reader.offset + count))
            reader.offset += count
            return Int64(count)
        }
        return try withExtendedLifetime(reader) {
            try BlurayDemuxCallback.run(sink: sink, reads: reads) { emit, context, monitor, monitorContext, stats in
                let readContext = Unmanaged.passUnretained(reader).toOpaque()
                guard var segment else {
                    return bd3d_demux(
                        read, readContext, monitor, monitorContext, audio, audio.count, subtitles, subtitles.count, emit,
                        context, stats
                    )
                }
                return bd3d_demux_segment(
                    read, readContext, &segment, monitor, monitorContext, audio, audio.count, subtitles, subtitles.count,
                    emit, context, stats
                )
            }
        }
    }
}

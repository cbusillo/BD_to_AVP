import Foundation

/// The video track's coding, as the file or decoder reports it.
struct PlaybackVideoDetails: Equatable {
    var codec: String
    /// One eye's picture size, or nil before it is known.
    var width: Int?
    var height: Int?
    var frameRate: Double?
    var bitsPerSecond: Double?
}

/// Frame and stall counts for the movie that is playing; nil where its player does not report one.
struct PlaybackFrameCounters: Equatable {
    var shown: UInt64?
    var late: UInt64?
    var missingRightEye: UInt64?
    var damaged: UInt64?
    var stalls: Int?
}

/// What "Stats for Nerds" shows about the movie that is playing, read about once a second.
struct PlaybackStatsSample: Equatable {
    var title: String
    var fileName: String
    var format: StereoFormat
    var fileByteCount: Int64?
    var isOnShare: Bool
    /// Bytes read from the share since the movie opened; nil for a movie on the headset.
    var shareBytesRead: UInt64?
    var isConnectionLost: Bool
    var currentTime: TimeInterval
    var duration: TimeInterval
    var loadedThrough: TimeInterval?
    var video: PlaybackVideoDetails?
    var audioTrack: String?
    var subtitleTrack: String?
    var frames = PlaybackFrameCounters()
}

/// Turns the share's running byte count into a read speed over the last few seconds.
struct PlaybackThroughputMeter {
    static let window: TimeInterval = 3

    private var samples: [(uptime: TimeInterval, bytes: UInt64)] = []

    /// Bytes per second across the window, or nil until two samples a moment apart exist.
    mutating func record(bytes: UInt64, at uptime: TimeInterval) -> Double? {
        // A smaller count means a new movie or a reopened share.
        if let last = samples.last, bytes < last.bytes || uptime < last.uptime {
            samples.removeAll()
        }
        samples.append((uptime, bytes))
        // Keeps the newest sample at least a window old as the starting point, so reads that
        // stop show as zero rather than disappearing.
        while samples.count > 2, uptime - samples[1].uptime >= Self.window {
            samples.removeFirst()
        }
        guard let first = samples.first, uptime - first.uptime >= 0.5 else {
            return nil
        }
        return Double(bytes - first.bytes) / (uptime - first.uptime)
    }
}

struct PlaybackStatsRow: Equatable, Identifiable {
    let label: String
    let value: String
    var id: String { label }
}

struct PlaybackStatsSection: Equatable, Identifiable {
    let title: String
    let rows: [PlaybackStatsRow]
    var id: String { title }
}

enum PlaybackStatsPresentation {
    static func sections(for sample: PlaybackStatsSample, readBytesPerSecond: Double?) -> [PlaybackStatsSection] {
        var sections: [PlaybackStatsSection] = []

        var movie = [
            PlaybackStatsRow(label: "Title", value: sample.title),
            PlaybackStatsRow(label: "File", value: sample.fileName),
        ]
        if let size = sample.fileByteCount {
            movie.append(PlaybackStatsRow(label: "File size", value: byteCount(size)))
            if sample.duration > 0 {
                movie.append(PlaybackStatsRow(
                    label: "Average bit rate",
                    value: bitRate(Double(size) * 8 / sample.duration)
                ))
            }
        }
        movie.append(PlaybackStatsRow(label: "Stored", value: sample.isOnShare ? "Network share" : "On this Vision Pro"))
        movie.append(PlaybackStatsRow(label: "3D format", value: sample.format.displayName))
        sections.append(PlaybackStatsSection(title: "Movie", rows: movie))

        var video: [PlaybackStatsRow] = []
        if let details = sample.video {
            video.append(PlaybackStatsRow(label: "Codec", value: details.codec))
            if let width = details.width, let height = details.height, width > 0, height > 0 {
                video.append(PlaybackStatsRow(label: "Picture per eye", value: "\(width) × \(height)"))
            }
            if let frameRate = details.frameRate, frameRate > 0 {
                video.append(PlaybackStatsRow(label: "Frame rate", value: String(format: "%.3f fps", frameRate)))
            }
            if let bits = details.bitsPerSecond, bits > 0 {
                video.append(PlaybackStatsRow(label: "Video bit rate", value: bitRate(bits)))
            }
        }
        if !video.isEmpty {
            sections.append(PlaybackStatsSection(title: "Video", rows: video))
        }

        sections.append(PlaybackStatsSection(title: "Audio and Subtitles", rows: [
            PlaybackStatsRow(label: "Audio", value: sample.audioTrack ?? "None"),
            PlaybackStatsRow(label: "Subtitles", value: sample.subtitleTrack ?? "Off"),
        ]))

        var playback = [
            PlaybackStatsRow(
                label: "Position",
                value: "\(PlaybackTimeFormatter.string(for: sample.currentTime)) / \(PlaybackTimeFormatter.string(for: sample.duration))"
            ),
        ]
        if let loadedThrough = sample.loadedThrough {
            let ahead = max(0, loadedThrough - sample.currentTime)
            playback.append(PlaybackStatsRow(label: "Loaded ahead", value: String(format: "%.1f s", ahead)))
        }
        let frames = sample.frames
        func count(_ label: String, _ value: UInt64?) {
            if let value {
                playback.append(PlaybackStatsRow(label: label, value: value.formatted()))
            }
        }
        count("Frames shown", frames.shown)
        count("Late frames", frames.late)
        count("Frames missing the right eye", frames.missingRightEye)
        count("Damaged video units", frames.damaged)
        if let stalls = frames.stalls {
            playback.append(PlaybackStatsRow(label: "Stalls", value: stalls.formatted()))
        }
        sections.append(PlaybackStatsSection(title: "Playback", rows: playback))

        if sample.isOnShare {
            var network = [
                PlaybackStatsRow(
                    label: "Read speed",
                    value: sample.isConnectionLost
                        ? "Connection lost"
                        : readBytesPerSecond.map { bitRate($0 * 8) } ?? "Measuring…"
                ),
            ]
            if let bytes = sample.shareBytesRead {
                network.append(PlaybackStatsRow(label: "Read so far", value: byteCount(Int64(clamping: bytes))))
            }
            sections.append(PlaybackStatsSection(title: "Network", rows: network))
        }
        return sections
    }

    static func byteCount(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }

    static func bitRate(_ bitsPerSecond: Double) -> String {
        if bitsPerSecond >= 1_000_000 {
            return String(format: "%.1f Mb/s", bitsPerSecond / 1_000_000)
        }
        return String(format: "%.0f kb/s", bitsPerSecond / 1_000)
    }

    /// A readable name for a video track's four-character codec code.
    static func codecName(fourCC: UInt32, format: StereoFormat) -> String {
        switch fourCC {
        case 0x6876_6331, 0x6865_7631: // hvc1, hev1
            return format == .mvHEVC ? "MV-HEVC" : "HEVC"
        case 0x6176_6331, 0x6176_6333: // avc1, avc3
            return "H.264"
        default:
            let bytes = [24, 16, 8, 0].map { UInt8(truncatingIfNeeded: fourCC >> $0) }
            let text = String(decoding: bytes, as: UTF8.self).trimmingCharacters(in: .whitespaces)
            return text.isEmpty ? "Unknown" : text
        }
    }
}

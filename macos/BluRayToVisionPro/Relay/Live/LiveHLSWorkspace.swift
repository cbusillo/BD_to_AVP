import Darwin
import Foundation

/// A live HLS workspace manifest was malformed or broke one of its invariants. The stream fails
/// with `code` rather than serving anything it cannot vouch for.
struct LiveHLSManifestError: Error, Equatable {
    static let code = "live_hls_manifest_invalid"
    let reason: String
}

/// `live-hls.json` as the worker writes it. Only the fields the relay serves are decoded; each is
/// checked strictly before the window uses it.
struct LiveHLSManifest: Decodable, Equatable {
    struct Segment: Decodable, Equatable {
        let sequence: UInt64
        let uri: String
        let startMicroseconds: Int64
        let durationMicroseconds: Int64
        let sizeBytes: Int64

        enum CodingKeys: String, CodingKey {
            case sequence
            case uri
            case startMicroseconds = "start_microseconds"
            case durationMicroseconds = "duration_microseconds"
            case sizeBytes = "size_bytes"
        }
    }

    struct RetiredSegment: Decodable, Equatable {
        let uri: String
        let sizeBytes: Int64

        enum CodingKeys: String, CodingKey {
            case uri
            case sizeBytes = "size_bytes"
        }
    }

    /// Producer capacity apart from pacing; see docs/live-hls-service.md.
    struct Producer: Decodable, Equatable, Sendable {
        let producedMicroseconds: Int64
        let wallSeconds: Double
        let heldSeconds: Double
        let viewerHeldSeconds: Double

        enum CodingKeys: String, CodingKey {
            case producedMicroseconds = "produced_microseconds"
            case wallSeconds = "wall_seconds"
            case heldSeconds = "held_seconds"
            case viewerHeldSeconds = "viewer_held_seconds"
        }
    }

    /// One alternate audio track the worker encodes beside the video's muxed audio. Its files
    /// are `audio-<pid>-init.mp4` and `audio-<pid>-segment-<n>.m4s` in the same workspace.
    struct AudioRendition: Decodable, Equatable {
        let sourcePID: Int
        let complete: Bool
        let initialization: String
        let targetDurationSeconds: Int
        let mediaSequence: UInt64
        let earliestAvailableMicroseconds: Int64
        let latestAvailableMicroseconds: Int64
        let segments: [Segment]
        let retiredSegments: [RetiredSegment]

        enum CodingKeys: String, CodingKey {
            case sourcePID = "source_pid"
            case complete
            case initialization
            case targetDurationSeconds = "target_duration_seconds"
            case mediaSequence = "media_sequence"
            case earliestAvailableMicroseconds = "earliest_available_microseconds"
            case latestAvailableMicroseconds = "latest_available_microseconds"
            case segments
            case retiredSegments = "retired_segments"
        }

        var namePrefix: String { LiveHLSManifest.audioNamePrefix(pid: sourcePID) }
    }

    static let fileName = "live-hls.json"
    static let initialization = "init.mp4"
    static let maximumBytes = 4 * 1_024 * 1_024
    /// The source helper carries at most seven alternate tracks.
    static let maximumAudioRenditions = 7

    let schemaVersion: Int
    let type: String
    let complete: Bool
    let initialization: String
    let targetDurationSeconds: Int
    let mediaSequence: UInt64
    let earliestAvailableMicroseconds: Int64
    let latestAvailableMicroseconds: Int64
    let segments: [Segment]
    let retiredSegments: [RetiredSegment]
    let producer: Producer?
    let audioRenditions: [AudioRendition]?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case type
        case complete
        case initialization
        case targetDurationSeconds = "target_duration_seconds"
        case mediaSequence = "media_sequence"
        case earliestAvailableMicroseconds = "earliest_available_microseconds"
        case latestAvailableMicroseconds = "latest_available_microseconds"
        case segments
        case retiredSegments = "retired_segments"
        case producer
        case audioRenditions = "audio_renditions"
    }

    static func decode(_ data: Data) throws -> LiveHLSManifest {
        guard data.count <= maximumBytes else { throw LiveHLSManifestError(reason: "too_large") }
        let manifest: LiveHLSManifest
        do {
            manifest = try JSONDecoder().decode(LiveHLSManifest.self, from: data)
        } catch {
            throw LiveHLSManifestError(reason: "undecodable")
        }
        try manifest.validate()
        return manifest
    }

    static func audioNamePrefix(pid: Int) -> String {
        "audio-\(pid)-"
    }

    /// Public segment names; staging uses other names and never appears here. An alternate audio
    /// rendition's names carry its `audio-<pid>-` prefix.
    static func isSegmentName(_ name: String, namePrefix: String = "") -> Bool {
        let bytes = Array(name.utf8)
        let prefix = Array("\(namePrefix)segment-".utf8)
        let suffix = Array(".m4s".utf8)
        let digits = bytes.count - prefix.count - suffix.count
        guard (5 ... 12).contains(digits),
              bytes.starts(with: prefix),
              bytes.suffix(suffix.count).elementsEqual(suffix)
        else { return false }
        return bytes[prefix.count ..< prefix.count + digits].allSatisfy { $0 >= 48 && $0 <= 57 }
    }

    private func validate() throws {
        func require(_ condition: Bool, _ reason: String) throws {
            if !condition { throw LiveHLSManifestError(reason: reason) }
        }
        try require(schemaVersion == 1 && type == "live_hls", "schema")
        try require(initialization == Self.initialization, "initialization")
        try Self.validateWindow(
            namePrefix: "",
            targetDurationSeconds: targetDurationSeconds,
            mediaSequence: mediaSequence,
            earliestAvailableMicroseconds: earliestAvailableMicroseconds,
            latestAvailableMicroseconds: latestAvailableMicroseconds,
            segments: segments,
            retiredSegments: retiredSegments
        )
        let renditions = audioRenditions ?? []
        try require(renditions.count <= Self.maximumAudioRenditions, "audio_rendition_count")
        try require(Set(renditions.map(\.sourcePID)).count == renditions.count, "audio_rendition_duplicate")
        for rendition in renditions {
            try require((1 ... 8_191).contains(rendition.sourcePID), "audio_rendition_pid")
            try require(rendition.initialization == rendition.namePrefix + Self.initialization, "audio_initialization")
            try Self.validateWindow(
                namePrefix: rendition.namePrefix,
                targetDurationSeconds: rendition.targetDurationSeconds,
                mediaSequence: rendition.mediaSequence,
                earliestAvailableMicroseconds: rendition.earliestAvailableMicroseconds,
                latestAvailableMicroseconds: rendition.latestAvailableMicroseconds,
                segments: rendition.segments,
                retiredSegments: rendition.retiredSegments
            )
        }
    }

    private static func validateWindow(
        namePrefix: String,
        targetDurationSeconds: Int,
        mediaSequence: UInt64,
        earliestAvailableMicroseconds: Int64,
        latestAvailableMicroseconds: Int64,
        segments: [Segment],
        retiredSegments: [RetiredSegment]
    ) throws {
        func require(_ condition: Bool, _ reason: String) throws {
            if !condition { throw LiveHLSManifestError(reason: reason) }
        }
        try require(targetDurationSeconds > 0 && targetDurationSeconds <= 60, "target_duration")
        try require(!segments.isEmpty, "empty")
        let maximumDuration = Int64(targetDurationSeconds) * 1_000_000 + 500_000
        var expectedSequence = mediaSequence
        var expectedStart = segments[0].startMicroseconds
        try require(expectedStart >= 0 && expectedStart == earliestAvailableMicroseconds, "earliest")
        for segment in segments {
            try require(Self.isSegmentName(segment.uri, namePrefix: namePrefix), "segment_name")
            try require(segment.sequence == expectedSequence, "sequence_gap")
            try require(segment.startMicroseconds == expectedStart, "timeline_gap")
            try require(segment.durationMicroseconds > 0 && segment.durationMicroseconds <= maximumDuration, "duration")
            try require(segment.sizeBytes > 0, "size")
            let next = expectedSequence.addingReportingOverflow(1)
            let end = expectedStart.addingReportingOverflow(segment.durationMicroseconds)
            try require(!next.overflow && !end.overflow, "overflow")
            expectedSequence = next.partialValue
            expectedStart = end.partialValue
        }
        try require(expectedStart == latestAvailableMicroseconds, "latest")
        try require(
            retiredSegments.allSatisfy { Self.isSegmentName($0.uri, namePrefix: namePrefix) && $0.sizeBytes > 0 },
            "retired_segment"
        )
    }
}

/// The window the relay serves, built from successive manifests.
///
/// It keeps the retained segments, the URIs still listed as retired, and a bounded history of
/// segment timing by URI, because retired entries carry only a URI and a size.
struct LiveHLSWindow: Sendable {
    struct Timing: Equatable, Sendable {
        let startMicroseconds: Int64
        let durationMicroseconds: Int64
    }

    /// One alternate audio rendition's current window and its rendered media playlist.
    struct AudioWindow: Equatable, Sendable {
        let pid: Int
        let initialization: String
        let segments: [LiveHLSWindowSegment]
        let retiredURIs: Set<String>
        let complete: Bool
        let mediaSequence: UInt64
        let latestMicroseconds: Int64
        let playlist: String
    }

    static let historyLimit = 512

    private(set) var segments: [LiveHLSWindowSegment] = []
    /// Fixed by the first manifest: the worker never adds or drops a rendition mid-stream.
    private(set) var audioWindows: [AudioWindow] = []
    private(set) var retiredURIs: Set<String> = []
    private(set) var complete = false
    private(set) var rendered: LiveHLSRenderedWindow?
    private(set) var producer: LiveHLSManifest.Producer?
    private var mediaSequence: UInt64?
    private var latestMicroseconds: Int64 = 0
    private var history: [String: Timing] = [:]
    private var historyOrder: [String] = []

    var earliestMicroseconds: Int64? { segments.first?.startMicroseconds }
    var latestAvailableMicroseconds: Int64? { segments.last?.endMicroseconds }

    /// Applies the next manifest. Media sequence and the latest position never decrease,
    /// completion never reverts, and a URI never changes its timing.
    mutating func apply(_ manifest: LiveHLSManifest) throws {
        if let mediaSequence {
            guard manifest.mediaSequence >= mediaSequence,
                  manifest.latestAvailableMicroseconds >= latestMicroseconds,
                  manifest.complete || !complete
            else {
                throw LiveHLSManifestError(reason: "regressed")
            }
        }
        let next = manifest.segments.map {
            LiveHLSWindowSegment(
                sequence: $0.sequence,
                uri: $0.uri,
                startMicroseconds: $0.startMicroseconds,
                durationMicroseconds: $0.durationMicroseconds
            )
        }
        for segment in next {
            let timing = Timing(startMicroseconds: segment.startMicroseconds, durationMicroseconds: segment.durationMicroseconds)
            if let known = history[segment.uri], known != timing {
                throw LiveHLSManifestError(reason: "timing_changed")
            }
        }
        let rendered: LiveHLSRenderedWindow
        do {
            rendered = try LiveHLSPlaylistRenderer.render(
                targetDurationSeconds: manifest.targetDurationSeconds,
                initialization: manifest.initialization,
                segments: next,
                complete: manifest.complete
            )
        } catch {
            throw LiveHLSManifestError(reason: "unrenderable")
        }
        let nextAudio = try nextAudioWindows(manifest.audioRenditions ?? [])
        for segment in next where history[segment.uri] == nil {
            history[segment.uri] = Timing(
                startMicroseconds: segment.startMicroseconds,
                durationMicroseconds: segment.durationMicroseconds
            )
            historyOrder.append(segment.uri)
        }
        if historyOrder.count > Self.historyLimit {
            let excess = historyOrder.count - Self.historyLimit
            for uri in historyOrder.prefix(excess) {
                history[uri] = nil
            }
            historyOrder.removeFirst(excess)
        }
        segments = next
        retiredURIs = Set(manifest.retiredSegments.map(\.uri))
        complete = manifest.complete
        mediaSequence = manifest.mediaSequence
        latestMicroseconds = manifest.latestAvailableMicroseconds
        producer = manifest.producer
        audioWindows = nextAudio
        self.rendered = rendered
    }

    /// The next audio windows. The rendition set never changes, and each rendition's media
    /// sequence and latest position never go back nor its completion revert.
    private func nextAudioWindows(_ renditions: [LiveHLSManifest.AudioRendition]) throws -> [AudioWindow] {
        if rendered != nil, renditions.map(\.sourcePID) != audioWindows.map(\.pid) {
            throw LiveHLSManifestError(reason: "audio_renditions_changed")
        }
        return try renditions.enumerated().map { index, rendition in
            if rendered != nil {
                let previous = audioWindows[index]
                guard rendition.mediaSequence >= previous.mediaSequence,
                      rendition.latestAvailableMicroseconds >= previous.latestMicroseconds,
                      rendition.complete || !previous.complete
                else { throw LiveHLSManifestError(reason: "regressed") }
            }
            let segments = rendition.segments.map {
                LiveHLSWindowSegment(
                    sequence: $0.sequence,
                    uri: $0.uri,
                    startMicroseconds: $0.startMicroseconds,
                    durationMicroseconds: $0.durationMicroseconds
                )
            }
            let playlist: String
            do {
                playlist = try LiveHLSPlaylistRenderer.render(
                    targetDurationSeconds: rendition.targetDurationSeconds,
                    initialization: rendition.initialization,
                    segments: segments,
                    complete: rendition.complete
                ).playlist
            } catch {
                throw LiveHLSManifestError(reason: "unrenderable")
            }
            return AudioWindow(
                pid: rendition.sourcePID,
                initialization: rendition.initialization,
                segments: segments,
                retiredURIs: Set(rendition.retiredSegments.map(\.uri)),
                complete: rendition.complete,
                mediaSequence: rendition.mediaSequence,
                latestMicroseconds: rendition.latestAvailableMicroseconds,
                playlist: playlist
            )
        }
    }

    /// The rendered media playlist served as `name`: `video.m3u8`, or `audio-<pid>.m3u8` for a
    /// rendition this stream carries.
    func mediaPlaylist(named name: String) -> String? {
        guard let rendered else { return nil }
        if name == LiveHLSPlaylistRenderer.videoPlaylistName { return rendered.playlist }
        return audioWindows.first { LiveHLSPlaylistRenderer.audioPlaylistName(pid: $0.pid) == name }?.playlist
    }

    /// `init.mp4`, a retained segment, or a segment the manifest still lists as retired, of the
    /// video or of an audio rendition.
    func isServable(_ name: String) -> Bool {
        guard rendered != nil else { return false }
        return name == LiveHLSManifest.initialization
            || retiredURIs.contains(name)
            || segments.contains { $0.uri == name }
            || audioWindows.contains { audio in
                name == audio.initialization || audio.retiredURIs.contains(name)
                    || audio.segments.contains { $0.uri == name }
            }
    }

    func timing(for uri: String) -> Timing? {
        history[uri]
    }
}

/// Follows one worker's live HLS workspace. The worker replaces the manifest atomically, so a
/// poll that sees the same inode, size, and modification time skips the read.
struct LiveHLSWorkspace: Sendable {
    enum MediaError: Error, Equatable {
        case notFound
        case tooLarge
    }

    let rootURL: URL
    let maximumMediaBytes: Int
    private(set) var window = LiveHLSWindow()
    private var lastManifestStamp: [Int64]?

    init(rootURL: URL, maximumMediaBytes: Int = 64 * 1_024 * 1_024) {
        self.rootURL = rootURL.standardizedFileURL
        self.maximumMediaBytes = maximumMediaBytes
    }

    var manifestURL: URL {
        rootURL.appendingPathComponent(LiveHLSManifest.fileName)
    }

    /// Reads the manifest when it changed. Returns whether the window changed. A missing manifest
    /// is not an error: the worker has not published yet.
    mutating func poll() throws -> Bool {
        var info = stat()
        guard lstat(manifestURL.path, &info) == 0 else {
            if errno == ENOENT { return false }
            throw LiveHLSManifestError(reason: "unreadable")
        }
        guard info.st_mode & S_IFMT == S_IFREG else { throw LiveHLSManifestError(reason: "not_a_file") }
        let stamp = [
            Int64(info.st_ino), Int64(info.st_size),
            Int64(info.st_mtimespec.tv_sec), Int64(info.st_mtimespec.tv_nsec),
        ]
        guard stamp != lastManifestStamp else { return false }
        let data: Data
        do {
            data = try Self.read(manifestURL, maximumBytes: LiveHLSManifest.maximumBytes)
        } catch MediaError.notFound {
            return false
        } catch {
            throw LiveHLSManifestError(reason: "unreadable")
        }
        try window.apply(LiveHLSManifest.decode(data))
        lastManifestStamp = stamp
        return true
    }

    /// The bytes of a servable resource. A retired segment the worker has already unlinked is
    /// `notFound`, as is anything the window does not list.
    func media(named name: String) throws -> Data {
        guard window.isServable(name) else { throw MediaError.notFound }
        return try Self.read(rootURL.appendingPathComponent(name), maximumBytes: maximumMediaBytes)
    }

    private static func read(_ url: URL, maximumBytes: Int) throws -> Data {
        let descriptor = open(url.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else {
            if errno == ENOENT || errno == ELOOP { throw MediaError.notFound }
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        var info = stat()
        guard fstat(descriptor, &info) == 0, info.st_mode & S_IFMT == S_IFREG else {
            throw MediaError.notFound
        }
        guard info.st_size <= Int64(maximumBytes) else { throw MediaError.tooLarge }
        let data = try handle.readToEnd() ?? Data()
        guard data.count <= maximumBytes else { throw MediaError.tooLarge }
        return data
    }
}

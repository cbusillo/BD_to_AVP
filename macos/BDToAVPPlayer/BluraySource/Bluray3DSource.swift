import Foundation

enum Bluray3DTrackKind: String, Codable, Hashable, Sendable {
    case audio
    case subtitle
}

/// One audio or subtitle stream of a title. `id` is the transport PID on a disc and the track
/// number in a Matroska file; packets carry the same value as their `pid`.
struct Bluray3DTrack: Codable, Hashable, Identifiable, Sendable {
    let id: Int
    let kind: Bluray3DTrackKind
    /// The Blu-ray `stream_coding_type`; Matroska codecs are mapped onto the same values, and 0
    /// means a codec Blu-ray does not use.
    let codingType: UInt8
    /// ISO 639-2 code, such as "eng", when the source names one.
    let language: String?
    let name: String?

    var codecName: String {
        switch codingType {
        case 0x80: return "LPCM"
        case 0x81: return "Dolby Digital"
        case 0x82: return "DTS"
        case 0x83: return "Dolby TrueHD"
        case 0x84, 0xA1: return "Dolby Digital Plus"
        case 0x85, 0xA2: return "DTS-HD High Resolution"
        case 0x86: return "DTS-HD Master Audio"
        case 0x90: return "PGS"
        case 0x92: return "Text"
        default: return "Unsupported codec"
        }
    }
}

enum Bluray3DUnsupportedReason: String, Codable, Hashable, Sendable {
    case encrypted
    case notStereo3D = "not-3d"
    case multipleAngles = "multiple-angles"
    /// Only in records saved before titles made of several clips could play (#1142); kept so
    /// those records still load, and Locations inspect such files again.
    case multipleClips = "multiple-clips"
    case partialClip = "partial-clip"
    case stereoStreamMissing = "ssif-missing"

    var message: String {
        switch self {
        case .encrypted:
            return "This disc is encrypted. Play it through the Mac with your own MakeMKV."
        case .notStereo3D:
            return "This source has no Blu-ray 3D video."
        case .multipleAngles:
            return "Titles with several camera angles are not supported yet."
        case .multipleClips:
            return "Titles made of several clips are not supported yet."
        case .partialClip:
            return "This title starts or ends partway through a clip where playback can't join it yet."
        case .stereoStreamMissing:
            return "This title's 3D stream is missing from the disc."
        }
    }
}

/// One title of a Blu-ray 3D source, with the streams the player can choose from.
struct Bluray3DTitle: Codable, Hashable, Identifiable, Sendable {
    /// "playlist:00005" on a disc, "matroska" for a Matroska file.
    let id: String
    let playlist: UInt32?
    let durationSeconds: Double
    let isMainFeature: Bool
    /// Blu-ray 3D usually codes the left eye as the base view; this flag marks the exception.
    let baseViewIsRightEye: Bool
    let unsupportedReason: Bluray3DUnsupportedReason?
    let audioTracks: [Bluray3DTrack]
    let subtitleTracks: [Bluray3DTrack]

    var isPlayable: Bool { unsupportedReason == nil }

    /// The audio streams in the form the headset audio player takes.
    var audioStreams: [BluRayAudioStreamDescriptor] {
        audioTracks.compactMap { track in
            BluRayAudioCoding(rawValue: track.codingType).map {
                BluRayAudioStreamDescriptor(pid: UInt16(clamping: track.id), coding: $0, languageCode: track.language)
            }
        }
    }
}

/// Everything the library and player learn when a source is opened.
struct Bluray3DSourceInfo: Codable, Hashable, Sendable {
    enum Container: String, Codable, Hashable, Sendable {
        case discImage = "disc-image"
        case discFolder = "disc-folder"
        case matroska
    }

    let container: Container
    /// Titles in preference order: playable first, then longest.
    let titles: [Bluray3DTitle]

    /// The title to play: the first playable one, if any.
    var mainTitle: Bluray3DTitle? { titles.first(where: \.isPlayable) }
}

/// One stereo frame in decode order: base and dependent view NAL units as one Annex B
/// byte stream, ready for an MVC decoder such as edge264.
struct MVCAccessUnit: Sendable {
    /// The 33-bit, 90 kHz PTS. Map it with `BluRayPresentationTimeline`, whose origin is the
    /// title's first video PTS, as the audio player does.
    let pts: UInt64
    /// The 33-bit, 90 kHz DTS; nil when the container stores no decode time (Matroska).
    let dts: UInt64?
    /// An IDR, where decoding can start.
    let isRandomAccess: Bool
    let data: Data
}

/// One packet of a title, in stream order. Audio and PG subtitles use the same PES packet type
/// the headset audio player consumes (`BluRayPESPacketSink`).
enum Bluray3DPacket: Sendable {
    case video(MVCAccessUnit)
    case audio(BluRayPESPacket)
    case subtitle(BluRayPESPacket)
    /// A disc title's next clip starts here, on the playlist timeline (33-bit, 90 kHz), when
    /// subtitles are read. Subtitles shown before it end.
    case clipStart(pts: UInt64)
}

/// What the player's decoders consume. The reader calls `receive` on its own thread, in stream
/// order, and blocks while the sink does, so a sink can pace the reader; returning false stops it.
protocol Bluray3DPacketSink: AnyObject {
    func receive(_ packet: Bluray3DPacket) -> Bool
    /// Whether the read still wants packets. A reader waiting to retry a share read stops once
    /// this turns false, as when a seek replaces the read, instead of retrying until the share
    /// answers again.
    var isWanted: Bool { get }
}

extension Bluray3DPacketSink {
    var isWanted: Bool { true }
}

/// Forwards video to a closure and audio to the headset audio player's PES sink.
final class Bluray3DPacketRouter: Bluray3DPacketSink {
    private let video: (MVCAccessUnit) -> Bool
    private let audio: BluRayPESPacketSink

    init(audio: BluRayPESPacketSink, video: @escaping (MVCAccessUnit) -> Bool) {
        self.video = video
        self.audio = audio
    }

    func receive(_ packet: Bluray3DPacket) -> Bool {
        switch packet {
        case let .video(unit): return video(unit)
        case let .audio(packet): audio.receive(packet)
        case .subtitle, .clipStart: break
        }
        return true
    }
}

struct Bluray3DReadStatistics: Codable, Equatable, Sendable {
    var bytes: UInt64 = 0
    var videoAccessUnits: UInt64 = 0
    var audioPackets: UInt64 = 0
    var subtitlePackets: UInt64 = 0
}

enum Bluray3DSourceError: Error, Equatable, LocalizedError {
    case unrecognized
    case openFailed
    case titleUnavailable
    case unsupported(Bluray3DUnsupportedReason)
    case invalidStream(String)
    case readFailed

    var errorDescription: String? {
        switch self {
        case .unrecognized:
            return "Choose a Blu-ray 3D ISO, a disc folder that contains BDMV, or an MKV with 3D video."
        case .openFailed:
            return "The Blu-ray source could not be opened."
        case .titleUnavailable:
            return "The title is not on this source."
        case let .unsupported(reason):
            return reason.message
        case let .invalidStream(detail):
            return "The 3D stream could not be read: \(detail)."
        case .readFailed:
            return "The source stopped responding while reading."
        }
    }
}

/// A Blu-ray 3D source the player can read.
protocol Bluray3DSource: AnyObject {
    var info: Bluray3DSourceInfo { get }

    /// Reads `title` from `startTicks` on its timeline (90 kHz, zero at the title's start) to
    /// the end, an error, or until the sink stops, delivering its MVC video plus the chosen audio
    /// and subtitle tracks to `sink`. Video starts at the last random access unit at or before
    /// `startTicks`, found through the disc's entry-point map or the Matroska cues. Blocks the
    /// caller.
    func read(
        title: Bluray3DTitle,
        from startTicks: Int64,
        audioTrackIDs: [Int],
        subtitleTrackIDs: [Int],
        into sink: Bluray3DPacketSink
    ) throws -> Bluray3DReadStatistics
}

extension Bluray3DSource {
    /// Reads `title` from its start.
    func read(
        title: Bluray3DTitle,
        audioTrackIDs: [Int],
        subtitleTrackIDs: [Int],
        into sink: Bluray3DPacketSink
    ) throws -> Bluray3DReadStatistics {
        try read(title: title, from: 0, audioTrackIDs: audioTrackIDs, subtitleTrackIDs: subtitleTrackIDs, into: sink)
    }
}

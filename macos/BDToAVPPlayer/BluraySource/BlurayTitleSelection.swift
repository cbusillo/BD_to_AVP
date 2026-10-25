import Foundation

/// What libbluray reports about one title, before the player decides whether it can play it.
struct BlurayTitleFacts: Equatable, Sendable {
    struct Stream: Equatable, Sendable {
        let pid: UInt16
        let codingType: UInt8
        let language: String
    }

    let playlist: UInt32
    let durationTicks: UInt64
    let angleCount: UInt32
    let clipCount: UInt32
    let isMainFeature: Bool
    let coversCompleteClip: Bool
    let hasStereoStream: Bool
    let baseViewIsRightEye: Bool
    let clipID: String
    let audio: [Stream]
    let subtitles: [Stream]
}

struct BlurayDiscFacts: Equatable, Sendable {
    let isStereo3D: Bool
    let isEncrypted: Bool
}

/// Chooses the playable titles of a disc, matching the Mac's `ssif_probe titles` rules.
enum BlurayTitleSelection {
    /// Why a title cannot play from its SSIF stream, or nil when it can.
    static func unsupportedReason(_ title: BlurayTitleFacts, disc: BlurayDiscFacts) -> Bluray3DUnsupportedReason? {
        if disc.isEncrypted { return .encrypted }
        if !disc.isStereo3D { return .notStereo3D }
        if title.angleCount != 1 { return .multipleAngles }
        if title.clipCount != 1 { return .multipleClips }
        if !title.coversCompleteClip { return .partialClip }
        if !title.hasStereoStream { return .stereoStreamMissing }
        return nil
    }

    /// Playable titles first, then longest, then the disc's main feature, then lower playlist.
    static func ordered(_ titles: [BlurayTitleFacts], disc: BlurayDiscFacts) -> [Bluray3DTitle] {
        titles
            .map { (facts: $0, reason: unsupportedReason($0, disc: disc)) }
            .sorted { first, second in
                if (first.reason == nil) != (second.reason == nil) { return first.reason == nil }
                if first.facts.durationTicks != second.facts.durationTicks {
                    return first.facts.durationTicks > second.facts.durationTicks
                }
                if first.facts.isMainFeature != second.facts.isMainFeature { return first.facts.isMainFeature }
                return first.facts.playlist < second.facts.playlist
            }
            .map { title($0.facts, reason: $0.reason) }
    }

    static func title(_ facts: BlurayTitleFacts, reason: Bluray3DUnsupportedReason?) -> Bluray3DTitle {
        Bluray3DTitle(
            id: String(format: "playlist:%05u", facts.playlist),
            playlist: facts.playlist,
            durationSeconds: Double(facts.durationTicks) / 90_000,
            isMainFeature: facts.isMainFeature,
            baseViewIsRightEye: facts.baseViewIsRightEye,
            unsupportedReason: reason,
            audioTracks: facts.audio.map { track($0, kind: .audio) },
            subtitleTracks: facts.subtitles.map { track($0, kind: .subtitle) }
        )
    }

    static func track(_ stream: BlurayTitleFacts.Stream, kind: Bluray3DTrackKind) -> Bluray3DTrack {
        Bluray3DTrack(
            id: Int(stream.pid),
            kind: kind,
            codingType: stream.codingType,
            language: stream.language.isEmpty ? nil : stream.language,
            name: nil
        )
    }
}

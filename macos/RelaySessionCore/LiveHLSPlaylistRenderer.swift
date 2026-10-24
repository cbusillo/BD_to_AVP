import Foundation

/// One retained segment of a live HLS window, in the worker's microsecond timeline.
struct LiveHLSWindowSegment: Equatable, Sendable {
let sequence: UInt64
let uri: String
let startMicroseconds: Int64
let durationMicroseconds: Int64

init(sequence: UInt64, uri: String, startMicroseconds: Int64, durationMicroseconds: Int64) {
        self.sequence = sequence
        self.uri = uri
        self.startMicroseconds = startMicroseconds
        self.durationMicroseconds = durationMicroseconds
    }

var endMicroseconds: Int64 {
        startMicroseconds + durationMicroseconds
    }
}

/// The playlist and snapshot a relay serves for one live window. Both come from the same window,
/// so the player's seek checks and AVPlayer see the same segments.
struct LiveHLSRenderedWindow: Equatable, Sendable {
let playlist: String
let snapshot: RelayPlaylistSnapshot
}

/// One audio choice in a live multivariant playlist.
struct LiveHLSAudioChoice: Equatable, Sendable {
    let name: String
    /// A BCP 47 language tag, or nil when the disc names none.
    let language: String?
    /// The rendition's media playlist name under the media prefix; nil for the audio muxed into
    /// the video segments.
    let playlistName: String?
}

enum LiveHLSPlaylistRenderer {
    static let videoPlaylistName = "video.m3u8"
    /// Nominal peak of a 20 Mbps MV-HEVC stream with 192 kbps audio. With one variant it only
    /// informs AVPlayer's buffering estimate.
    static let variantBandwidth = 25_000_000
    private static let audioGroupID = "audio"

    static func audioPlaylistName(pid: Int) -> String {
        "audio-\(pid).m3u8"
    }

    /// Renders the multivariant playlist a live title starts from: one video variant, whose
    /// segments carry the selected audio, plus the alternate audio renditions AVPlayer offers as
    /// media selection options. The first choice is the muxed audio and the default.
    ///
    /// - There is no `REQ-VIDEO-LAYOUT`: the MV-HEVC initialization segment already marks the
    ///   stereo views, as it does for the media playlist the player used before, and macOS
    ///   AVFoundation refuses a variant that requires the stereo layout.
    /// - `CLOSED-CAPTIONS=NONE` keeps AVPlayer from offering CEA-608 captions the stream lacks.
    static func renderMultivariant(audio: [LiveHLSAudioChoice]) -> String {
        var lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            "#EXT-X-INDEPENDENT-SEGMENTS",
            "#EXT-X-START:TIME-OFFSET=0,PRECISE=YES",
        ]
        var usedNames: Set<String> = []
        for (index, choice) in audio.enumerated() {
            var name = quotedStringSafe(choice.name)
            if usedNames.contains(name) {
                name = "\(name) (\(index + 1))"
            }
            usedNames.insert(name)
            var attributes = [
                "TYPE=AUDIO",
                "GROUP-ID=\"\(audioGroupID)\"",
                "NAME=\"\(name)\"",
            ]
            if let language = choice.language {
                attributes.append("LANGUAGE=\"\(quotedStringSafe(language))\"")
            }
            attributes.append(index == 0 ? "DEFAULT=YES" : "DEFAULT=NO")
            attributes.append("AUTOSELECT=YES")
            attributes.append("CHANNELS=\"2\"")
            if let playlistName = choice.playlistName {
                attributes.append("URI=\"\(RelayWireContract.mediaPathPrefix)\(playlistName)\"")
            }
            lines.append("#EXT-X-MEDIA:" + attributes.joined(separator: ","))
        }
        let audioGroup = audio.isEmpty ? "" : ",AUDIO=\"\(audioGroupID)\""
        lines.append("#EXT-X-STREAM-INF:BANDWIDTH=\(variantBandwidth)\(audioGroup),CLOSED-CAPTIONS=NONE")
        lines.append("\(RelayWireContract.mediaPathPrefix)\(videoPlaylistName)")
        return lines.joined(separator: "\n") + "\n"
    }

    /// A quoted-string may not contain a double quote, CR, or LF.
    private static func quotedStringSafe(_ value: String) -> String {
        String(value.unicodeScalars.map { scalar in
            scalar == "\"" || CharacterSet.newlines.contains(scalar) ? "'" : Character(scalar)
        })
    }

    /// The BCP 47 tag for a disc's ISO 639-2 language code, such as `de` for `deu` or `ger`.
    static func languageTag(forDiscLanguage code: String?) -> String? {
        guard let code, !code.isEmpty,
              code.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.contains($0) && $0.isASCII })
        else { return nil }
        return Locale.LanguageCode(code.lowercased()).identifier(.alpha2) ?? code.lowercased()
    }

    /// Renders a sliding live window.
    ///
    /// - Every URI is root-relative under `RelayWireContract.mediaPathPrefix`, the only form the
    ///   player's loopback bridge accepts.
    /// - There is no `EXT-X-PLAYLIST-TYPE`: the window evicts history, which an `EVENT` playlist
    ///   must never do. `EXT-X-START:TIME-OFFSET=0` asks the player to begin at the earliest
    ///   retained segment rather than near the live edge. `EXT-X-ENDLIST` marks completion.
    /// - The snapshot converts segment boundaries, not durations, to milliseconds, so adjacent
    ///   segments stay contiguous when a duration is not a whole number of milliseconds.
static func render(
        targetDurationSeconds: Int,
        initialization: String,
        segments: [LiveHLSWindowSegment],
        complete: Bool
    ) throws -> LiveHLSRenderedWindow {
        guard targetDurationSeconds > 0, let first = segments.first, let last = segments.last else {
            throw RelayPlaylistError.invalidSegment
        }
        var lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            "#EXT-X-TARGETDURATION:\(targetDurationSeconds)",
            "#EXT-X-MEDIA-SEQUENCE:\(first.sequence)",
            "#EXT-X-START:TIME-OFFSET=0,PRECISE=YES",
            "#EXT-X-INDEPENDENT-SEGMENTS",
            "#EXT-X-MAP:URI=\"\(RelayWireContract.mediaPathPrefix)\(initialization)\"",
        ]
        var snapshotSegments: [RelayPlaylistSegment] = []
        snapshotSegments.reserveCapacity(segments.count)
        for segment in segments {
            lines.append("#EXTINF:\(seconds(fromMicroseconds: segment.durationMicroseconds)),")
            lines.append("\(RelayWireContract.mediaPathPrefix)\(segment.uri)")
            let startMilliseconds = milliseconds(boundaryMicroseconds: segment.startMicroseconds)
            snapshotSegments.append(try RelayPlaylistSegment(
                sequenceNumber: segment.sequence,
                startTimeMilliseconds: startMilliseconds,
                durationMilliseconds: milliseconds(boundaryMicroseconds: segment.endMicroseconds) - startMilliseconds,
                resourceIdentifier: segment.uri
            ))
        }
        if complete {
            lines.append("#EXT-X-ENDLIST")
        }
        let snapshot = try RelayPlaylistSnapshot(
            earliestPlayableTimeMilliseconds: milliseconds(boundaryMicroseconds: first.startMicroseconds),
            totalDurationMilliseconds: milliseconds(boundaryMicroseconds: last.endMicroseconds),
            isFinalized: complete,
            segments: snapshotSegments
        )
        return LiveHLSRenderedWindow(playlist: lines.joined(separator: "\n") + "\n", snapshot: snapshot)
    }

    /// A timeline boundary in milliseconds, rounded down.
static func milliseconds(boundaryMicroseconds: Int64) -> Int64 {
        boundaryMicroseconds / 1_000
    }

    private static func seconds(fromMicroseconds value: Int64) -> String {
        let fraction = String(value % 1_000_000)
        return "\(value / 1_000_000).\(String(repeating: "0", count: 6 - fraction.count))\(fraction)"
    }
}

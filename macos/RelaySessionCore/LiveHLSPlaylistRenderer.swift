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

enum LiveHLSPlaylistRenderer {
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

import Foundation

enum RelayEventHLSFixtureError: Error, Equatable, Sendable {
    case invalidDirectory
    case missingInitializationSegment
    case missingPlaylist
    case invalidPlaylist
    case missingSegment(String)
}

struct RelayEventHLSFixture: Equatable, Sendable {
    struct Segment: Equatable, Sendable {
        let resourceIdentifier: String
        let duration: TimeInterval
    }

    let initializationResourceIdentifier: String
    let targetDuration: TimeInterval
    let segments: [Segment]

    static func load(
        directory: URL,
        initializationResourceIdentifier: String = "init.mp4"
    ) throws -> RelayEventHLSFixture {
        let root = directory.standardizedFileURL.resolvingSymlinksInPath()
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: root.path, isDirectory: &isDirectory), isDirectory.boolValue else {
            throw RelayEventHLSFixtureError.invalidDirectory
        }

        try requireRegularFile(initializationResourceIdentifier, in: root, missing: .missingInitializationSegment)
        let playlistURL = root.appendingPathComponent("media.m3u8", isDirectory: false)
        guard FileManager.default.fileExists(atPath: playlistURL.path) else {
            throw RelayEventHLSFixtureError.missingPlaylist
        }
        let playlist = try String(contentsOf: playlistURL, encoding: .utf8)
        let parsed = try parse(playlist)
        for segment in parsed.segments {
            try requireRegularFile(
                segment.resourceIdentifier,
                in: root,
                missing: .missingSegment(segment.resourceIdentifier)
            )
        }
        return RelayEventHLSFixture(
            initializationResourceIdentifier: initializationResourceIdentifier,
            targetDuration: parsed.targetDuration,
            segments: parsed.segments
        )
    }

    private static func parse(
        _ source: String
    ) throws -> (targetDuration: TimeInterval, segments: [Segment]) {
        let lines = source
            .split(whereSeparator: \.isNewline)
            .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
        guard lines.first == "#EXTM3U" else {
            throw RelayEventHLSFixtureError.invalidPlaylist
        }

        var targetDuration: TimeInterval?
        var pendingDuration: TimeInterval?
        var segments: [Segment] = []
        for line in lines.dropFirst() where !line.isEmpty {
            if line.hasPrefix("#EXT-X-TARGETDURATION:") {
                let rawValue = line.dropFirst("#EXT-X-TARGETDURATION:".count)
                guard targetDuration == nil, let value = TimeInterval(rawValue), value.isFinite, value > 0 else {
                    throw RelayEventHLSFixtureError.invalidPlaylist
                }
                targetDuration = value
                continue
            }
            if line.hasPrefix("#EXTINF:") {
                let rawValue = line.dropFirst("#EXTINF:".count).split(separator: ",", maxSplits: 1).first ?? ""
                guard pendingDuration == nil,
                      let value = TimeInterval(rawValue),
                      value.isFinite,
                      value > 0
                else {
                    throw RelayEventHLSFixtureError.invalidPlaylist
                }
                pendingDuration = value
                continue
            }
            guard !line.hasPrefix("#") else {
                continue
            }
            guard let duration = pendingDuration, isSafeResourceIdentifier(line) else {
                throw RelayEventHLSFixtureError.invalidPlaylist
            }
            segments.append(Segment(resourceIdentifier: line, duration: duration))
            pendingDuration = nil
        }
        guard pendingDuration == nil,
              let targetDuration,
              !segments.isEmpty,
              segments.allSatisfy({ $0.duration <= targetDuration })
        else {
            throw RelayEventHLSFixtureError.invalidPlaylist
        }
        return (targetDuration, segments)
    }

    private static func requireRegularFile(
        _ resourceIdentifier: String,
        in root: URL,
        missing: RelayEventHLSFixtureError
    ) throws {
        guard isSafeResourceIdentifier(resourceIdentifier) else {
            throw RelayEventHLSFixtureError.invalidPlaylist
        }
        let candidate = root.appendingPathComponent(resourceIdentifier, isDirectory: false)
            .standardizedFileURL
            .resolvingSymlinksInPath()
        guard candidate.path.hasPrefix(root.path + "/"),
              FileManager.default.fileExists(atPath: candidate.path),
              !candidate.hasDirectoryPath
        else {
            throw missing
        }
    }

    private static func isSafeResourceIdentifier(_ value: String) -> Bool {
        let bytes = value.utf8
        guard (1 ... RelayPlaylistLimits.maximumResourceIdentifierLength).contains(bytes.count),
              !value.contains("%"),
              bytes.allSatisfy({ byte in
                  (byte >= 65 && byte <= 90)
                      || (byte >= 97 && byte <= 122)
                      || (byte >= 48 && byte <= 57)
                      || "-._/".utf8.contains(byte)
              })
        else {
            return false
        }
        return value.split(separator: "/", omittingEmptySubsequences: false).allSatisfy {
            !$0.isEmpty && $0 != "." && $0 != ".."
        }
    }
}

import Foundation

enum RelayEventHLSFixtureError: Error, Equatable, Sendable {
    case invalidDirectory
    case missingInitializationSegment
    case missingPlaylist
    case invalidPlaylist
    case missingSegment(String)
}

struct RelayEventHLSFixture: Equatable, Sendable {
    private static let maximumPlaylistBytes = 1_024 * 1_024
    private static let maximumSegmentCount = 1_000

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

        let playlistURL = root.appendingPathComponent("media.m3u8", isDirectory: false)
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: playlistURL.path),
              let playlistSize = attributes[.size] as? NSNumber
        else {
            throw RelayEventHLSFixtureError.missingPlaylist
        }
        guard playlistSize.intValue <= maximumPlaylistBytes,
              let playlist = String(data: try Data(contentsOf: playlistURL), encoding: .utf8)
        else {
            throw RelayEventHLSFixtureError.invalidPlaylist
        }
        let parsed = try parse(playlist, initializationResourceIdentifier: initializationResourceIdentifier)
        try requireRegularFile(
            parsed.initializationResourceIdentifier,
            in: root,
            missing: .missingInitializationSegment
        )
        for segment in parsed.segments {
            try requireRegularFile(
                segment.resourceIdentifier,
                in: root,
                missing: .missingSegment(segment.resourceIdentifier)
            )
        }
        return RelayEventHLSFixture(
            initializationResourceIdentifier: parsed.initializationResourceIdentifier,
            targetDuration: parsed.targetDuration,
            segments: parsed.segments
        )
    }

    private static func parse(
        _ source: String,
        initializationResourceIdentifier: String
    ) throws -> (initializationResourceIdentifier: String, targetDuration: TimeInterval, segments: [Segment]) {
        let lines = source
            .split(whereSeparator: \.isNewline)
            .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
        guard lines.first == "#EXTM3U" else {
            throw RelayEventHLSFixtureError.invalidPlaylist
        }

        var isEventPlaylist = false
        var parsedInitializationResourceIdentifier: String?
        var targetDuration: TimeInterval?
        var pendingDuration: TimeInterval?
        var segments: [Segment] = []
        for line in lines.dropFirst() where !line.isEmpty {
            if line == "#EXT-X-PLAYLIST-TYPE:EVENT" {
                guard !isEventPlaylist else {
                    throw RelayEventHLSFixtureError.invalidPlaylist
                }
                isEventPlaylist = true
                continue
            }
            if line.hasPrefix("#EXT-X-PLAYLIST-TYPE:") {
                throw RelayEventHLSFixtureError.invalidPlaylist
            }
            if line.hasPrefix("#EXT-X-MAP:") {
                let prefix = "#EXT-X-MAP:URI=\""
                guard parsedInitializationResourceIdentifier == nil,
                      line.hasPrefix(prefix), line.hasSuffix("\"")
                else {
                    throw RelayEventHLSFixtureError.invalidPlaylist
                }
                let identifier = String(line.dropFirst(prefix.count).dropLast())
                guard identifier == initializationResourceIdentifier,
                      isSafeResourceIdentifier(identifier)
                else {
                    throw RelayEventHLSFixtureError.invalidPlaylist
                }
                parsedInitializationResourceIdentifier = identifier
                continue
            }
            if line.hasPrefix("#EXT-X-TARGETDURATION:") {
                let rawValue = line.dropFirst("#EXT-X-TARGETDURATION:".count)
                guard targetDuration == nil, let value = Int(rawValue), value > 0 else {
                    throw RelayEventHLSFixtureError.invalidPlaylist
                }
                targetDuration = TimeInterval(value)
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
            guard segments.count < maximumSegmentCount else {
                throw RelayEventHLSFixtureError.invalidPlaylist
            }
            segments.append(Segment(resourceIdentifier: line, duration: duration))
            pendingDuration = nil
        }
        guard pendingDuration == nil,
              isEventPlaylist,
              let parsedInitializationResourceIdentifier,
              let targetDuration,
              !segments.isEmpty,
              segments.allSatisfy({ $0.duration <= targetDuration })
        else {
            throw RelayEventHLSFixtureError.invalidPlaylist
        }
        return (parsedInitializationResourceIdentifier, targetDuration, segments)
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
        var isDirectory: ObjCBool = false
        guard candidate.path.hasPrefix(root.path + "/"),
              FileManager.default.fileExists(atPath: candidate.path, isDirectory: &isDirectory),
              !isDirectory.boolValue
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

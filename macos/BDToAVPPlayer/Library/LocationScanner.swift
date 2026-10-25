import CryptoKit
import Foundation

/// A file or disc folder in a Location that might be a playable title.
struct LocationCandidate: Equatable, Sendable {
    /// Path inside the Location, "/"-separated; empty when the Location itself is a disc folder.
    let relativePath: String
    let size: Int64?
    let modified: Date?
    /// For a disc folder, a digest of the BDMV files that decide its titles; nil for a file.
    let fingerprint: String?

    init(relativePath: String, size: Int64?, modified: Date?, fingerprint: String? = nil) {
        self.relativePath = relativePath
        self.size = size
        self.modified = modified
        self.fingerprint = fingerprint
    }

    var fileName: String {
        relativePath.split(separator: "/").last.map(String.init) ?? ""
    }
}

struct LocationListing: Equatable, Sendable {
    let candidates: [LocationCandidate]
    /// True when the scan stopped before reading everything; the wearer should pick a narrower folder.
    let isTruncated: Bool
}

/// Lists a Location's folder without reading any movie: movie files, ISOs and MKVs by
/// extension, and disc folders by their `BDMV/index.bdmv`. Inspection decides what plays.
enum LocationScanner {
    static let maximumDepth = 4
    static let maximumCandidates = 1_000
    static let fileExtensions: Set<String> = ["mov", "mp4", "m4v", "iso", "mkv"]

    private static let resourceKeys: [URLResourceKey] = [
        .isDirectoryKey, .isSymbolicLinkKey, .fileSizeKey, .contentModificationDateKey,
    ]

    /// The BDMV folders whose files decide a disc's titles, tracks and stereo streams.
    private static let discFingerprintFolders = ["", "PLAYLIST", "CLIPINF", "STREAM", "STREAM/SSIF"]

    /// Throws when the Location's own folder cannot be listed; unreadable subfolders are skipped.
    /// Folders are read breadth first. The scan stops, and says the listing is truncated, when
    /// `budget` runs out or `limit` candidates are found, checked before every entry so one very
    /// wide folder cannot run past either.
    static func scan(
        root: URL,
        budget: Duration = .seconds(15),
        limit: Int = maximumCandidates,
        fileManager: FileManager = .default
    ) throws -> LocationListing {
        if let discRoot = discRoot(of: root, fileManager: fileManager) {
            let values = try? root.resourceValues(forKeys: [.contentModificationDateKey])
            return LocationListing(
                candidates: [LocationCandidate(
                    relativePath: "", size: nil, modified: values?.contentModificationDate,
                    fingerprint: discFingerprint(discRoot, fileManager: fileManager)
                )],
                isTruncated: false
            )
        }
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: budget)
        var candidates: [LocationCandidate] = []
        var isTruncated = false
        var pending: [(url: URL, relativePath: String, depth: Int)] = []

        func mustStop() -> Bool {
            if candidates.count >= limit || clock.now >= deadline {
                isTruncated = true
                return true
            }
            return false
        }

        func visit(_ directory: URL, relativePath: String, depth: Int) throws {
            let entries = try fileManager.contentsOfDirectory(
                at: directory, includingPropertiesForKeys: resourceKeys, options: [.skipsHiddenFiles]
            )
            for entry in entries {
                if mustStop() { return }
                let name = entry.lastPathComponent
                guard !isSkipped(name), let values = try? entry.resourceValues(forKeys: Set(resourceKeys)),
                      values.isSymbolicLink != true
                else { continue }
                let path = relativePath.isEmpty ? name : "\(relativePath)/\(name)"
                if values.isDirectory == true {
                    if let discRoot = discRoot(of: entry, fileManager: fileManager) {
                        candidates.append(LocationCandidate(
                            relativePath: path, size: nil, modified: values.contentModificationDate,
                            fingerprint: discFingerprint(discRoot, fileManager: fileManager)
                        ))
                    } else if depth < maximumDepth {
                        pending.append((entry, path, depth + 1))
                    } else {
                        isTruncated = true
                    }
                } else if fileExtensions.contains(entry.pathExtension.lowercased()) {
                    candidates.append(LocationCandidate(
                        relativePath: path,
                        size: values.fileSize.map(Int64.init),
                        modified: values.contentModificationDate
                    ))
                }
            }
        }

        try visit(root, relativePath: "", depth: 1)
        while !pending.isEmpty, !mustStop() {
            let next = pending.removeFirst()
            try? visit(next.url, relativePath: next.relativePath, depth: next.depth)
        }
        let sorted = candidates.sorted { $0.relativePath.localizedStandardCompare($1.relativePath) == .orderedAscending }
        return LocationListing(candidates: sorted, isTruncated: isTruncated)
    }

    /// The disc root for a disc folder (one that holds `BDMV/index.bdmv`, or `BDMV` itself).
    static func discRoot(of url: URL, fileManager: FileManager = .default) -> URL? {
        if case let .discFolder(root) = Bluray3DSourceOpener.kind(of: url, fileManager: fileManager) { return root }
        return nil
    }

    /// A digest of the names, sizes and dates of the disc's navigation, clip and stream files,
    /// so replacing a playlist or stream is noticed even though the disc folder's date is not.
    static func discFingerprint(_ discRoot: URL, fileManager: FileManager = .default) -> String {
        let bdmv = discRoot.appendingPathComponent("BDMV", isDirectory: true)
        let keys: [URLResourceKey] = [.isRegularFileKey, .fileSizeKey, .contentModificationDateKey]
        var lines: [String] = []
        for folder in discFingerprintFolders {
            let directory = folder.isEmpty ? bdmv : bdmv.appendingPathComponent(folder, isDirectory: true)
            guard let entries = try? fileManager.contentsOfDirectory(
                at: directory, includingPropertiesForKeys: keys, options: [.skipsHiddenFiles]
            ) else { continue }
            for entry in entries {
                guard let values = try? entry.resourceValues(forKeys: Set(keys)), values.isRegularFile == true else {
                    continue
                }
                let modified = values.contentModificationDate?.timeIntervalSinceReferenceDate ?? 0
                lines.append("\(folder)/\(entry.lastPathComponent)|\(values.fileSize ?? -1)|\(modified)")
            }
        }
        let digest = SHA256.hash(data: Data(lines.sorted().joined(separator: "\n").utf8))
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    /// NAS housekeeping folders, such as Synology's `@eaDir` and `#recycle`.
    private static func isSkipped(_ name: String) -> Bool {
        name.hasPrefix("@") || name.hasPrefix("#") || name.hasPrefix(".")
    }
}

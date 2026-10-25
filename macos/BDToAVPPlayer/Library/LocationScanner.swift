import Foundation

/// A file or disc folder in a Location that might be a playable title.
struct LocationCandidate: Equatable, Sendable {
    /// Path inside the Location, "/"-separated; empty when the Location itself is a disc folder.
    let relativePath: String
    let size: Int64?
    let modified: Date?

    var fileName: String {
        relativePath.split(separator: "/").last.map(String.init) ?? ""
    }
}

struct LocationListing: Equatable, Sendable {
    let candidates: [LocationCandidate]
    /// True when the folder held more than the scan reads; the wearer should pick a narrower folder.
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

    /// Throws when the Location's own folder cannot be listed; unreadable subfolders are skipped.
    /// Subfolders are read breadth first until `budget` runs out, then the listing is truncated.
    static func scan(
        root: URL,
        budget: Duration = .seconds(15),
        fileManager: FileManager = .default
    ) throws -> LocationListing {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: budget)
        if isDiscFolder(root, fileManager: fileManager) {
            let values = try? root.resourceValues(forKeys: [.contentModificationDateKey])
            return LocationListing(
                candidates: [LocationCandidate(relativePath: "", size: nil, modified: values?.contentModificationDate)],
                isTruncated: false
            )
        }
        var candidates: [LocationCandidate] = []
        var isTruncated = false
        var pending: [(url: URL, relativePath: String, depth: Int)] = []

        func visit(_ directory: URL, relativePath: String, depth: Int) throws {
            let entries = try fileManager.contentsOfDirectory(
                at: directory, includingPropertiesForKeys: resourceKeys, options: [.skipsHiddenFiles]
            )
            for entry in entries {
                let name = entry.lastPathComponent
                guard !isSkipped(name), let values = try? entry.resourceValues(forKeys: Set(resourceKeys)),
                      values.isSymbolicLink != true
                else { continue }
                let path = relativePath.isEmpty ? name : "\(relativePath)/\(name)"
                if values.isDirectory == true {
                    if isDiscFolder(entry, fileManager: fileManager) {
                        candidates.append(LocationCandidate(
                            relativePath: path, size: nil, modified: values.contentModificationDate
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
        while !pending.isEmpty, candidates.count < maximumCandidates, clock.now < deadline {
            let next = pending.removeFirst()
            try? visit(next.url, relativePath: next.relativePath, depth: next.depth)
        }
        if candidates.count >= maximumCandidates || !pending.isEmpty {
            isTruncated = true
        }
        let sorted = candidates
            .sorted { $0.relativePath.localizedStandardCompare($1.relativePath) == .orderedAscending }
            .prefix(maximumCandidates)
        return LocationListing(candidates: Array(sorted), isTruncated: isTruncated)
    }

    /// A disc root that holds `BDMV/index.bdmv`, or a `BDMV` folder itself.
    static func isDiscFolder(_ url: URL, fileManager: FileManager = .default) -> Bool {
        if case .discFolder = Bluray3DSourceOpener.kind(of: url, fileManager: fileManager) { return true }
        return false
    }

    /// NAS housekeeping folders, such as Synology's `@eaDir` and `#recycle`.
    private static func isSkipped(_ name: String) -> Bool {
        name.hasPrefix("@") || name.hasPrefix("#") || name.hasPrefix(".")
    }
}

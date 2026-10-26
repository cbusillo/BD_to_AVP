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
    /// Subfolders and entries that could not be read, so a partial listing is not shown as complete.
    var unreadableCount = 0
    /// True when the Location's own folder listed nothing at all, even after a second look.
    var folderLooksEmpty = false
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

    /// Throws when the Location's own folder cannot be listed; unreadable subfolders are skipped
    /// and counted. Folders are read breadth first. The scan stops, and says the listing is
    /// truncated, when `budget` runs out or `limit` candidates are found, checked before every
    /// entry so one very wide folder cannot run past either. A Location's folder that lists
    /// nothing is listed once more after `emptyRetryDelay`: on an SMB share the first listing
    /// can come back empty without an error (Apple DTS, developer forums thread 831359).
    static func scan(
        root: URL,
        budget: Duration = .seconds(15),
        limit: Int = maximumCandidates,
        emptyRetryDelay: Duration = .seconds(1),
        fileManager: FileManager = .default,
        listDirectory: ((URL) throws -> [URL])? = nil
    ) throws -> LocationListing {
        let list = listDirectory ?? { try coordinatedContents(of: $0, fileManager: fileManager) }
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
        var unreadableCount = 0
        var pending: [(url: URL, relativePath: String, depth: Int)] = []

        func mustStop() -> Bool {
            if candidates.count >= limit || clock.now >= deadline {
                isTruncated = true
                return true
            }
            return false
        }

        func visit(_ entries: [URL], relativePath: String, depth: Int) {
            for entry in entries {
                if mustStop() { return }
                let name = entry.lastPathComponent
                guard !isSkipped(name) else { continue }
                guard let values = try? entry.resourceValues(forKeys: Set(resourceKeys)) else {
                    unreadableCount += 1
                    continue
                }
                guard values.isSymbolicLink != true else { continue }
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

        var rootEntries = try list(root)
        if rootEntries.isEmpty, clock.now.advanced(by: emptyRetryDelay) < deadline {
            Thread.sleep(forTimeInterval: emptyRetryDelay / .seconds(1))
            rootEntries = try list(root)
        }
        visit(rootEntries, relativePath: "", depth: 1)
        while !pending.isEmpty, !mustStop() {
            let next = pending.removeFirst()
            do {
                visit(try list(next.url), relativePath: next.relativePath, depth: next.depth)
            } catch {
                unreadableCount += 1
            }
        }
        let sorted = candidates.sorted { $0.relativePath.localizedStandardCompare($1.relativePath) == .orderedAscending }
        return LocationListing(
            candidates: sorted, isTruncated: isTruncated,
            unreadableCount: unreadableCount, folderLooksEmpty: rootEntries.isEmpty
        )
    }

    /// Lists a folder inside a coordinated read, as Apple asks for folders picked in Files
    /// ("Providing access to directories"), so the folder's file provider takes part.
    static func coordinatedContents(of directory: URL, fileManager: FileManager = .default) throws -> [URL] {
        var coordinationError: NSError?
        var listed: Result<[URL], Error> = .failure(CocoaError(.fileReadUnknown))
        NSFileCoordinator(filePresenter: nil).coordinate(
            readingItemAt: directory, options: [], error: &coordinationError
        ) { url in
            listed = Result {
                try fileManager.contentsOfDirectory(
                    at: url, includingPropertiesForKeys: resourceKeys, options: [.skipsHiddenFiles]
                )
            }
        }
        if let coordinationError { throw coordinationError }
        return try listed.get()
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

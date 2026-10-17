import Foundation

/// A movie found in a watch folder, with the fingerprint used to tell whether it is still changing.
struct WatchFolderCandidate: Equatable, Sendable {
    let url: URL
    let kind: ConversionSourceKind
    let fingerprint: WatchFolderSourceFingerprint

    var source: ConversionSource {
        ConversionSource(kind: kind, url: url)
    }
}

/// A candidate whose size and modification time stayed unchanged for the settle interval.
struct SettledWatchFolderCandidate: Equatable, Sendable {
    let candidate: WatchFolderCandidate
    let arrivedAt: Date
    let settledAt: Date
}

struct WatchFolderSettleEvaluation: Equatable {
    let settled: [SettledWatchFolderCandidate]
    let pending: [WatchFolderCandidate]
    /// When the earliest pending candidate could next settle; `nil` when nothing is pending.
    let nextEvaluationAt: Date?
}

/// Decides when a file in a watch folder has finished arriving.
///
/// A candidate settles only after two observations at least `settleInterval` apart see the same
/// size and modification time, so a copy in progress (which keeps changing size) is never admitted,
/// even when the copy preserves the original modification time.
struct WatchFolderSettleEvaluator: Equatable {
    static let defaultSettleInterval: TimeInterval = 60

    private struct Observation: Equatable {
        var sizeBytes: Int64
        var modifiedAtMilliseconds: Int64
        let arrivedAt: Date
        var unchangedSince: Date
    }

    let settleInterval: TimeInterval
    private var observations: [String: Observation] = [:]

    init(settleInterval: TimeInterval = defaultSettleInterval) {
        self.settleInterval = max(0, settleInterval)
    }

    var pendingCount: Int { observations.count }

    /// Evaluates the candidates currently present. Candidates missing from this call are forgotten.
    mutating func evaluate(_ candidates: [WatchFolderCandidate], at now: Date) -> WatchFolderSettleEvaluation {
        var settled: [SettledWatchFolderCandidate] = []
        var pending: [WatchFolderCandidate] = []
        var nextObservations: [String: Observation] = [:]
        for candidate in candidates {
            let path = candidate.fingerprint.path
            guard nextObservations[path] == nil else {
                continue
            }
            guard var observation = observations[path] else {
                nextObservations[path] = Observation(
                    sizeBytes: candidate.fingerprint.sizeBytes,
                    modifiedAtMilliseconds: candidate.fingerprint.modifiedAtMilliseconds,
                    arrivedAt: now,
                    unchangedSince: now
                )
                pending.append(candidate)
                continue
            }
            let changed = observation.sizeBytes != candidate.fingerprint.sizeBytes
                || observation.modifiedAtMilliseconds != candidate.fingerprint.modifiedAtMilliseconds
            // A clock that moved backwards cannot prove the file stayed unchanged; start over.
            if changed || now < observation.unchangedSince {
                observation.sizeBytes = candidate.fingerprint.sizeBytes
                observation.modifiedAtMilliseconds = candidate.fingerprint.modifiedAtMilliseconds
                observation.unchangedSince = now
                nextObservations[path] = observation
                pending.append(candidate)
                continue
            }
            if now.timeIntervalSince(observation.unchangedSince) >= settleInterval {
                settled.append(SettledWatchFolderCandidate(
                    candidate: candidate,
                    arrivedAt: observation.arrivedAt,
                    settledAt: now
                ))
            } else {
                nextObservations[path] = observation
                pending.append(candidate)
            }
        }
        observations = nextObservations
        return WatchFolderSettleEvaluation(settled: settled, pending: pending, nextEvaluationAt: nextEvaluationAt)
    }

    /// When the earliest pending candidate could next settle; `nil` when nothing is pending.
    var nextEvaluationAt: Date? {
        observations.values
            .map { $0.unchangedSince.addingTimeInterval(settleInterval) }
            .min()
    }

    /// Puts settled candidates that turned out to be still in use back to pending, keeping their
    /// arrival time, so they must stay unchanged for another full interval after `now`.
    mutating func restartSettling(_ candidates: [SettledWatchFolderCandidate], at now: Date) {
        for settled in candidates {
            observations[settled.candidate.fingerprint.path] = Observation(
                sizeBytes: settled.candidate.fingerprint.sizeBytes,
                modifiedAtMilliseconds: settled.candidate.fingerprint.modifiedAtMilliseconds,
                arrivedAt: settled.arrivedAt,
                unchangedSince: now
            )
        }
    }

    /// Forgets every observation, for example when the folder's drive goes offline.
    mutating func reset() {
        observations.removeAll()
    }
}

/// Finds movie sources in a watch folder, skipping names that indicate an unfinished or temporary file.
enum WatchFolderCandidateScanner {
    static let maximumSubfolderDepth = 4

    private static let temporaryExtensions: Set<String> = [
        "part", "partial", "tmp", "temp", "crdownload", "download", "filepart", "inprogress",
    ]
    private static let discStructureNames: Set<String> = ["bdmv", "certificate"]

    static func isIgnoredName(_ name: String) -> Bool {
        guard !name.isEmpty, !name.hasPrefix("."), !name.hasSuffix("~") else {
            return true
        }
        let pathExtension = (name as NSString).pathExtension.lowercased()
        return temporaryExtensions.contains(pathExtension)
    }

    /// Lists candidates directly in `directory`, and in its subfolders when `includesSubfolders` is set.
    ///
    /// Blu-ray folders are reported whole; the scan never descends into one, so their stream files
    /// are not mistaken for separate movies.
    static func candidates(
        in directory: URL,
        includesSubfolders: Bool,
        fileManager: FileManager = .default
    ) throws -> [WatchFolderCandidate] {
        var results: [WatchFolderCandidate] = []
        try collect(
            in: directory.standardizedFileURL,
            depth: 0,
            includesSubfolders: includesSubfolders,
            fileManager: fileManager,
            into: &results
        )
        return results.sorted { $0.fingerprint.path < $1.fingerprint.path }
    }

    private static let entryKeys: [URLResourceKey] = [
        .isDirectoryKey, .isSymbolicLinkKey, .isPackageKey, .fileSizeKey, .contentModificationDateKey,
    ]

    private static func collect(
        in directory: URL,
        depth: Int,
        includesSubfolders: Bool,
        fileManager: FileManager,
        into results: inout [WatchFolderCandidate]
    ) throws {
        let entries = try fileManager.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: entryKeys,
            options: [.skipsHiddenFiles]
        )
        for entry in entries {
            let name = entry.lastPathComponent
            guard !isIgnoredName(name),
                  let values = try? entry.resourceValues(forKeys: Set(entryKeys)),
                  values.isSymbolicLink != true
            else {
                continue
            }
            if values.isDirectory == true {
                guard values.isPackage != true, !discStructureNames.contains(name.lowercased()) else {
                    continue
                }
                if DiscSourceDetector.isBluRayFolder(entry, fileManager: fileManager) {
                    if let fingerprint = folderFingerprint(entry, fileManager: fileManager) {
                        results.append(WatchFolderCandidate(url: entry.standardizedFileURL, kind: .bluRayFolder, fingerprint: fingerprint))
                    }
                } else if includesSubfolders, depth < maximumSubfolderDepth {
                    try? collect(
                        in: entry,
                        depth: depth + 1,
                        includesSubfolders: includesSubfolders,
                        fileManager: fileManager,
                        into: &results
                    )
                }
                continue
            }
            guard let source = ConversionSource.infer(from: entry, fileManager: fileManager),
                  source.kind != .sourceFolder,
                  source.kind != .bluRayFolder,
                  let size = values.fileSize,
                  let modifiedAt = values.contentModificationDate
            else {
                continue
            }
            results.append(WatchFolderCandidate(
                url: source.url,
                kind: source.kind,
                fingerprint: WatchFolderSourceFingerprint(
                    path: source.url.path,
                    sizeBytes: Int64(size),
                    modifiedAt: modifiedAt
                )
            ))
        }
    }

    /// Sums sizes and takes the newest modification time of everything inside a Blu-ray folder,
    /// so a folder still being copied keeps changing until the copy finishes.
    private static func folderFingerprint(_ folder: URL, fileManager: FileManager) -> WatchFolderSourceFingerprint? {
        let keys: [URLResourceKey] = [.fileSizeKey, .contentModificationDateKey, .isDirectoryKey]
        guard let enumerator = fileManager.enumerator(at: folder, includingPropertiesForKeys: keys),
              var newest = (try? folder.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate
        else {
            return nil
        }
        var totalSize: Int64 = 0
        var entryCount: Int64 = 0
        for case let entry as URL in enumerator {
            guard let values = try? entry.resourceValues(forKeys: Set(keys)) else {
                continue
            }
            entryCount += 1
            if let modifiedAt = values.contentModificationDate, modifiedAt > newest {
                newest = modifiedAt
            }
            if values.isDirectory != true, let size = values.fileSize {
                totalSize += Int64(size)
            }
        }
        // Folding in the entry count catches an added empty file that leaves size unchanged.
        return WatchFolderSourceFingerprint(
            path: folder.standardizedFileURL.path,
            sizeBytes: totalSize &+ entryCount,
            modifiedAt: newest
        )
    }
}

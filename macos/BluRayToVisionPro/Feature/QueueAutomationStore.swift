import Foundation

/// What happens to a source once it settles in a watch folder.
enum WatchFolderAdmissionMode: String, Codable, CaseIterable, Equatable {
    /// Add it to the queue held for review; it never starts until someone releases it.
    case holdForReview
    /// Add it to the queue as waiting, ready for the queue to start it.
    case startAutomatically
}

/// A folder whose new movies are admitted to the queue automatically.
struct WatchFolder: Codable, Identifiable, Equatable {
    let id: UUID
    var bookmark: Data
    var displayName: String
    var mode: WatchFolderAdmissionMode
    /// The profile for admitted sources; `nil` follows the app's default profile.
    var profileID: String?
    var includesSubfolders: Bool
    var isEnabled: Bool

    init(
        id: UUID = UUID(),
        bookmark: Data,
        displayName: String,
        mode: WatchFolderAdmissionMode = .holdForReview,
        profileID: String? = nil,
        includesSubfolders: Bool = false,
        isEnabled: Bool = true
    ) {
        self.id = id
        self.bookmark = bookmark
        self.displayName = displayName
        self.mode = mode
        self.profileID = profileID
        self.includesSubfolders = includesSubfolders
        self.isEnabled = isEnabled
    }

    private enum CodingKeys: String, CodingKey {
        case id, bookmark, displayName, mode, profileID, includesSubfolders, isEnabled
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(UUID.self, forKey: .id)
        bookmark = try values.decode(Data.self, forKey: .bookmark)
        displayName = try values.decode(String.self, forKey: .displayName)
        mode = try values.decodeIfPresent(WatchFolderAdmissionMode.self, forKey: .mode) ?? .holdForReview
        profileID = try values.decodeIfPresent(String.self, forKey: .profileID)
        includesSubfolders = try values.decodeIfPresent(Bool.self, forKey: .includesSubfolders) ?? false
        isEnabled = try values.decodeIfPresent(Bool.self, forKey: .isEnabled) ?? true
    }
}

/// Identifies one version of a source file or Blu-ray folder by location, size, and modification time.
///
/// Modification time is kept in whole milliseconds so it survives a JSON round trip exactly.
struct WatchFolderSourceFingerprint: Codable, Hashable, Sendable {
    let path: String
    let sizeBytes: Int64
    let modifiedAtMilliseconds: Int64

    init(path: String, sizeBytes: Int64, modifiedAtMilliseconds: Int64) {
        self.path = path
        self.sizeBytes = sizeBytes
        self.modifiedAtMilliseconds = modifiedAtMilliseconds
    }

    init(path: String, sizeBytes: Int64, modifiedAt: Date) {
        self.init(
            path: path,
            sizeBytes: sizeBytes,
            modifiedAtMilliseconds: Int64((modifiedAt.timeIntervalSince1970 * 1_000).rounded())
        )
    }
}

/// A source a watch folder already admitted or offered, so removing its queue item does not re-admit it.
struct WatchFolderSeenSource: Codable, Equatable {
    let watchFolderID: UUID
    let fingerprint: WatchFolderSourceFingerprint
    let recordedAt: Date
}

struct QueueAutomationDocument: Codable, Equatable {
    static let currentVersion = 1

    let version: Int
    var watchFolders: [WatchFolder]
    var seenSources: [WatchFolderSeenSource]

    init(
        version: Int = currentVersion,
        watchFolders: [WatchFolder] = [],
        seenSources: [WatchFolderSeenSource] = []
    ) {
        self.version = version
        self.watchFolders = watchFolders
        self.seenSources = seenSources
    }

    private enum CodingKeys: String, CodingKey { case version, watchFolders, seenSources }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let version = try values.decode(Int.self, forKey: .version)
        guard version == Self.currentVersion else {
            throw QueueAutomationStoreError.unsupportedVersion(version)
        }
        self.init(
            version: version,
            watchFolders: try values.decodeIfPresent([WatchFolder].self, forKey: .watchFolders) ?? [],
            seenSources: try values.decodeIfPresent([WatchFolderSeenSource].self, forKey: .seenSources) ?? []
        )
    }
}

enum QueueAutomationStoreError: LocalizedError, Equatable {
    case unknownWatchFolder
    case alreadyWatched
    case unsupportedVersion(Int)
    case writesBlocked
    case writeFailed

    var errorDescription: String? {
        switch self {
        case .unknownWatchFolder:
            "That watch folder is no longer configured."
        case .alreadyWatched:
            "That folder is already a watch folder."
        case let .unsupportedVersion(version):
            "Watch folder data version \(version) is not supported."
        case .writesBlocked:
            "Watch folder changes are disabled because the saved settings could not be loaded safely."
        case .writeFailed:
            "Watch folder settings could not be saved."
        }
    }
}

/// Persists watch folders and the sources they have already admitted.
///
/// The data lives in a versioned JSON file next to `queue.json`, so a separate helper process can
/// read the same configuration the app uses.
@MainActor
final class QueueAutomationStore: ObservableObject {
    static let maximumSeenSources = 10_000

    @Published private(set) var document: QueueAutomationDocument
    @Published private(set) var loadErrorMessage: String?
    @Published private(set) var writesBlocked = false

    private let fileURL: URL?
    private let fileManager: FileManager
    private let dataWriter: (Data, URL) throws -> Void

    init(
        fileURL: URL? = nil,
        fileManager: FileManager = .default,
        dataReader: (URL) throws -> Data = { try Data(contentsOf: $0) },
        dataWriter: @escaping (Data, URL) throws -> Void = { data, url in
            try data.write(to: url, options: .atomic)
        },
        inMemory: Bool = false
    ) {
        self.fileManager = fileManager
        self.dataWriter = dataWriter
        self.fileURL = inMemory ? nil : (fileURL ?? Self.defaultFileURL(fileManager: fileManager))
        document = QueueAutomationDocument()
        if let fileURL = self.fileURL, fileManager.fileExists(atPath: fileURL.path) {
            do {
                document = try Self.decoder().decode(QueueAutomationDocument.self, from: dataReader(fileURL))
            } catch let error as QueueAutomationStoreError {
                writesBlocked = true
                loadErrorMessage = error.localizedDescription
            } catch {
                writesBlocked = true
                loadErrorMessage = "Watch folder settings could not be loaded. Changes are disabled to avoid overwriting them."
            }
        }
    }

    static func inMemory() -> QueueAutomationStore {
        QueueAutomationStore(inMemory: true)
    }

    var watchFolders: [WatchFolder] { document.watchFolders }
    var seenSources: [WatchFolderSeenSource] { document.seenSources }

    func watchFolder(id: UUID) -> WatchFolder? {
        document.watchFolders.first { $0.id == id }
    }

    /// Adds a watch folder, or replaces the saved one with the same identifier.
    func saveWatchFolder(_ folder: WatchFolder) throws {
        try mutate { document in
            if let index = document.watchFolders.firstIndex(where: { $0.id == folder.id }) {
                document.watchFolders[index] = folder
            } else {
                document.watchFolders.append(folder)
            }
        }
    }

    /// Removes a watch folder and forgets the sources it admitted.
    func removeWatchFolder(id: UUID) throws {
        guard watchFolder(id: id) != nil else {
            throw QueueAutomationStoreError.unknownWatchFolder
        }
        try mutate { document in
            document.watchFolders.removeAll { $0.id == id }
            document.seenSources.removeAll { $0.watchFolderID == id }
        }
    }

    func hasSeen(_ fingerprint: WatchFolderSourceFingerprint) -> Bool {
        document.seenSources.contains { $0.fingerprint == fingerprint }
    }

    func recordSeen(_ fingerprints: [WatchFolderSourceFingerprint], watchFolderID: UUID, at date: Date) throws {
        let newFingerprints = fingerprints.filter { !hasSeen($0) }
        guard !newFingerprints.isEmpty else {
            return
        }
        try mutate { document in
            var seen = document.seenSources
            for fingerprint in Set(newFingerprints) {
                seen.append(WatchFolderSeenSource(watchFolderID: watchFolderID, fingerprint: fingerprint, recordedAt: date))
            }
            if seen.count > Self.maximumSeenSources {
                seen.sort { $0.recordedAt < $1.recordedAt }
                seen.removeFirst(seen.count - Self.maximumSeenSources)
            }
            document.seenSources = seen
        }
    }

    /// Forgets every recorded version of the source at `path`, so the next scan may admit it again.
    func forgetSeenSource(path: String) throws {
        try mutate { document in
            document.seenSources.removeAll { $0.fingerprint.path == path }
        }
    }

    /// Forgets every source a watch folder has recorded, so the next scan reconsiders its contents.
    func forgetSeenSources(watchFolderID: UUID) throws {
        try mutate { document in
            document.seenSources.removeAll { $0.watchFolderID == watchFolderID }
        }
    }

    private func mutate(_ mutation: (inout QueueAutomationDocument) throws -> Void) throws {
        guard !writesBlocked else {
            throw QueueAutomationStoreError.writesBlocked
        }
        var nextDocument = document
        try mutation(&nextDocument)
        guard nextDocument != document else {
            return
        }
        if let fileURL {
            do {
                try fileManager.createDirectory(
                    at: fileURL.deletingLastPathComponent(),
                    withIntermediateDirectories: true
                )
                try dataWriter(Self.encoder().encode(nextDocument), fileURL)
            } catch {
                throw QueueAutomationStoreError.writeFailed
            }
        }
        document = nextDocument
    }

    static func defaultFileURL(fileManager: FileManager = .default) -> URL {
        let applicationSupportURL = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        return applicationSupportURL
            .appendingPathComponent("3D Blu-ray to Vision Pro", isDirectory: true)
            .appendingPathComponent("queue.automation.json")
    }

    private static func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }

    private static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}

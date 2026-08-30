import Foundation

enum ResumeStoreError: Error, Equatable {
    case invalidIdentifier
    case invalidPosition
    case exceedsCapacity
}

final class ResumeStore {
    struct Entry: Codable, Equatable, Sendable {
        let position: TimeInterval
        let updatedAt: Date
    }

    private struct Document: Codable {
        var entries: [String: Entry]
    }

    private let storageURL: URL
    private let fileManager: FileManager
    private let maxEntries: Int
    private let maxFileBytes: Int
    private let now: () -> Date
    private var entries: [String: Entry]

    init(
        storageURL: URL = ResumeStore.defaultStorageURL(),
        maxEntries: Int = 256,
        maxFileBytes: Int = 64 * 1024,
        fileManager: FileManager = .default,
        now: @escaping () -> Date = Date.init
    ) {
        self.storageURL = storageURL
        self.fileManager = fileManager
        self.maxEntries = max(1, maxEntries)
        self.maxFileBytes = max(1, maxFileBytes)
        self.now = now
        self.entries = Self.load(
            from: storageURL,
            maxEntries: max(1, maxEntries),
            maxFileBytes: max(1, maxFileBytes),
            fileManager: fileManager
        )
    }

    static func defaultStorageURL(fileManager: FileManager = .default) -> URL {
        let directory = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("BDToAVPPlayer", isDirectory: true)
        return directory.appendingPathComponent("resume.json")
    }

    func resumeTime(for id: String) -> TimeInterval? {
        entries[id]?.position
    }

    func setResumeTime(_ position: TimeInterval, for id: String) throws {
        guard !id.isEmpty, id.count <= 512 else {
            throw ResumeStoreError.invalidIdentifier
        }
        guard position.isFinite, position >= 0 else {
            throw ResumeStoreError.invalidPosition
        }

        entries[id] = Entry(position: position, updatedAt: now())
        try persist()
    }

    func remove(id: String) throws {
        entries.removeValue(forKey: id)
        try persist()
    }

    private func persist() throws {
        var candidate = entries
        while true {
            let data = try encode(candidate)
            if candidate.count <= maxEntries, data.count <= maxFileBytes {
                entries = candidate
                let directory = storageURL.deletingLastPathComponent()
                try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
                try data.write(to: storageURL, options: .atomic)
                return
            }
            guard let oldestID = candidate.min(by: { $0.value.updatedAt < $1.value.updatedAt })?.key else {
                throw ResumeStoreError.exceedsCapacity
            }
            candidate.removeValue(forKey: oldestID)
        }
    }

    private func encode(_ entries: [String: Entry]) throws -> Data {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(Document(entries: entries))
    }

    private static func load(
        from storageURL: URL,
        maxEntries: Int,
        maxFileBytes: Int,
        fileManager: FileManager
    ) -> [String: Entry] {
        guard let data = fileManager.contents(atPath: storageURL.path), data.count <= maxFileBytes else {
            return [:]
        }
        do {
            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .iso8601
            let document = try decoder.decode(Document.self, from: data)
            let validEntries = document.entries.filter {
                !$0.key.isEmpty && $0.key.count <= 512 && $0.value.position.isFinite && $0.value.position >= 0
            }
            return Dictionary(
                uniqueKeysWithValues: validEntries
                    .sorted { $0.value.updatedAt > $1.value.updatedAt }
                    .prefix(maxEntries)
                    .map { ($0.key, $0.value) }
            )
        } catch {
            return [:]
        }
    }
}

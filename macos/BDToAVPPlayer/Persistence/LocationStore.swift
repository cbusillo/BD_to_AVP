import Foundation

/// A network share or folder the wearer added under Locations. Its folder bookmark lives in
/// `BookmarkStore`; this record keeps its name and what each file held when last inspected,
/// so reopening a Location does not read every movie over the network again.
struct StoredLocation: Codable, Equatable, Identifiable, Sendable {
    let id: UUID
    var name: String
    var inspectedFiles: [InspectedFile]
}

struct InspectedFile: Codable, Equatable, Sendable {
    let relativePath: String
    let size: Int64?
    let modified: Date?
    /// A disc folder's BDMV digest; see `LocationScanner.discFingerprint`.
    let fingerprint: String?
    let inspection: MediaSourceInspection

    func matches(_ candidate: LocationCandidate) -> Bool {
        relativePath == candidate.relativePath && size == candidate.size && modified == candidate.modified
            && fingerprint == candidate.fingerprint
            && inspection.readerVersion == MediaSourceInspection.currentReaderVersion
    }
}

final class LocationStore {
    private struct Document: Codable {
        var locations: [StoredLocation]
    }

    private let storageURL: URL
    private let fileManager: FileManager

    init(storageURL: URL = LocationStore.defaultStorageURL(), fileManager: FileManager = .default) {
        self.storageURL = storageURL
        self.fileManager = fileManager
    }

    static func defaultStorageURL(fileManager: FileManager = .default) -> URL {
        fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("BDToAVPPlayer", isDirectory: true)
            .appendingPathComponent("locations.json")
    }

    func load() -> [StoredLocation] {
        guard let data = fileManager.contents(atPath: storageURL.path),
              let document = try? JSONDecoder().decode(Document.self, from: data)
        else {
            return []
        }
        return document.locations
    }

    func save(_ locations: [StoredLocation]) throws {
        let data = try JSONEncoder().encode(Document(locations: locations))
        try fileManager.createDirectory(at: storageURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: storageURL, options: .atomic)
    }
}

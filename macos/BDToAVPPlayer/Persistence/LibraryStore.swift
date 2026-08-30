import Foundation

final class LibraryStore {
    private struct Document: Codable {
        var items: [MediaItem]
    }

    private let storageURL: URL
    private let fileManager: FileManager

    init(storageURL: URL = LibraryStore.defaultStorageURL(), fileManager: FileManager = .default) {
        self.storageURL = storageURL
        self.fileManager = fileManager
    }

    static func defaultStorageURL(fileManager: FileManager = .default) -> URL {
        let directory = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("BDToAVPPlayer", isDirectory: true)
        return directory.appendingPathComponent("library.json")
    }

    func load() -> [MediaItem] {
        guard let data = fileManager.contents(atPath: storageURL.path) else {
            return []
        }

        do {
            let document = try JSONDecoder().decode(Document.self, from: data)
            return deduplicated(document.items)
        } catch {
            return []
        }
    }

    func save(_ items: [MediaItem]) throws {
        let data = try JSONEncoder().encode(Document(items: deduplicated(items)))
        let directory = storageURL.deletingLastPathComponent()
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        try data.write(to: storageURL, options: .atomic)
    }

    func upsert(_ item: MediaItem) throws {
        var items = load()
        if let index = items.firstIndex(where: { $0.id == item.id }) {
            items[index] = item
        } else {
            items.append(item)
        }
        try save(items)
    }

    func remove(id: String) throws {
        try save(load().filter { $0.id != id })
    }

    private func deduplicated(_ items: [MediaItem]) -> [MediaItem] {
        var seen = Set<String>()
        return items.filter { seen.insert($0.id).inserted }
    }
}

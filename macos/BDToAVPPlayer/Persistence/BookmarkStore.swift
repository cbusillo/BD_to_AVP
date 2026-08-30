import Foundation

enum BookmarkStoreError: Error, Equatable {
    case invalidIdentifier
    case missingBookmark(String)
    case staleBookmark(String)
    case invalidBookmark(String)
    case missingResource(URL)
}

final class SecurityScopedResourceLease {
    let url: URL

    private var isActive: Bool
    private let stopAccessing: () -> Void

    init(url: URL) {
        self.url = url
        self.isActive = url.startAccessingSecurityScopedResource()
        self.stopAccessing = {
            url.stopAccessingSecurityScopedResource()
        }
    }

    init(url: URL, startAccessing: () -> Bool, stopAccessing: @escaping () -> Void) {
        self.url = url
        self.isActive = startAccessing()
        self.stopAccessing = stopAccessing
    }

    func close() {
        guard isActive else { return }
        isActive = false
        stopAccessing()
    }

    deinit {
        close()
    }
}

final class BookmarkStore {
    private struct Document: Codable {
        var bookmarks: [String: Data]
    }

    private let storageURL: URL
    private let fileManager: FileManager
    private var bookmarks: [String: Data]

    init(storageURL: URL = BookmarkStore.defaultStorageURL(), fileManager: FileManager = .default) {
        self.storageURL = storageURL
        self.fileManager = fileManager
        self.bookmarks = (try? Self.decodeBookmarks(fileManager.contents(atPath: storageURL.path) ?? Data())) ?? [:]
    }

    static func defaultStorageURL(fileManager: FileManager = .default) -> URL {
        let directory = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("BDToAVPPlayer", isDirectory: true)
        return directory.appendingPathComponent("bookmarks.json")
    }

    static func encodeBookmarks(_ bookmarks: [String: Data]) throws -> Data {
        try JSONEncoder().encode(Document(bookmarks: bookmarks))
    }

    static func decodeBookmarks(_ data: Data) throws -> [String: Data] {
        try JSONDecoder().decode(Document.self, from: data).bookmarks
    }

    func save(url: URL, for id: String) throws {
        let data = try url.bookmarkData(options: [])
        try save(bookmarkData: data, for: id)
    }

    func save(bookmarkData: Data, for id: String) throws {
        guard !id.isEmpty, id.count <= 512 else { throw BookmarkStoreError.invalidIdentifier }
        bookmarks[id] = bookmarkData
        try persist()
    }

    func bookmarkData(for id: String) -> Data? {
        bookmarks[id]
    }

    func remove(id: String) throws {
        bookmarks.removeValue(forKey: id)
        try persist()
    }

    func resolve(id: String) throws -> URL {
        guard let bookmarkData = bookmarks[id] else {
            throw BookmarkStoreError.missingBookmark(id)
        }

        var isStale = false
        let resolvedURL: URL
        do {
            resolvedURL = try URL(
                resolvingBookmarkData: bookmarkData,
                options: [],
                relativeTo: nil,
                bookmarkDataIsStale: &isStale
            )
        } catch {
            throw BookmarkStoreError.invalidBookmark(id)
        }

        if isStale {
            throw BookmarkStoreError.staleBookmark(id)
        }
        return resolvedURL
    }

    func open(id: String) throws -> SecurityScopedResourceLease {
        let url = try resolve(id: id)
        let lease = SecurityScopedResourceLease(url: url)
        guard fileManager.fileExists(atPath: url.path) else {
            lease.close()
            throw BookmarkStoreError.missingResource(url)
        }
        return lease
    }

    func withResolvedURL<T>(for id: String, _ body: (URL) throws -> T) throws -> T {
        let lease = try open(id: id)
        defer { lease.close() }
        return try body(lease.url)
    }

    private func persist() throws {
        let data = try Self.encodeBookmarks(bookmarks)
        let directory = storageURL.deletingLastPathComponent()
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        try data.write(to: storageURL, options: .atomic)
    }
}

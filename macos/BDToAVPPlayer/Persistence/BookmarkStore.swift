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
    private let resolveBookmark: (Data) throws -> (URL, Bool)
    private let bookmarkDataForURL: (URL) throws -> Data
    private let makeLease: (URL) -> SecurityScopedResourceLease

    init(
        storageURL: URL = BookmarkStore.defaultStorageURL(),
        fileManager: FileManager = .default,
        resolveBookmark: @escaping (Data) throws -> (URL, Bool) = { data in
            var isStale = false
            let url = try URL(
                resolvingBookmarkData: data,
                options: [],
                relativeTo: nil,
                bookmarkDataIsStale: &isStale
            )
            return (url, isStale)
        },
        bookmarkDataForURL: @escaping (URL) throws -> Data = { url in
            try url.bookmarkData(options: [])
        },
        makeLease: @escaping (URL) -> SecurityScopedResourceLease = { url in
            SecurityScopedResourceLease(url: url)
        }
    ) {
        self.storageURL = storageURL
        self.fileManager = fileManager
        self.bookmarks = (try? Self.decodeBookmarks(fileManager.contents(atPath: storageURL.path) ?? Data())) ?? [:]
        self.resolveBookmark = resolveBookmark
        self.bookmarkDataForURL = bookmarkDataForURL
        self.makeLease = makeLease
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

        do {
            let (resolvedURL, isStale) = try resolveBookmark(bookmarkData)
            if isStale {
                throw BookmarkStoreError.staleBookmark(id)
            }
            return resolvedURL
        } catch {
            if error is BookmarkStoreError {
                throw error
            }
            throw BookmarkStoreError.invalidBookmark(id)
        }
    }

    func open(id: String) throws -> SecurityScopedResourceLease {
        guard let bookmarkData = bookmarks[id] else {
            throw BookmarkStoreError.missingBookmark(id)
        }

        let resolvedURL: URL
        let isStale: Bool
        do {
            (resolvedURL, isStale) = try resolveBookmark(bookmarkData)
        } catch {
            throw BookmarkStoreError.invalidBookmark(id)
        }

        let lease = makeLease(resolvedURL)
        do {
            guard fileManager.fileExists(atPath: resolvedURL.path) else {
                throw BookmarkStoreError.missingResource(resolvedURL)
            }
            if isStale {
                try save(bookmarkData: bookmarkDataForURL(resolvedURL), for: id)
            }
            return lease
        } catch {
            lease.close()
            if isStale {
                throw BookmarkStoreError.staleBookmark(id)
            }
            throw error
        }
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

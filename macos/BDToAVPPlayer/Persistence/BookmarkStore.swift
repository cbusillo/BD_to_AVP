import Foundation

enum BookmarkStoreError: Error, Equatable {
    case invalidIdentifier
    case missingBookmark(String)
    case staleBookmark(String)
    case invalidBookmark(String)
    case missingResource(URL)
}

final class SecurityScopedResourceLease: @unchecked Sendable {
    let url: URL

    private let lock = NSLock()
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
        lock.lock()
        let shouldStop = isActive
        isActive = false
        lock.unlock()
        if shouldStop {
            stopAccessing()
        }
    }

    deinit {
        close()
    }
}

@MainActor
final class BookmarkStore {
    private struct Document: Codable {
        var bookmarks: [String: Data]
    }

    private let storageURL: URL
    private let fileManager: FileManager
    private var bookmarks: [String: Data]
    private let access: BookmarkAccess

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
        },
        resourceExists: @escaping (URL) -> Bool = { url in
            FileManager.default.fileExists(atPath: url.path)
        }
    ) {
        self.storageURL = storageURL
        self.fileManager = fileManager
        self.bookmarks = (try? Self.decodeBookmarks(fileManager.contents(atPath: storageURL.path) ?? Data())) ?? [:]
        self.access = BookmarkAccess(
            resolveBookmark: resolveBookmark,
            bookmarkDataForURL: bookmarkDataForURL,
            makeLease: makeLease,
            resourceExists: resourceExists
        )
    }

    nonisolated static func defaultStorageURL(fileManager: FileManager = .default) -> URL {
        let directory = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("BDToAVPPlayer", isDirectory: true)
        return directory.appendingPathComponent("bookmarks.json")
    }

    nonisolated static func encodeBookmarks(_ bookmarks: [String: Data]) throws -> Data {
        try JSONEncoder().encode(Document(bookmarks: bookmarks))
    }

    nonisolated static func decodeBookmarks(_ data: Data) throws -> [String: Data] {
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

    func open(id: String) async throws -> SecurityScopedResourceLease {
        guard let bookmarkData = bookmarks[id] else {
            throw BookmarkStoreError.missingBookmark(id)
        }

        try Task.checkCancellation()
        let openTask = Task.detached(priority: .userInitiated) { [access] in
            try access.open(bookmarkData: bookmarkData)
        }
        let opened: OpenedBookmark
        do {
            opened = try await withTaskCancellationHandler {
                try await openTask.value
            } onCancel: {
                openTask.cancel()
            }
        } catch {
            if error is CancellationError {
                throw error
            }
            if let bookmarkError = error as? BookmarkStoreError {
                switch bookmarkError {
                case .invalidBookmark:
                    throw BookmarkStoreError.invalidBookmark(id)
                case .staleBookmark:
                    throw BookmarkStoreError.staleBookmark(id)
                default:
                    throw bookmarkError
                }
            }
            throw BookmarkStoreError.invalidBookmark(id)
        }

        if let refreshedBookmarkData = opened.refreshedBookmarkData {
            guard bookmarks[id] == bookmarkData else {
                return opened.lease
            }
            do {
                try save(bookmarkData: refreshedBookmarkData, for: id)
            } catch {
                return opened.lease
            }
        }
        return opened.lease
    }

    private func persist() throws {
        let data = try Self.encodeBookmarks(bookmarks)
        let directory = storageURL.deletingLastPathComponent()
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        try data.write(to: storageURL, options: .atomic)
    }
}

private struct OpenedBookmark: @unchecked Sendable {
    let lease: SecurityScopedResourceLease
    let refreshedBookmarkData: Data?
}

private final class BookmarkAccess: @unchecked Sendable {
    private let resolveBookmark: (Data) throws -> (URL, Bool)
    private let bookmarkDataForURL: (URL) throws -> Data
    private let makeLease: (URL) -> SecurityScopedResourceLease
    private let resourceExists: (URL) -> Bool

    init(
        resolveBookmark: @escaping (Data) throws -> (URL, Bool),
        bookmarkDataForURL: @escaping (URL) throws -> Data,
        makeLease: @escaping (URL) -> SecurityScopedResourceLease,
        resourceExists: @escaping (URL) -> Bool
    ) {
        self.resolveBookmark = resolveBookmark
        self.bookmarkDataForURL = bookmarkDataForURL
        self.makeLease = makeLease
        self.resourceExists = resourceExists
    }

    func open(bookmarkData: Data) throws -> OpenedBookmark {
        try Task.checkCancellation()

        let resolvedURL: URL
        let isStale: Bool
        do {
            (resolvedURL, isStale) = try resolveBookmark(bookmarkData)
        } catch {
            throw BookmarkStoreError.invalidBookmark("")
        }

        try Task.checkCancellation()
        let lease = makeLease(resolvedURL)
        do {
            try Task.checkCancellation()
            guard resourceExists(resolvedURL) else {
                throw BookmarkStoreError.missingResource(resolvedURL)
            }
            let refreshedBookmarkData = isStale ? try bookmarkDataForURL(resolvedURL) : nil
            try Task.checkCancellation()
            return OpenedBookmark(
                lease: lease,
                refreshedBookmarkData: refreshedBookmarkData
            )
        } catch {
            lease.close()
            if error is CancellationError || error is BookmarkStoreError {
                throw error
            }
            throw BookmarkStoreError.staleBookmark("")
        }
    }
}

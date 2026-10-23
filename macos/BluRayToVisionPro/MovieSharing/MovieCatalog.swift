import Darwin
import Foundation

struct MovieSourceBookmark: Codable, Identifiable, Equatable, Sendable {
    let id: String
    let name: String
    let bookmark: Data
}

final class MovieRootDirectory: @unchecked Sendable {
    let source: MovieSourceBookmark
    let url: URL
    let descriptor: Int32
    private let accessing: Bool
    private let accessURL: URL

    init(source: MovieSourceBookmark, url: URL) throws {
        self.source = source
        accessURL = url
        accessing = url.startAccessingSecurityScopedResource()
        descriptor = Darwin.open(url.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else {
            if accessing { url.stopAccessingSecurityScopedResource() }
            throw MovieLibraryError.unavailable
        }
        // Foundation deliberately shortens /private/var to /var when resolving
        // symlinks, while its enumerator returns /private/var. Use the opened
        // directory's kernel path so both sides use the same path components.
        var path = [CChar](repeating: 0, count: Int(MAXPATHLEN))
        guard fcntl(descriptor, F_GETPATH, &path) == 0 else {
            Darwin.close(descriptor)
            if accessing { url.stopAccessingSecurityScopedResource() }
            throw MovieLibraryError.unavailable
        }
        self.url = URL(fileURLWithPath: String(decoding: path.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self), isDirectory: true)
    }

    deinit {
        Darwin.close(descriptor)
        if accessing { accessURL.stopAccessingSecurityScopedResource() }
    }

    func openFile(_ components: [String]) throws -> Int32 {
        switch openFileReportingErrno(components) {
        case let .success(descriptor): return descriptor
        case .failure: throw MovieLibraryError.sourceChanged
        }
    }

    /// Opens a file below the root one component at a time, never following a link. The
    /// failure carries the `errno` of the step that failed.
    func openFileReportingErrno(_ components: [String]) -> Result<Int32, MovieFileAccessError> {
        guard Self.areSafe(components) else { return .failure(MovieFileAccessError(code: ENOENT)) }
        var parent = Darwin.dup(descriptor)
        guard parent >= 0 else { return .failure(MovieFileAccessError(code: errno)) }
        for (index, component) in components.enumerated() {
            let flags = O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK | (index == components.count - 1 ? 0 : O_DIRECTORY)
            let next = Darwin.openat(parent, component, flags)
            let code = errno
            Darwin.close(parent)
            guard next >= 0 else { return .failure(MovieFileAccessError(code: code)) }
            parent = next
        }
        return .success(parent)
    }

    /// `lstat` of a regular file below the root, walking directories without following links.
    /// Listing ISOs costs this, not an open.
    func statFile(_ components: [String]) -> stat? {
        guard Self.areSafe(components) else { return nil }
        var parent = Darwin.dup(descriptor)
        guard parent >= 0 else { return nil }
        defer { Darwin.close(parent) }
        for component in components.dropLast() {
            let next = Darwin.openat(parent, component, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_DIRECTORY)
            guard next >= 0 else { return nil }
            Darwin.close(parent)
            parent = next
        }
        var info = stat()
        guard let name = components.last, Darwin.fstatat(parent, name, &info, AT_SYMLINK_NOFOLLOW) == 0,
              (info.st_mode & S_IFMT) == S_IFREG, info.st_size > 0
        else { return nil }
        return info
    }

    private static func areSafe(_ components: [String]) -> Bool {
        !components.isEmpty
            && components.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." && !$0.contains("/") && !$0.contains("\0") })
    }

    var isAtApprovedPath: Bool {
        var opened = stat()
        var current = stat()
        return Darwin.fstat(descriptor, &opened) == 0 && Darwin.lstat(url.path, &current) == 0
            && (current.st_mode & S_IFMT) == S_IFDIR
            && opened.st_dev == current.st_dev && opened.st_ino == current.st_ino
    }
}

struct MovieFileAccessError: Error, Equatable {
    let code: Int32
}

/// An ISO under an approved root, as the last listing saw it. It authorizes nothing by itself:
/// `LiveSourceAuthorizer` reopens and rechecks it before every use.
struct LiveCatalogCandidate: Sendable {
    let id: String
    let rootID: String
    let fileName: String
    let revision: String
    let root: MovieRootDirectory
    let components: [String]
}

/// The movies and live ISO candidates one read of the shared folders found.
struct MovieCatalogListing: Sendable {
    let catalog: SharedMovieCatalog
    let liveCandidates: [LiveCatalogCandidate]
}

actor MovieCatalog {
    private struct Entry: Sendable {
        let movie: SharedMovie
        let root: MovieRootDirectory
        let components: [String]
    }
    private let sources: [MovieSourceBookmark]
    private let resolve: BookmarkResolver
    private let openDeadline: TimeInterval
    /// How long a listing is served before a request starts a background reread; nil rereads on
    /// every request.
    private let maxListingAge: TimeInterval?
    private let now: @Sendable () -> Date
    private var roots: [String: MovieRootDirectory] = [:]
    private var entries: [String: Entry] = [:]
    private var liveEntries: [String: LiveCatalogCandidate] = [:]
    private var cached: (listing: MovieCatalogListing, at: Date)?
    private var reading: Task<MovieCatalogListing, Error>?
    private var lastReadFailed = false
    private var readObserver: (@Sendable (MovieCatalogListing) async -> Void)?
    private var stopped = false

    typealias BookmarkResolver = @Sendable (Data) throws -> URL

    static let resolveSecurityScopedBookmark: BookmarkResolver = { data in
        var stale = false
        return try URL(resolvingBookmarkData: data, options: [.withSecurityScope, .withoutUI], relativeTo: nil, bookmarkDataIsStale: &stale)
    }

    /// Resolves and opens the approved roots off the caller, each within `deadline`. A root on a
    /// share that does not answer in time (for example while macOS waits for network-volume
    /// consent) is listed as unavailable instead of blocking the caller, and is opened again on
    /// the next read.
    static func open(
        sources: [MovieSourceBookmark],
        deadline: TimeInterval = BoundedFileAccess.defaultDeadline,
        maxListingAge: TimeInterval? = 30,
        now: @escaping @Sendable () -> Date = { Date() },
        resolve: @escaping BookmarkResolver = resolveSecurityScopedBookmark
    ) async throws -> MovieCatalog {
        try validate(sources)
        let catalog = MovieCatalog(
            sources: sources, roots: [:], resolve: resolve, openDeadline: deadline, maxListingAge: maxListingAge, now: now
        )
        await catalog.openMissingRoots()
        return catalog
    }

    /// Opens the roots on the calling thread. Only for tests with local folders; the app uses
    /// `open(sources:)`.
    init(
        sources: [MovieSourceBookmark],
        maxListingAge: TimeInterval? = nil,
        now: @escaping @Sendable () -> Date = { Date() },
        resolve: @escaping BookmarkResolver = resolveSecurityScopedBookmark
    ) throws {
        try Self.validate(sources)
        var roots: [String: MovieRootDirectory] = [:]
        for source in sources {
            if let url = try? resolve(source.bookmark), let root = try? MovieRootDirectory(source: source, url: url) {
                roots[source.id] = root
            }
        }
        self.init(
            sources: sources, roots: roots, resolve: resolve, openDeadline: BoundedFileAccess.defaultDeadline,
            maxListingAge: maxListingAge, now: now
        )
    }

    private init(
        sources: [MovieSourceBookmark],
        roots: [String: MovieRootDirectory],
        resolve: @escaping BookmarkResolver,
        openDeadline: TimeInterval,
        maxListingAge: TimeInterval?,
        now: @escaping @Sendable () -> Date
    ) {
        self.sources = sources
        self.roots = roots
        self.resolve = resolve
        self.openDeadline = openDeadline
        self.maxListingAge = maxListingAge
        self.now = now
    }

    private func openMissingRoots() async {
        for source in sources where roots[source.id] == nil {
            let resolve = resolve
            let opened = await BoundedFileAccess.run(deadline: openDeadline) {
                (try? resolve(source.bookmark)).flatMap { try? MovieRootDirectory(source: source, url: $0) }
            }
            guard !stopped else { return }
            if let root = opened ?? nil { roots[source.id] = root }
        }
    }

    private static func validate(_ sources: [MovieSourceBookmark]) throws {
        guard sources.count <= MovieLibraryContract.maximumRoots, Set(sources.map(\.id)).count == sources.count,
              sources.allSatisfy({ UUID(uuidString: $0.id) != nil && $0.name.utf8.count <= 512 })
        else { throw MovieLibraryError.invalidResponse }
    }

    func stop() {
        stopped = true
        cached = nil
        entries.removeAll()
        liveEntries.removeAll()
        roots.removeAll()
    }

    /// Shared folders the last read could not open. Every folder when the last read did not
    /// finish, since it cannot tell which one stalled. Empty until the first read ends.
    func unreadableRootIDs() -> Set<String> {
        if lastReadFailed { return Set(sources.map(\.id)) }
        return Set(cached?.listing.catalog.roots.filter { !$0.isAvailable }.map(\.id) ?? [])
    }

    /// Called after every completed read, including background rereads.
    func observeReads(_ observer: @escaping @Sendable (MovieCatalogListing) async -> Void) {
        readObserver = observer
    }

    func snapshot() async throws -> SharedMovieCatalog {
        try await listing().catalog
    }

    /// The ISO from the last listing with this ID, if any.
    func liveCandidate(id: String) -> LiveCatalogCandidate? {
        stopped ? nil : liveEntries[id]
    }

    /// Movies, plus the ISOs that may stream live, sorted by name. ISOs never enter `movies`.
    /// A listing younger than `maxListingAge` is served as is; an older one is served while a
    /// reread runs in the background, so a request never waits on a walk it does not need.
    func listing() async throws -> MovieCatalogListing {
        guard !stopped else { throw MovieLibraryError.unavailable }
        if let maxListingAge, let cached {
            if now().timeIntervalSince(cached.at) > maxListingAge, reading == nil {
                Task { _ = try? await self.read() }
            }
            return cached.listing
        }
        return try await read()
    }

    /// Reads the shared folders now, joining a read that is already running.
    @discardableResult
    func read() async throws -> MovieCatalogListing {
        if let reading { return try await reading.value }
        let task = Task { try await self.walk() }
        reading = task
        defer { reading = nil }
        do {
            return try await task.value
        } catch {
            if !stopped { lastReadFailed = true }
            throw error
        }
    }

    private func walk() async throws -> MovieCatalogListing {
        guard !stopped else { throw MovieLibraryError.unavailable }
        await openMissingRoots()
        guard !stopped else { throw MovieLibraryError.unavailable }
        let sources = sources
        let roots = roots
        // The walk stats files that may be on a network share, so it runs off this actor.
        guard let result = await BoundedFileAccess.run(deadline: BoundedFileAccess.defaultDeadline, {
            Result { try Self.scan(sources: sources, roots: roots) }
        }) else { throw MovieLibraryError.unavailable }
        let scan = try result.get()
        guard !stopped else { throw MovieLibraryError.unavailable }
        if scan.catalog.truncated {
            // A walk cut short never reached some files. Keep what the last read found for them,
            // so playback of an unchanged file goes on; every read still rechecks the revision.
            entries = scan.entries.merging(entries) { new, _ in new }
            liveEntries = scan.live.merging(liveEntries) { new, _ in new }
        } else {
            entries = scan.entries
            liveEntries = scan.live
        }
        let candidates = scan.live.values.sorted {
            $0.fileName.localizedStandardCompare($1.fileName) == .orderedAscending
        }
        let listing = MovieCatalogListing(catalog: scan.catalog, liveCandidates: candidates)
        cached = (listing, now())
        lastReadFailed = false
        await readObserver?(listing)
        return listing
    }

    private struct Scan: Sendable {
        let catalog: SharedMovieCatalog
        let entries: [String: Entry]
        let live: [String: LiveCatalogCandidate]
    }

    private static func scan(sources: [MovieSourceBookmark], roots: [String: MovieRootDirectory]) throws -> Scan {
        let deadline = Date().addingTimeInterval(5)
        var next: [String: Entry] = [:]
        var nextLive: [String: LiveCatalogCandidate] = [:]
        var summaries: [SharedMovieRoot] = []
        var truncated = false
        var visits = 0
        for source in sources {
            guard let root = roots[source.id], root.isAtApprovedPath, let enumerator = FileManager.default.enumerator(
                at: root.url,
                includingPropertiesForKeys: [.isRegularFileKey, .isDirectoryKey, .isSymbolicLinkKey],
                options: [.skipsHiddenFiles, .skipsPackageDescendants]
            ) else {
                summaries.append(SharedMovieRoot(id: source.id, name: source.name, isAvailable: false))
                continue
            }
            summaries.append(SharedMovieRoot(id: source.id, name: source.name, isAvailable: true))
            let prefix = root.url.pathComponents
            for case let url as URL in enumerator {
                    visits += 1
                if next.count >= MovieLibraryContract.maximumMovies || visits > 10_000 || Date() > deadline {
                    truncated = true
                    break
                }
                guard url.pathComponents.starts(with: prefix) else { enumerator.skipDescendants(); continue }
                let components = Array(url.pathComponents.dropFirst(prefix.count))
                let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .isDirectoryKey, .isSymbolicLinkKey])
                if values?.isSymbolicLink == true || components.count > 5 {
                    enumerator.skipDescendants()
                    continue
                }
                guard values?.isRegularFile == true else { continue }
                let id = MovieLibraryContract.digest(Data((source.id + "/" + components.joined(separator: "/")).utf8))
                if MovieLibraryContract.liveExtensions.contains(url.pathExtension.lowercased()) {
                    guard nextLive.count < MovieLibraryContract.maximumLiveTitles else {
                        truncated = true
                        continue
                    }
                    if let info = root.statFile(components) {
                        nextLive[id] = LiveCatalogCandidate(
                            id: id, rootID: source.id, fileName: url.lastPathComponent,
                            revision: Self.revision(info), root: root, components: components
                        )
                    }
                    continue
                }
                guard MovieLibraryContract.supportedExtensions.contains(url.pathExtension.lowercased()),
                      let fd = try? root.openFile(components)
                else { continue }
                defer { Darwin.close(fd) }
                guard let info = try? Self.fileInfo(fd) else { continue }
                let movie = SharedMovie(id: id, rootID: source.id, fileName: url.lastPathComponent, byteCount: info.st_size, revision: Self.revision(info))
                guard (try? movie.validate()) != nil else { continue }
                next[id] = Entry(movie: movie, root: root, components: components)
            }
        }
        let catalog = SharedMovieCatalog(roots: summaries, movies: next.values.map(\.movie).sorted {
            $0.fileName.localizedStandardCompare($1.fileName) == .orderedAscending
        }, truncated: truncated)
        try catalog.validate()
        return Scan(catalog: catalog, entries: next, live: nextLive)
    }

    func read(_ request: MovieByteRequest) throws -> Data {
        guard !stopped else { throw MovieLibraryError.unavailable }
        guard let entry = entries[request.movieID], entry.movie.revision == request.revision else { throw MovieLibraryError.sourceChanged }
        guard request.offset >= 0, request.offset < entry.movie.byteCount,
              request.count > 0, request.count <= MovieLibraryContract.maximumChunkBytes,
              Int64(request.count) <= entry.movie.byteCount - request.offset
        else { throw MovieLibraryError.invalidRange }
        try Task.checkCancellation()
        let fd = try entry.root.openFile(entry.components)
        defer { Darwin.close(fd) }
        guard Self.revision(try Self.fileInfo(fd)) == request.revision else { throw MovieLibraryError.sourceChanged }
        var data = Data(count: request.count)
        let count = try data.withUnsafeMutableBytes { bytes in
            var count: Int
            repeat {
                try Task.checkCancellation()
                count = Darwin.pread(fd, bytes.baseAddress, request.count, off_t(request.offset))
            } while count < 0 && errno == EINTR
            return count
        }
        guard count == request.count, Self.revision(try Self.fileInfo(fd)) == request.revision else {
            throw MovieLibraryError.sourceChanged
        }
        try Task.checkCancellation()
        return data
    }

    private static func fileInfo(_ descriptor: Int32) throws -> stat {
        var info = stat()
        guard Darwin.fstat(descriptor, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG, info.st_size > 0 else {
            throw MovieLibraryError.sourceChanged
        }
        return info
    }

    static func revision(_ info: stat) -> String {
        MovieLibraryContract.digest(Data("\(info.st_dev):\(info.st_ino):\(info.st_size):\(info.st_mtimespec.tv_sec):\(info.st_mtimespec.tv_nsec):\(info.st_ctimespec.tv_sec):\(info.st_ctimespec.tv_nsec):\(info.st_gen)".utf8))
    }
}

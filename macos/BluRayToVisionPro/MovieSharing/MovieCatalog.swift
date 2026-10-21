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

actor MovieCatalog {
    private struct Entry {
        let movie: SharedMovie
        let root: MovieRootDirectory
        let components: [String]
    }
    private let sources: [MovieSourceBookmark]
    private var roots: [String: MovieRootDirectory] = [:]
    private var entries: [String: Entry] = [:]
    private var liveEntries: [String: LiveCatalogCandidate] = [:]
    private var stopped = false

    init(sources: [MovieSourceBookmark], resolve: (Data) throws -> URL = { data in
        var stale = false
        return try URL(resolvingBookmarkData: data, options: [.withSecurityScope, .withoutUI], relativeTo: nil, bookmarkDataIsStale: &stale)
    }) throws {
        guard sources.count <= MovieLibraryContract.maximumRoots, Set(sources.map(\.id)).count == sources.count else {
            throw MovieLibraryError.invalidResponse
        }
        self.sources = sources
        for source in sources {
            guard UUID(uuidString: source.id) != nil, source.name.utf8.count <= 512 else { throw MovieLibraryError.invalidResponse }
            if let url = try? resolve(source.bookmark), let root = try? MovieRootDirectory(source: source, url: url) {
                roots[source.id] = root
            }
        }
    }

    func stop() {
        stopped = true
        entries.removeAll()
        liveEntries.removeAll()
        roots.removeAll()
    }

    func snapshot() throws -> SharedMovieCatalog {
        try listing().catalog
    }

    /// The ISO from the last listing with this ID, if any.
    func liveCandidate(id: String) -> LiveCatalogCandidate? {
        stopped ? nil : liveEntries[id]
    }

    /// Movies, plus the ISOs that may stream live, sorted by name. ISOs never enter `movies`.
    func listing() throws -> (catalog: SharedMovieCatalog, liveCandidates: [LiveCatalogCandidate]) {
        guard !stopped else { throw MovieLibraryError.unavailable }
        let deadline = Date().addingTimeInterval(5)
        var next: [String: Entry] = [:]
        var nextLive: [String: LiveCatalogCandidate] = [:]
        var summaries: [SharedMovieRoot] = []
        var truncated = false
        var visits = 0
        for source in sources {
            try Task.checkCancellation()
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
                try Task.checkCancellation()
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
        entries = next
        liveEntries = nextLive
        let catalog = SharedMovieCatalog(roots: summaries, movies: next.values.map(\.movie).sorted {
            $0.fileName.localizedStandardCompare($1.fileName) == .orderedAscending
        }, truncated: truncated)
        try catalog.validate()
        let candidates = nextLive.values.sorted {
            $0.fileName.localizedStandardCompare($1.fileName) == .orderedAscending
        }
        return (catalog, candidates)
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

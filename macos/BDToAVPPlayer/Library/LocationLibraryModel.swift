import Combine
import Foundation

enum LocationStatus: Equatable, Sendable {
    case notChecked
    case checking
    case available
    case unreachable
}

/// A file in a Location that the player cannot play, with the plain reason.
struct UnplayableLocationFile: Equatable, Identifiable, Sendable {
    let relativePath: String
    let reason: String

    var id: String { relativePath }
}

/// Locations: network shares (a NAS or a Mac's File Sharing) and other Files folders the wearer
/// adds once. visionOS apps cannot mount SMB themselves, so the wearer connects the server in
/// Files and picks a folder; the folder's bookmark keeps access across launches. Each Location
/// lists its playable titles, which play through the same players as local movies.
@MainActor
final class LocationLibraryModel: ObservableObject {
    @Published private(set) var locations: [StoredLocation]
    @Published private(set) var statuses: [UUID: LocationStatus] = [:]
    @Published private(set) var titles: [UUID: [MediaItem]] = [:]
    @Published private(set) var unplayableFiles: [UUID: [UnplayableLocationFile]] = [:]
    @Published private(set) var truncatedLocations: Set<UUID> = []
    /// Files inspected so far while a Location is being checked.
    @Published private(set) var progress: [UUID: (inspected: Int, total: Int)] = [:]
    @Published var errorMessage: String?

    private let store: LocationStore
    private let bookmarkStore: BookmarkStore
    private let inspector: MediaSourceInspector
    private let openTimeout: Duration
    private let scanBudget: Duration
    private let inspectionTimeout: Duration
    private var refreshGenerations: [UUID: Int] = [:]
    private var refreshesInFlight: Set<UUID> = []

    init(
        store: LocationStore,
        bookmarkStore: BookmarkStore,
        inspector: MediaSourceInspector,
        openTimeout: Duration = .seconds(45),
        scanBudget: Duration = .seconds(15),
        inspectionTimeout: Duration = .seconds(60)
    ) {
        self.store = store
        self.bookmarkStore = bookmarkStore
        self.inspector = inspector
        self.openTimeout = openTimeout
        self.scanBudget = scanBudget
        self.inspectionTimeout = inspectionTimeout
        locations = store.load()
        for location in locations {
            statuses[location.id] = bookmarkStore.bookmarkData(for: Self.bookmarkID(for: location.id)) == nil
                ? .unreachable : .notChecked
        }
    }

    // MARK: Identity

    nonisolated static func bookmarkID(for locationID: UUID) -> String {
        "location:\(locationID.uuidString)"
    }

    nonisolated static func itemID(locationID: UUID, relativePath: String) -> String {
        "\(bookmarkID(for: locationID))/\(relativePath)"
    }

    nonisolated static func locationID(forItemID itemID: String) -> UUID? {
        guard itemID.hasPrefix("location:"), let slash = itemID.firstIndex(of: "/") else { return nil }
        return UUID(uuidString: String(itemID[itemID.index(itemID.startIndex, offsetBy: 9)..<slash]))
    }

    func location(id: UUID) -> StoredLocation? {
        locations.first { $0.id == id }
    }

    func item(id itemID: String) -> MediaItem? {
        guard let locationID = Self.locationID(forItemID: itemID) else { return nil }
        return titles[locationID]?.first { $0.id == itemID }
    }

    func locationName(forItemID itemID: String) -> String? {
        Self.locationID(forItemID: itemID).flatMap(location(id:))?.name
    }

    /// A title's source status follows its Location: reachable or not.
    func sourceStatus(forItemID itemID: String) -> MediaSourceStatus? {
        guard let locationID = Self.locationID(forItemID: itemID) else { return nil }
        switch statuses[locationID] {
        case .available, .checking:
            return item(id: itemID) == nil ? .missing : .available
        case .notChecked:
            return .checking
        case .unreachable, nil:
            return .unavailable
        }
    }

    // MARK: Adding and removing

    /// Adds the folder the wearer picked in Files and lists it. Returns the new Location's ID.
    @discardableResult
    func add(folderURL: URL) async -> UUID? {
        let location = StoredLocation(id: UUID(), name: Self.displayName(for: folderURL), inspectedFiles: [])
        guard saveBookmark(for: folderURL, locationID: location.id) else { return nil }
        locations.append(location)
        persist()
        await refresh(location.id)
        return location.id
    }

    /// Replaces a Location's folder access after the share moved or its bookmark stopped working,
    /// keeping its name and what was already inspected.
    func reconnect(_ locationID: UUID, folderURL: URL) async {
        guard location(id: locationID) != nil, saveBookmark(for: folderURL, locationID: locationID) else { return }
        await refresh(locationID, restart: true)
    }

    func remove(_ locationID: UUID) {
        refreshGenerations[locationID, default: 0] += 1
        locations.removeAll { $0.id == locationID }
        statuses.removeValue(forKey: locationID)
        titles.removeValue(forKey: locationID)
        unplayableFiles.removeValue(forKey: locationID)
        truncatedLocations.remove(locationID)
        progress.removeValue(forKey: locationID)
        try? bookmarkStore.remove(id: Self.bookmarkID(for: locationID))
        persist()
    }

    private func saveBookmark(for folderURL: URL, locationID: UUID) -> Bool {
        let lease = SecurityScopedResourceLease(url: folderURL)
        defer { lease.close() }
        do {
            try bookmarkStore.save(url: folderURL, for: Self.bookmarkID(for: locationID))
            return true
        } catch {
            errorMessage = "Shiny 3D Cinema could not keep access to this folder. Choose it again in Files."
            return false
        }
    }

    // MARK: Listing

    func refreshAll() async {
        for location in locations {
            await refresh(location.id)
        }
    }

    /// Lists the Location's folder and inspects files that are new or changed since last time.
    /// A refresh already under way is left to finish, so a slow first look at a large share is
    /// not restarted by the next periodic refresh; `restart` replaces it, as after reconnecting.
    func refresh(_ locationID: UUID, restart: Bool = false) async {
        guard location(id: locationID) != nil, restart || !refreshesInFlight.contains(locationID) else { return }
        refreshGenerations[locationID, default: 0] += 1
        let generation = refreshGenerations[locationID]!
        func isCurrent() -> Bool { refreshGenerations[locationID] == generation && !Task.isCancelled }
        refreshesInFlight.insert(locationID)
        defer {
            if refreshGenerations[locationID] == generation {
                refreshesInFlight.remove(locationID)
            }
        }

        statuses[locationID] = .checking
        let bookmarkStore = bookmarkStore
        let scanBudget = scanBudget
        let bookmarkID = Self.bookmarkID(for: locationID)
        let listed: (SecurityScopedResourceLease, LocationListing)
        do {
            listed = try await Self.withTimeout(openTimeout) { @MainActor in
                let lease = try await bookmarkStore.open(id: bookmarkID)
                let root = lease.url
                let listing = try await Task.detached(priority: .userInitiated) {
                    try LocationScanner.scan(root: root, budget: scanBudget)
                }.value
                return (lease, listing)
            }
        } catch {
            if isCurrent() { markUnreachable(locationID) }
            return
        }
        let (lease, listing) = listed
        defer { lease.close() }
        guard isCurrent() else { return }

        var inspected: [InspectedFile] = []
        var playable: [MediaItem] = []
        var unplayable: [UnplayableLocationFile] = []
        let cache = Dictionary(
            (location(id: locationID)?.inspectedFiles ?? []).map { ($0.relativePath, $0) },
            uniquingKeysWith: { first, _ in first }
        )
        // A first listing fills in as files are inspected; a refresh swaps the list once at the end.
        let showsProgressively = (titles[locationID] ?? []).isEmpty
        progress[locationID] = (0, listing.candidates.count)
        for (index, candidate) in listing.candidates.enumerated() {
            let itemID = Self.itemID(locationID: locationID, relativePath: candidate.relativePath)
            bookmarkStore.registerFolderChild(id: itemID, folderID: bookmarkID, relativePath: candidate.relativePath)
            let inspection: MediaSourceInspection?
            if let cached = cache[candidate.relativePath], cached.matches(candidate) {
                inspection = cached.inspection
            } else {
                let url = candidate.relativePath.isEmpty ? lease.url : lease.url.appendingPathComponent(candidate.relativePath)
                let inspector = inspector
                do {
                    inspection = try await Self.withTimeout(inspectionTimeout) { try await inspector.inspect(url) }
                } catch let error as Bluray3DSourceError where error.isAboutTheSource {
                    // The file was read and is not something the player can play.
                    inspection = MediaSourceInspection(format: .unsupported, bluray3DTitle: nil)
                } catch is LocationTimeoutError {
                    if isCurrent() { markUnreachable(locationID) }
                    return
                } catch {
                    inspection = nil
                }
            }
            guard isCurrent() else { return }
            if let inspection {
                inspected.append(InspectedFile(
                    relativePath: candidate.relativePath, size: candidate.size,
                    modified: candidate.modified, inspection: inspection
                ))
                if let reason = inspection.unplayableReason {
                    unplayable.append(UnplayableLocationFile(relativePath: candidate.relativePath, reason: reason))
                } else {
                    playable.append(Self.mediaItem(id: itemID, candidate: candidate, inspection: inspection))
                }
            } else {
                unplayable.append(UnplayableLocationFile(
                    relativePath: candidate.relativePath, reason: "This file could not be read. Try again later."
                ))
            }
            progress[locationID] = (index + 1, listing.candidates.count)
            if showsProgressively {
                titles[locationID] = playable
            }
            if (index + 1).isMultiple(of: 10) {
                // Keep what was learned so far if the scan is interrupted.
                let pending = listing.candidates[(index + 1)...].compactMap { cache[$0.relativePath] }
                saveInspectedFiles(inspected + pending, for: locationID)
            }
        }

        titles[locationID] = playable
        unplayableFiles[locationID] = unplayable
        if listing.isTruncated {
            truncatedLocations.insert(locationID)
        } else {
            truncatedLocations.remove(locationID)
        }
        progress.removeValue(forKey: locationID)
        statuses[locationID] = .available
        saveInspectedFiles(inspected, for: locationID)
    }

    private func saveInspectedFiles(_ files: [InspectedFile], for locationID: UUID) {
        guard let index = locations.firstIndex(where: { $0.id == locationID }) else { return }
        locations[index].inspectedFiles = files
        persist()
    }

    private func markUnreachable(_ locationID: UUID) {
        statuses[locationID] = .unreachable
        titles[locationID] = []
        unplayableFiles[locationID] = []
        progress.removeValue(forKey: locationID)
    }

    private func persist() {
        do {
            try store.save(locations)
        } catch {
            errorMessage = "Your Locations could not be saved."
        }
    }

    private static func mediaItem(id: String, candidate: LocationCandidate, inspection: MediaSourceInspection) -> MediaItem {
        let fileName = candidate.fileName
        let fileExtension = (fileName as NSString).pathExtension.lowercased()
        let title = LocationScanner.fileExtensions.contains(fileExtension)
            ? (fileName as NSString).deletingPathExtension
            : fileName
        return MediaItem(
            id: id,
            title: title.isEmpty ? "Disc" : title,
            fileName: fileName,
            format: inspection.format,
            bluray3DTitle: inspection.bluray3DTitle
        )
    }

    nonisolated static func displayName(for folderURL: URL) -> String {
        let name = folderURL.lastPathComponent
        return name.isEmpty || name == "/" ? "Location" : name
    }

    /// Runs `operation`, giving up after `timeout`. A share that stops answering can block a
    /// file call far longer than that, so the operation runs unstructured and is left behind.
    nonisolated static func withTimeout<T: Sendable>(
        _ timeout: Duration,
        _ operation: @escaping @Sendable () async throws -> T
    ) async throws -> T {
        let gate = TimeoutGate<T>()
        return try await withCheckedThrowingContinuation { continuation in
            gate.install(continuation)
            let timer = Task {
                try await Task.sleep(for: timeout)
                gate.resume(.failure(LocationTimeoutError()))
            }
            Task {
                let result: Result<T, Error>
                do { result = .success(try await operation()) } catch { result = .failure(error) }
                timer.cancel()
                gate.resume(result)
            }
        }
    }
}

struct LocationTimeoutError: Error {}

private final class TimeoutGate<T: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<T, Error>?

    func install(_ continuation: CheckedContinuation<T, Error>) {
        lock.withLock { self.continuation = continuation }
    }

    func resume(_ result: Result<T, Error>) {
        let pending: CheckedContinuation<T, Error>? = lock.withLock {
            defer { continuation = nil }
            return continuation
        }
        pending?.resume(with: result)
    }
}

private extension Bluray3DSourceError {
    /// Errors that describe the file itself rather than a failed read.
    var isAboutTheSource: Bool {
        switch self {
        case .unrecognized, .titleUnavailable, .unsupported, .invalidStream:
            return true
        case .openFailed, .readFailed:
            return false
        }
    }
}

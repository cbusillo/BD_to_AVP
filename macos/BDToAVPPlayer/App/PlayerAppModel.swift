import AVFoundation
import Combine
import Foundation

enum LibraryViewMode: String, CaseIterable, Sendable {
    case posters
    case files

    var title: String {
        rawValue.capitalized
    }
}

enum MediaFormatFilter: String, CaseIterable, Sendable {
    case all
    case mvHEVC
    case sideBySide
    case overUnder
    case unsupported

    var title: String {
        switch self {
        case .all:
            return "All Formats"
        case .mvHEVC:
            return "MV-HEVC"
        case .sideBySide:
            return "SBS"
        case .overUnder:
            return "OVER-UNDER"
        case .unsupported:
            return "Unsupported"
        }
    }

    func matches(_ item: MediaItem) -> Bool {
        switch self {
        case .all:
            return true
        case .mvHEVC:
            return item.format == .mvHEVC
        case .sideBySide:
            return item.format == .sideBySide
        case .overUnder:
            return item.format == .overUnder
        case .unsupported:
            return item.format == .unsupported
        }
    }
}

enum MediaSortOrder: String, CaseIterable, Sendable {
    case title
    case fileName

    var title: String {
        switch self {
        case .title:
            return "Title"
        case .fileName:
            return "Filename"
        }
    }
}

enum MediaSourceStatus: Equatable, Sendable {
    case checking
    case available
    case unavailable
    case missing
    case stale

    var title: String {
        switch self {
        case .checking:
            return "Checking source"
        case .available:
            return "Available"
        case .unavailable:
            return "Source unavailable"
        case .missing:
            return "Source missing"
        case .stale:
            return "Source moved"
        }
    }
}

struct PlaybackRequest: Equatable, Sendable {
    let item: MediaItem
}

enum PlaybackAvailability: Equatable, Sendable {
    case playable
    case planned(String)
    case unavailable(String)
}

@MainActor
final class PlayerAppModel: ObservableObject {
    typealias FormatInspector = (URL) async throws -> StereoFormat
    typealias StereoCheckInstaller = @Sendable () throws -> [InstalledStereoCheck]

    @Published private(set) var library: MediaLibraryModel
    @Published private(set) var sourceStatuses: [String: MediaSourceStatus]
    @Published var viewMode: LibraryViewMode = .posters
    @Published var formatFilter: MediaFormatFilter = .all
    @Published var sortOrder: MediaSortOrder = .title
    @Published var selectedItemID: String?
    @Published var isShowingDetails = false
    @Published var isImporting = false
    @Published var errorMessage: String?
    @Published private(set) var stereoCheckErrorMessage: String?
    @Published private(set) var playbackRequest: PlaybackRequest?
    @Published private(set) var hasBootstrapped = false

    var onPlaybackRequested: ((MediaItem) -> Void)?

    private let libraryStore: LibraryStore
    let bookmarkStore: BookmarkStore
    private let formatInspector: FormatInspector
    private let stereoCheckInstaller: StereoCheckInstaller
    private let documentsURL: URL
    private var isRefreshingSourceStatuses = false
    private var needsSourceStatusRefresh = false
    private var sourceStatusProbeGenerations: [String: Int] = [:]

    init(
        libraryStore: LibraryStore = LibraryStore(),
        bookmarkStore: BookmarkStore? = nil,
        documentsURL: URL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0],
        formatInspector: @escaping FormatInspector = MediaFormatInspector.inspect,
        stereoCheckInstaller: @escaping StereoCheckInstaller = { try BuiltInStereoChecks.install() }
    ) {
        let resolvedBookmarkStore = bookmarkStore ?? BookmarkStore()
        self.libraryStore = libraryStore
        self.bookmarkStore = resolvedBookmarkStore
        self.documentsURL = documentsURL
        self.formatInspector = formatInspector
        self.stereoCheckInstaller = stereoCheckInstaller
        let loadedLibrary = MediaLibraryModel(items: libraryStore.load())
        self.library = loadedLibrary
        self.sourceStatuses = Dictionary(
            uniqueKeysWithValues: loadedLibrary.items.map { item in
                (
                    item.id,
                    resolvedBookmarkStore.bookmarkData(for: item.id) == nil ? .missing : .checking
                )
            }
        )
    }

    var visibleItems: [MediaItem] {
        library.items
            .filter { formatFilter.matches($0) }
            .sorted {
                switch sortOrder {
                case .title:
                    return $0.title.localizedStandardCompare($1.title) == .orderedAscending
                case .fileName:
                    return $0.fileName.localizedStandardCompare($1.fileName) == .orderedAscending
                }
            }
    }

    var builtInStereoCheckItems: [MediaItem] {
        BuiltInStereoChecks.orderedIDs.compactMap(item(id:))
    }

    var importedItems: [MediaItem] {
        library.items.filter { !BuiltInStereoChecks.contains($0) }
    }

    var visibleImportedItems: [MediaItem] {
        visibleItems.filter { !BuiltInStereoChecks.contains($0) }
    }

    var selectedItem: MediaItem? {
        guard let selectedItemID else { return nil }
        return library.items.first { $0.id == selectedItemID }
    }

    func item(id: String) -> MediaItem? {
        library.items.first { $0.id == id }
    }

    func sourceTitle(for item: MediaItem) -> String {
        if BuiltInStereoChecks.contains(item) {
            return "Built-in Stereo Check"
        }
        return item.id.hasPrefix("documents:") ? "On My Vision Pro" : "Files"
    }

    func showDetails(for id: String) {
        guard item(id: id) != nil else { return }
        selectedItemID = id
        isShowingDetails = true
    }

    func closeDetails() {
        isShowingDetails = false
        selectedItemID = nil
    }

    func playbackAvailability(for item: MediaItem) -> PlaybackAvailability {
        let sourceStatus = sourceStatuses[item.id] ?? .missing
        guard sourceStatus == .available || sourceStatus == .checking else {
            if sourceStatus == .unavailable {
                return .unavailable("Try the source again or locate it before playing.")
            }
            return .unavailable("Locate the source before playing.")
        }

        switch item.format {
        case .mvHEVC, .sideBySide, .overUnder:
            return .playable
        case .unsupported:
            return .unavailable("This media format is not supported for playback.")
        }
    }

    func requestPlayback(for id: String) {
        guard let item = item(id: id), playbackAvailability(for: item) == .playable else {
            return
        }
        let request = PlaybackRequest(item: item)
        playbackRequest = request
        onPlaybackRequested?(item)
    }

    func clearPlaybackRequest() {
        playbackRequest = nil
    }

    func importMovie(from url: URL) async {
        _ = await importMovie(from: url, replacing: nil)
    }

    @discardableResult
    func locate(itemID: String, at url: URL, shouldShowDetails: Bool = true) async -> Bool {
        await importMovie(
            from: url,
            replacing: itemID,
            shouldShowDetails: shouldShowDetails,
            reportErrors: true
        )
    }

    func remove(itemID: String) {
        guard !BuiltInStereoChecks.orderedIDs.contains(itemID) else {
            return
        }
        do {
            try libraryStore.remove(id: itemID)
            invalidateSourceStatusProbe(for: itemID)
            objectWillChange.send()
            library.remove(id: itemID)
            sourceStatuses.removeValue(forKey: itemID)
            if selectedItemID == itemID {
                closeDetails()
            }

            do {
                try bookmarkStore.remove(id: itemID)
            } catch {
                errorMessage = "The movie was removed, but its saved file access could not be cleaned up."
            }
        } catch {
            errorMessage = "Could not remove this movie."
        }
    }

    func clearError() {
        errorMessage = nil
    }

    func bootstrap() async {
        guard !hasBootstrapped else { return }
        hasBootstrapped = true

        await installBuiltInStereoChecks()

        guard let urls = try? FileManager.default.contentsOfDirectory(
                  at: documentsURL,
                  includingPropertiesForKeys: [.isRegularFileKey],
                  options: [.skipsHiddenFiles]
              )
        else {
            return
        }

        for url in urls where Self.supportedMovieExtensions.contains(url.pathExtension.lowercased()) {
            let itemID = "documents:\(url.lastPathComponent.lowercased())"
            _ = await importMovie(from: url, replacing: itemID, shouldShowDetails: false, reportErrors: false)
        }

        await refreshSourceStatuses()
    }

    private func installBuiltInStereoChecks() async {
        do {
            let installer = stereoCheckInstaller
            let installedChecks = try await Task.detached(priority: .utility) {
                try installer()
            }.value
            for installedCheck in installedChecks {
                try bookmarkStore.save(url: installedCheck.url, for: installedCheck.item.id)
                try libraryStore.upsert(installedCheck.item)
                invalidateSourceStatusProbe(for: installedCheck.item.id)
                objectWillChange.send()
                library.upsert(installedCheck.item)
                sourceStatuses[installedCheck.item.id] = .available
            }
            stereoCheckErrorMessage = nil
        } catch {
            stereoCheckErrorMessage = "The built-in stereo checks could not be prepared."
        }
    }

    private func importMovie(from url: URL, replacing itemID: String?) async -> Bool {
        await importMovie(from: url, replacing: itemID, shouldShowDetails: true, reportErrors: true)
    }

    private func importMovie(
        from url: URL,
        replacing itemID: String?,
        shouldShowDetails: Bool,
        reportErrors: Bool
    ) async -> Bool {
        isImporting = true
        defer { isImporting = false }

        let lease = SecurityScopedResourceLease(url: url)
        defer { lease.close() }

        do {
            let bookmarkData = try url.bookmarkData(options: [])
            let format = try await formatInspector(url)
            let importedItemID = itemID ?? documentsItemID(for: url)
            let existingItem = importedItemID.flatMap(item(id:))
            let importedItem = MediaItem(
                id: importedItemID ?? UUID().uuidString,
                title: existingItem?.title ?? url.deletingPathExtension().lastPathComponent,
                fileName: url.lastPathComponent,
                format: format
            )

            try bookmarkStore.save(bookmarkData: bookmarkData, for: importedItem.id)
            try libraryStore.upsert(importedItem)
            invalidateSourceStatusProbe(for: importedItem.id)
            objectWillChange.send()
            library.upsert(importedItem)
            sourceStatuses[importedItem.id] = .available
            if shouldShowDetails {
                showDetails(for: importedItem.id)
            }
            return true
        } catch {
            if reportErrors {
                errorMessage = "Could not inspect or add this movie."
            }
            return false
        }
    }

    private static let supportedMovieExtensions = Set(["mov", "mp4", "m4v"])

    private func documentsItemID(for url: URL) -> String? {
        let documentsDirectory = documentsURL.standardizedFileURL.resolvingSymlinksInPath()
        let fileURL = url.standardizedFileURL.resolvingSymlinksInPath()
        guard fileURL.deletingLastPathComponent() == documentsDirectory else {
            return nil
        }
        return "documents:\(fileURL.lastPathComponent.lowercased())"
    }

    func refreshSourceStatuses() async {
        guard !isRefreshingSourceStatuses else {
            needsSourceStatusRefresh = true
            return
        }
        isRefreshingSourceStatuses = true
        defer { isRefreshingSourceStatuses = false }

        repeat {
            needsSourceStatusRefresh = false
            for item in library.items {
                guard !Task.isCancelled else {
                    return
                }
                let probeGeneration = beginSourceStatusProbe(for: item.id)
                guard let result = await sourceStatus(for: item.id),
                      sourceStatusProbeGenerations[item.id] == probeGeneration,
                      self.item(id: item.id) != nil,
                      bookmarkStore.bookmarkData(for: item.id) == result.bookmarkData
                else {
                    continue
                }
                sourceStatuses[item.id] = result.status
            }
        } while needsSourceStatusRefresh && !Task.isCancelled
    }

    func refreshSourceStatus(for itemID: String) async {
        guard item(id: itemID) != nil else {
            return
        }
        let probeGeneration = beginSourceStatusProbe(for: itemID)
        guard let result = await sourceStatus(for: itemID),
              sourceStatusProbeGenerations[itemID] == probeGeneration,
              item(id: itemID) != nil,
              bookmarkStore.bookmarkData(for: itemID) == result.bookmarkData
        else {
            return
        }
        sourceStatuses[itemID] = result.status
    }

    private func sourceStatus(for itemID: String) async -> SourceStatusResult? {
        let originalBookmarkData = bookmarkStore.bookmarkData(for: itemID)
        do {
            let lease = try await bookmarkStore.open(id: itemID)
            lease.close()
            return SourceStatusResult(
                status: .available,
                bookmarkData: bookmarkStore.bookmarkData(for: itemID)
            )
        } catch is CancellationError {
            return nil
        } catch BookmarkStoreError.staleBookmark {
            return SourceStatusResult(status: .stale, bookmarkData: originalBookmarkData)
        } catch BookmarkStoreError.missingBookmark,
                BookmarkStoreError.invalidBookmark {
            return SourceStatusResult(status: .missing, bookmarkData: originalBookmarkData)
        } catch {
            return SourceStatusResult(status: .unavailable, bookmarkData: originalBookmarkData)
        }
    }

    private func beginSourceStatusProbe(for itemID: String) -> Int {
        let generation = (sourceStatusProbeGenerations[itemID] ?? 0) + 1
        sourceStatusProbeGenerations[itemID] = generation
        return generation
    }

    private func invalidateSourceStatusProbe(for itemID: String) {
        sourceStatusProbeGenerations[itemID] = (sourceStatusProbeGenerations[itemID] ?? 0) + 1
    }
}

private struct SourceStatusResult {
    let status: MediaSourceStatus
    let bookmarkData: Data?
}

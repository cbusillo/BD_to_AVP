import AVFoundation
import Combine
import Foundation
import UniformTypeIdentifiers

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
    case bluray3D
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
        case .bluray3D:
            return "Blu-ray 3D"
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
        case .bluray3D:
            return item.format == .bluray3D
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
    case unavailable(String)
}

@MainActor
final class PlayerAppModel: ObservableObject {
    typealias FormatInspector = MediaSourceInspector.FormatInspector
    typealias BlurayInspector = MediaSourceInspector.BlurayInspector
    typealias StereoCheckInstaller = @Sendable () throws -> [InstalledStereoCheck]

    @Published private(set) var library: MediaLibraryModel
    @Published private(set) var sourceStatuses: [String: MediaSourceStatus]
    @Published var viewMode: LibraryViewMode = .posters
    @Published var formatFilter: MediaFormatFilter = .all
    @Published var sortOrder: MediaSortOrder = .title
    @Published var selectedItemID: String?
    @Published var isShowingDetails = false
    @Published var errorMessage: String?
    @Published private(set) var stereoCheckErrorMessage: String?
    @Published private(set) var playbackRequest: PlaybackRequest?
    @Published private(set) var hasBootstrapped = false

    var onPlaybackRequested: ((MediaItem) -> Void)?

    private let libraryStore: LibraryStore
    let bookmarkStore: BookmarkStore
    /// Network shares the wearer added; each is its own library.
    let locations: LocationLibraryModel
    private let sourceInspector: MediaSourceInspector
    private let stereoCheckInstaller: StereoCheckInstaller
    private let documentsURL: URL
    private var isRefreshingSourceStatuses = false
    private var needsSourceStatusRefresh = false
    private var sourceStatusProbeGenerations: [String: Int] = [:]
    private var locationsChange: AnyCancellable?

    init(
        libraryStore: LibraryStore = LibraryStore(),
        bookmarkStore: BookmarkStore? = nil,
        locationStore: LocationStore = LocationStore(),
        documentsURL: URL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0],
        formatInspector: @escaping FormatInspector = { try await MediaFormatInspector.inspect(url: $0) },
        blurayInspector: @escaping BlurayInspector = MediaSourceInspector().blurayInspector,
        stereoCheckInstaller: @escaping StereoCheckInstaller = { try BuiltInStereoChecks.install() }
    ) {
        let resolvedBookmarkStore = bookmarkStore ?? BookmarkStore()
        let sourceInspector = MediaSourceInspector(formatInspector: formatInspector, blurayInspector: blurayInspector)
        self.libraryStore = libraryStore
        self.bookmarkStore = resolvedBookmarkStore
        self.sourceInspector = sourceInspector
        self.locations = LocationLibraryModel(
            store: locationStore,
            bookmarkStore: resolvedBookmarkStore,
            inspector: sourceInspector
        )
        self.documentsURL = documentsURL
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
        // Views watch this model; a Location's listing changing is a change here too.
        locationsChange = locations.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }
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
        library.items.first { $0.id == id } ?? locations.item(id: id)
    }

    /// Whether an item's source can be reached right now. A Location's titles share its status.
    func sourceStatus(for itemID: String) -> MediaSourceStatus? {
        locations.sourceStatus(forItemID: itemID) ?? sourceStatuses[itemID]
    }

    func isLocationItem(_ itemID: String) -> Bool {
        LocationLibraryModel.locationID(forItemID: itemID) != nil
    }

    func sourceTitle(for item: MediaItem) -> String {
        if BuiltInStereoChecks.contains(item) {
            return "Built-in Stereo Check"
        }
        if let name = locations.locationName(forItemID: item.id) {
            return name
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
        let sourceStatus = self.sourceStatus(for: item.id) ?? .missing
        guard sourceStatus == .available || sourceStatus == .checking else {
            if sourceStatus == .unavailable {
                return .unavailable("Try the source again or locate it before playing.")
            }
            return .unavailable("Locate the source before playing.")
        }

        switch item.format {
        case .mvHEVC, .sideBySide, .overUnder:
            return .playable
        case .bluray3D:
            guard let title = item.bluray3DTitle else {
                return .unavailable(Bluray3DUnsupportedReason.notStereo3D.message)
            }
            if let reason = title.unsupportedReason {
                return .unavailable(reason.message)
            }
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

        // Movie files, Blu-ray 3D images, and copied disc folders that hold BDMV.
        for url in urls where Self.supportedMovieExtensions.contains(url.pathExtension.lowercased())
            || Self.isDiscFolder(url) {
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

    /// Inspects `url` and stores it as the library item `itemID`: a Documents file found on
    /// launch, or an existing item the wearer located again.
    private func importMovie(
        from url: URL,
        replacing itemID: String,
        shouldShowDetails: Bool,
        reportErrors: Bool
    ) async -> Bool {
        let lease = SecurityScopedResourceLease(url: url)
        defer { lease.close() }

        do {
            let bookmarkData = try url.bookmarkData(options: [])
            let inspection = try await sourceInspector.inspect(url)
            let importedItem = MediaItem(
                id: itemID,
                title: item(id: itemID)?.title ?? url.deletingPathExtension().lastPathComponent,
                fileName: url.lastPathComponent,
                format: inspection.format,
                bluray3DTitle: inspection.bluray3DTitle
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
                errorMessage = "Could not inspect this movie."
            }
            return false
        }
    }

    private static func isDiscFolder(_ url: URL) -> Bool {
        if case .discFolder = Bluray3DSourceOpener.kind(of: url) { return true }
        return false
    }

    private static let supportedMovieExtensions = Set(["mov", "mp4", "m4v", "iso", "mkv"])

    /// What Locate accepts: movies, Blu-ray 3D ISOs and MKVs, and disc folders.
    nonisolated static let importableContentTypes: [UTType] = [
        .movie,
        UTType("public.iso-image") ?? UTType(filenameExtension: "iso"),
        UTType(filenameExtension: "mkv"),
        .folder,
    ].compactMap { $0 }

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
        if let locationID = LocationLibraryModel.locationID(forItemID: itemID) {
            await locations.refresh(locationID)
            return
        }
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

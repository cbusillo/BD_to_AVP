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
    case available
    case missing
    case stale

    var title: String {
        switch self {
        case .available:
            return "Available"
        case .missing:
            return "Source unavailable"
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

    @Published private(set) var library: MediaLibraryModel
    @Published private(set) var sourceStatuses: [String: MediaSourceStatus]
    @Published var viewMode: LibraryViewMode = .posters
    @Published var formatFilter: MediaFormatFilter = .all
    @Published var sortOrder: MediaSortOrder = .title
    @Published var selectedItemID: String?
    @Published var isShowingDetails = false
    @Published var isImporting = false
    @Published var errorMessage: String?
    @Published private(set) var playbackRequest: PlaybackRequest?
    @Published private(set) var hasBootstrapped = false

    var onPlaybackRequested: ((MediaItem) -> Void)?

    private let libraryStore: LibraryStore
    let bookmarkStore: BookmarkStore
    private let formatInspector: FormatInspector
    private let documentsURL: URL

    init(
        libraryStore: LibraryStore = LibraryStore(),
        bookmarkStore: BookmarkStore = BookmarkStore(),
        documentsURL: URL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0],
        formatInspector: @escaping FormatInspector = MediaFormatInspector.inspect
    ) {
        self.libraryStore = libraryStore
        self.bookmarkStore = bookmarkStore
        self.documentsURL = documentsURL
        self.formatInspector = formatInspector
        let loadedLibrary = MediaLibraryModel(items: libraryStore.load())
        self.library = loadedLibrary
        self.sourceStatuses = [:]
        refreshSourceStatuses()
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

    var selectedItem: MediaItem? {
        guard let selectedItemID else { return nil }
        return library.items.first { $0.id == selectedItemID }
    }

    func item(id: String) -> MediaItem? {
        library.items.first { $0.id == id }
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
        guard sourceStatuses[item.id] == .available else {
            return .unavailable("Locate the source before playing.")
        }

        switch item.format {
        case .mvHEVC:
            return .playable
        case .sideBySide:
            return .planned("SBS playback is planned for a later slice.")
        case .overUnder:
            return .planned("Over-under playback is planned for a later slice.")
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
        closeDetails()
        onPlaybackRequested?(item)
    }

    func clearPlaybackRequest() {
        playbackRequest = nil
    }

    func importMovie(from url: URL) async {
        await importMovie(from: url, replacing: nil)
    }

    func locate(itemID: String, at url: URL) async {
        await importMovie(from: url, replacing: itemID)
    }

    func remove(itemID: String) {
        do {
            try bookmarkStore.remove(id: itemID)
            try libraryStore.remove(id: itemID)
            objectWillChange.send()
            library.remove(id: itemID)
            sourceStatuses.removeValue(forKey: itemID)
            if selectedItemID == itemID {
                closeDetails()
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
            await importMovie(from: url, replacing: itemID, shouldShowDetails: false, reportErrors: false)
        }
    }

    private func importMovie(from url: URL, replacing itemID: String?) async {
        await importMovie(from: url, replacing: itemID, shouldShowDetails: true, reportErrors: true)
    }

    private func importMovie(
        from url: URL,
        replacing itemID: String?,
        shouldShowDetails: Bool,
        reportErrors: Bool
    ) async {
        isImporting = true
        defer { isImporting = false }

        let lease = SecurityScopedResourceLease(url: url)
        defer { lease.close() }

        do {
            let bookmarkData = try url.bookmarkData(options: [])
            let format = try await formatInspector(url)
            let existingItem = itemID.flatMap(item(id:))
            let importedItem = MediaItem(
                id: itemID ?? UUID().uuidString,
                title: existingItem?.title ?? url.deletingPathExtension().lastPathComponent,
                fileName: url.lastPathComponent,
                format: format
            )

            try bookmarkStore.save(bookmarkData: bookmarkData, for: importedItem.id)
            try libraryStore.upsert(importedItem)
            objectWillChange.send()
            library.upsert(importedItem)
            sourceStatuses[importedItem.id] = .available
            if shouldShowDetails {
                showDetails(for: importedItem.id)
            }
        } catch {
            if reportErrors {
                errorMessage = "Could not inspect or add this movie."
            }
        }
    }

    private static let supportedMovieExtensions = Set(["mov", "mp4", "m4v"])

    private func refreshSourceStatuses() {
        var statuses: [String: MediaSourceStatus] = [:]
        for item in library.items {
            do {
                let lease = try bookmarkStore.open(id: item.id)
                lease.close()
                statuses[item.id] = .available
            } catch BookmarkStoreError.staleBookmark {
                statuses[item.id] = .stale
            } catch {
                statuses[item.id] = .missing
            }
        }
        sourceStatuses = statuses
    }
}

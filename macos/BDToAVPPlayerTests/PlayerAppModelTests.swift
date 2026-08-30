import XCTest
@testable import BDToAVPPlayer

@MainActor
final class PlayerAppModelTests: XCTestCase {
    func testLoadedCatalogDrivesFilteredAndSortedProjection() throws {
        let items = [
            MediaItem(
                id: "zulu",
                title: "Zulu",
                fileName: "zulu.mov",
                format: .unsupported
            ),
            MediaItem(
                id: "alpha",
                title: "Alpha",
                fileName: "alpha.mov",
                format: .mvHEVC
            )
        ]
        let libraryStore = LibraryStore(storageURL: temporaryURL())
        try libraryStore.save(items)
        let model = PlayerAppModel(
            libraryStore: libraryStore,
            bookmarkStore: BookmarkStore(storageURL: temporaryURL()),
            formatInspector: { _ in .mvHEVC }
        )

        XCTAssertEqual(model.viewMode, .posters)
        XCTAssertEqual(model.visibleItems.map(\.id), ["alpha", "zulu"])

        model.formatFilter = .mvHEVC

        XCTAssertEqual(model.visibleItems.map(\.id), ["alpha"])
        XCTAssertEqual(model.library.posters.map(\.id), ["zulu", "alpha"])
        XCTAssertEqual(model.library.files, model.library.posters)
    }

    func testSelectionAndPlaybackStateTransformationsRemainHonest() throws {
        let item = MediaItem(
            id: "movie-1",
            title: "Movie",
            fileName: "movie.mov",
            format: .sideBySide
        )
        let libraryStore = LibraryStore(storageURL: temporaryURL())
        try libraryStore.save([item])
        let model = PlayerAppModel(
            libraryStore: libraryStore,
            bookmarkStore: BookmarkStore(storageURL: temporaryURL()),
            formatInspector: { _ in .mvHEVC }
        )

        model.showDetails(for: item.id)

        XCTAssertEqual(model.selectedItemID, item.id)
        XCTAssertTrue(model.isShowingDetails)
        XCTAssertEqual(
            model.playbackAvailability(for: item),
            .unavailable("Locate the source before playing.")
        )

        model.closeDetails()

        XCTAssertNil(model.selectedItemID)
        XCTAssertFalse(model.isShowingDetails)
    }

    func testAvailableMVHEVCEmitsPlaybackStateAndCallback() throws {
        let sourceURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("mov")
        try FileManager.default.createDirectory(
            at: sourceURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data().write(to: sourceURL)

        let item = MediaItem(url: sourceURL, format: .mvHEVC)
        let libraryStore = LibraryStore(storageURL: temporaryURL())
        try libraryStore.save([item])
        let bookmarkStore = BookmarkStore(storageURL: temporaryURL())
        try bookmarkStore.save(url: sourceURL, for: item.id)
        let model = PlayerAppModel(
            libraryStore: libraryStore,
            bookmarkStore: bookmarkStore,
            formatInspector: { _ in .mvHEVC }
        )
        var callbackItem: MediaItem?
        model.onPlaybackRequested = { callbackItem = $0 }

        XCTAssertEqual(model.playbackAvailability(for: item), .playable)

        model.requestPlayback(for: item.id)

        XCTAssertEqual(model.playbackRequest?.item, item)
        XCTAssertEqual(callbackItem, item)
    }

    func testBootstrapIndexesSupportedMoviesWithoutOpeningDetails() async throws {
        let documentsURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerDocuments")
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: documentsURL, withIntermediateDirectories: true)
        let movieURL = documentsURL.appendingPathComponent("Example.mov")
        let ignoredURL = documentsURL.appendingPathComponent("Notes.txt")
        try Data().write(to: movieURL)
        try Data().write(to: ignoredURL)

        let model = PlayerAppModel(
            libraryStore: LibraryStore(storageURL: temporaryURL()),
            bookmarkStore: BookmarkStore(storageURL: temporaryURL()),
            documentsURL: documentsURL,
            formatInspector: { _ in .mvHEVC }
        )

        await model.bootstrap()

        XCTAssertEqual(model.library.items.map(\.fileName), ["Example.mov"])
        XCTAssertEqual(model.sourceStatuses.values.first, .available)
        XCTAssertFalse(model.isShowingDetails)
        XCTAssertTrue(model.hasBootstrapped)
    }

    private func temporaryURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("json")
    }
}

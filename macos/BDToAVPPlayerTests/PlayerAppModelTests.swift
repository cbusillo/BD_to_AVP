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
        XCTAssertEqual(model.sourceTitle(for: items[0]), "Files")
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

        model.showDetails(for: item.id)
        model.requestPlayback(for: item.id)

        XCTAssertEqual(model.playbackRequest?.item, item)
        XCTAssertEqual(callbackItem, item)
        XCTAssertEqual(model.selectedItemID, item.id)
        XCTAssertTrue(model.isShowingDetails)
    }

    func testAvailablePackedStereoFormatsArePlayable() throws {
        for format in [StereoFormat.sideBySide, .overUnder] {
            let sourceURL = FileManager.default.temporaryDirectory
                .appendingPathComponent("BDToAVPPlayerTests")
                .appendingPathComponent(UUID().uuidString)
                .appendingPathExtension("mov")
            try FileManager.default.createDirectory(
                at: sourceURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try Data().write(to: sourceURL)

            let item = MediaItem(url: sourceURL, format: format)
            let libraryStore = LibraryStore(storageURL: temporaryURL())
            try libraryStore.save([item])
            let bookmarkStore = BookmarkStore(storageURL: temporaryURL())
            try bookmarkStore.save(url: sourceURL, for: item.id)
            let model = PlayerAppModel(
                libraryStore: libraryStore,
                bookmarkStore: bookmarkStore,
                formatInspector: { _ in format }
            )

            XCTAssertEqual(model.playbackAvailability(for: item), .playable)
        }
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
        XCTAssertEqual(model.sourceTitle(for: model.library.items[0]), "On My Vision Pro")
        XCTAssertEqual(model.sourceStatuses.values.first, .available)
        XCTAssertFalse(model.isShowingDetails)
        XCTAssertTrue(model.hasBootstrapped)
    }

    func testAddingBootstrappedDocumentMovieUpdatesItsExistingLibraryEntry() async throws {
        let documentsURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerDocuments")
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: documentsURL, withIntermediateDirectories: true)
        let movieURL = documentsURL.appendingPathComponent("Example.mov")
        try Data().write(to: movieURL)

        let model = PlayerAppModel(
            libraryStore: LibraryStore(storageURL: temporaryURL()),
            bookmarkStore: BookmarkStore(storageURL: temporaryURL()),
            documentsURL: documentsURL,
            formatInspector: { _ in .mvHEVC }
        )

        await model.bootstrap()
        await model.importMovie(from: movieURL)

        XCTAssertEqual(model.library.items.map(\.id), ["documents:example.mov"])
        XCTAssertEqual(model.library.items.count, 1)
    }

    func testAddingMovieOutsideDocumentsUsesUUIDIdentity() async throws {
        let documentsURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerDocuments")
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: documentsURL, withIntermediateDirectories: true)
        let externalDirectory = documentsURL.deletingLastPathComponent()
            .appendingPathComponent("\(documentsURL.lastPathComponent)-copy", isDirectory: true)
        try FileManager.default.createDirectory(at: externalDirectory, withIntermediateDirectories: true)
        let movieURL = externalDirectory.appendingPathComponent("Example.mov")
        try Data().write(to: movieURL)

        let model = PlayerAppModel(
            libraryStore: LibraryStore(storageURL: temporaryURL()),
            bookmarkStore: BookmarkStore(storageURL: temporaryURL()),
            documentsURL: documentsURL,
            formatInspector: { _ in .mvHEVC }
        )

        await model.importMovie(from: movieURL)

        XCTAssertEqual(model.library.items.count, 1)
        XCTAssertNotEqual(model.library.items[0].id, "documents:example.mov")
        XCTAssertFalse(model.library.items[0].id.hasPrefix("documents:"))
    }

    private func temporaryURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("json")
    }
}

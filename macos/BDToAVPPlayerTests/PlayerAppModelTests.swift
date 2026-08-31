import AVFoundation
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
            formatInspector: { _ in .mvHEVC },
            stereoCheckInstaller: { [] }
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
            formatInspector: { _ in .mvHEVC },
            stereoCheckInstaller: { [] }
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
            formatInspector: { _ in .mvHEVC },
            stereoCheckInstaller: { [] }
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
                formatInspector: { _ in format },
                stereoCheckInstaller: { [] }
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
            formatInspector: { _ in .mvHEVC },
            stereoCheckInstaller: { [] }
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
            formatInspector: { _ in .mvHEVC },
            stereoCheckInstaller: { [] }
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
            formatInspector: { _ in .mvHEVC },
            stereoCheckInstaller: { [] }
        )

        await model.importMovie(from: movieURL)

        XCTAssertEqual(model.library.items.count, 1)
        XCTAssertNotEqual(model.library.items[0].id, "documents:example.mov")
        XCTAssertFalse(model.library.items[0].id.hasPrefix("documents:"))
    }

    func testBootstrapInstallsBuiltInStereoChecksAsPlayableSources() async throws {
        let sourceDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerBuiltInSources")
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let documentsURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerDocuments")
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: sourceDirectory, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: documentsURL, withIntermediateDirectories: true)

        let installedChecks = [
            InstalledStereoCheck(
                item: MediaItem(
                    id: BuiltInStereoChecks.sideBySideID,
                    title: "Side-by-Side Stereo Check",
                    fileName: "Stereo-Check-SBS.mov",
                    format: .sideBySide
                ),
                url: sourceDirectory.appendingPathComponent("Stereo-Check-SBS.mov")
            ),
            InstalledStereoCheck(
                item: MediaItem(
                    id: BuiltInStereoChecks.overUnderID,
                    title: "Over-Under Stereo Check",
                    fileName: "Stereo-Check-OU.mov",
                    format: .overUnder
                ),
                url: sourceDirectory.appendingPathComponent("Stereo-Check-OU.mov")
            )
        ]
        for installedCheck in installedChecks {
            try Data("fixture".utf8).write(to: installedCheck.url)
        }

        let model = PlayerAppModel(
            libraryStore: LibraryStore(storageURL: temporaryURL()),
            bookmarkStore: BookmarkStore(storageURL: temporaryURL()),
            documentsURL: documentsURL,
            formatInspector: { _ in .mvHEVC },
            stereoCheckInstaller: { installedChecks }
        )

        await model.bootstrap()

        XCTAssertEqual(model.builtInStereoCheckItems.map(\.id), BuiltInStereoChecks.orderedIDs)
        XCTAssertTrue(model.importedItems.isEmpty)
        for item in model.builtInStereoCheckItems {
            XCTAssertEqual(model.sourceStatuses[item.id], .available)
            XCTAssertEqual(model.playbackAvailability(for: item), .playable)
            XCTAssertEqual(model.sourceTitle(for: item), "Built-in Stereo Check")
        }
    }

    func testBundledStereoCheckResourcesInstallAndInspectAsPackedHEVC() async throws {
        let destinationDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerBuiltInInstall")
            .appendingPathComponent(UUID().uuidString, isDirectory: true)

        let installedChecks = try BuiltInStereoChecks.install(destinationDirectory: destinationDirectory)

        XCTAssertEqual(installedChecks.map(\.item.id), BuiltInStereoChecks.orderedIDs)
        for installedCheck in installedChecks {
            XCTAssertTrue(FileManager.default.fileExists(atPath: installedCheck.url.path))
            let detectedFormat = try await MediaFormatInspector.inspect(url: installedCheck.url)
            XCTAssertEqual(detectedFormat, installedCheck.item.format)
            let asset = AVURLAsset(url: installedCheck.url)
            let duration = try await asset.load(.duration)
            let composition = try await PackedStereoComposition.make(
                asset: asset,
                format: installedCheck.item.format,
                duration: duration,
                eyeOrder: .normal,
                spatialMetadataFallback: .qualificationFixture
            )
            XCTAssertEqual(composition.colorYCbCrMatrix, PackedStereoComposition.outputColorYCbCrMatrix)
            XCTAssertEqual(composition.colorPrimaries, PackedStereoComposition.outputColorPrimaries)
            XCTAssertEqual(composition.colorTransferFunction, PackedStereoComposition.outputColorTransferFunction)
        }
    }

    func testBundledStereoCheckInstallSkipsUnchangedVersionedFixtures() throws {
        let destinationDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerBuiltInInstallCache")
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let installedCheck = try BuiltInStereoChecks.install(destinationDirectory: destinationDirectory)[0]
        let preservedDate = Date(timeIntervalSince1970: 1_000_000)
        try FileManager.default.setAttributes([.modificationDate: preservedDate], ofItemAtPath: installedCheck.url.path)

        _ = try BuiltInStereoChecks.install(destinationDirectory: destinationDirectory)

        let attributes = try FileManager.default.attributesOfItem(atPath: installedCheck.url.path)
        XCTAssertEqual(attributes[.modificationDate] as? Date, preservedDate)
    }

    func testPackedStereoFixtureBecomesReadyInAVPlayer() async throws {
        let destinationDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerReadyFixture")
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let installedCheck = try BuiltInStereoChecks.install(destinationDirectory: destinationDirectory)[0]
        let asset = AVURLAsset(url: installedCheck.url)
        let duration = try await asset.load(.duration)
        let item = AVPlayerItem(asset: asset)
        item.videoComposition = try await PackedStereoComposition.make(
            asset: asset,
            format: installedCheck.item.format,
            duration: duration,
            eyeOrder: .normal,
            spatialMetadataFallback: .qualificationFixture
        )
        let player = AVPlayer(playerItem: item)

        for _ in 0 ..< 50 where item.status == .unknown {
            try await Task.sleep(nanoseconds: 100_000_000)
        }

        XCTAssertEqual(
            item.status,
            .readyToPlay,
            "\(item.error.map(String.init(describing:)) ?? "No player item error")"
        )
        player.pause()
    }

    @MainActor
    func testPackedStereoSessionCanReverseEyeOrderAndBecomeReadyAgain() async throws {
        let destinationDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerEyeOrderFixture")
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let installedCheck = try BuiltInStereoChecks.install(destinationDirectory: destinationDirectory)[0]
        let bookmarkStore = BookmarkStore(storageURL: temporaryURL())
        let resumeStore = ResumeStore(storageURL: temporaryURL())
        try bookmarkStore.save(url: installedCheck.url, for: installedCheck.item.id)
        let session = MVHEVCPlayerSession()

        await session.prepare(
            mediaItem: installedCheck.item,
            bookmarkStore: bookmarkStore,
            resumeStore: resumeStore
        )
        for _ in 0 ..< 100 where !session.isReady {
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        XCTAssertTrue(session.isReady, session.failureMessage ?? "Packed stereo session did not become ready.")

        session.toggleEyeSwap()
        for _ in 0 ..< 100 where session.isChangingEyeOrder || !session.isEyeSwapped {
            try await Task.sleep(nanoseconds: 100_000_000)
        }

        XCTAssertTrue(session.isEyeSwapped, session.failureMessage ?? "Eye order did not reverse.")
        XCTAssertFalse(session.isChangingEyeOrder)
        XCTAssertTrue(session.isReady)
        session.finish()
    }

    private func temporaryURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("json")
    }
}

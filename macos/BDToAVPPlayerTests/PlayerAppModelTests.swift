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

        XCTAssertEqual(model.sourceStatuses[item.id], .checking)
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

    func testRefreshingMissingResourceClassifiesSourceAsUnavailable() async throws {
        let sourceURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("mov")
        let item = MediaItem(id: "offline", title: "Offline", fileName: "offline.mov", format: .mvHEVC)
        let libraryStore = LibraryStore(storageURL: temporaryURL())
        try libraryStore.save([item])
        let bookmarkStore = BookmarkStore(
            storageURL: temporaryURL(),
            resolveBookmark: { _ in (sourceURL, false) },
            resourceExists: { _ in false }
        )
        try bookmarkStore.save(bookmarkData: Data([1]), for: item.id)
        let model = PlayerAppModel(
            libraryStore: libraryStore,
            bookmarkStore: bookmarkStore,
            formatInspector: { _ in .mvHEVC },
            stereoCheckInstaller: { [] }
        )

        await model.refreshSourceStatus(for: item.id)

        XCTAssertEqual(model.sourceStatuses[item.id], .unavailable)
        XCTAssertEqual(
            model.playbackAvailability(for: item),
            .unavailable("Try the source again or locate it before playing.")
        )
    }

    func testRefreshingStaleBookmarkMarksSourceAvailableAfterRepair() async throws {
        let sourceURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("mov")
        try FileManager.default.createDirectory(
            at: sourceURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data().write(to: sourceURL)

        let item = MediaItem(id: "moved", title: "Moved", fileName: "moved.mov", format: .mvHEVC)
        let libraryStore = LibraryStore(storageURL: temporaryURL())
        try libraryStore.save([item])
        let refreshedBookmarkData = Data([4, 5, 6])
        let bookmarkStore = BookmarkStore(
            storageURL: temporaryURL(),
            resolveBookmark: { _ in (sourceURL, true) },
            bookmarkDataForURL: { _ in refreshedBookmarkData }
        )
        try bookmarkStore.save(bookmarkData: Data([1]), for: item.id)
        let model = PlayerAppModel(
            libraryStore: libraryStore,
            bookmarkStore: bookmarkStore,
            formatInspector: { _ in .mvHEVC },
            stereoCheckInstaller: { [] }
        )

        await model.refreshSourceStatus(for: item.id)

        XCTAssertEqual(model.sourceStatuses[item.id], .available)
        XCTAssertEqual(bookmarkStore.bookmarkData(for: item.id), refreshedBookmarkData)
    }

    func testNewerSingleSourceProbeWinsOverOlderFullRefresh() async throws {
        let unavailableURL = URL(fileURLWithPath: "/tmp/provider-unavailable.mov")
        let availableURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("mov")
        try FileManager.default.createDirectory(
            at: availableURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data().write(to: availableURL)

        let firstProbeStarted = expectation(description: "first probe started")
        let releaseFirstProbe = DispatchSemaphore(value: 0)
        let attempts = LockedAttemptCounter()
        let item = MediaItem(id: "movie-1", title: "Movie", fileName: "movie.mov", format: .mvHEVC)
        let libraryStore = LibraryStore(storageURL: temporaryURL())
        try libraryStore.save([item])
        let bookmarkStore = BookmarkStore(
            storageURL: temporaryURL(),
            resolveBookmark: { _ in
                if attempts.next() == 1 {
                    firstProbeStarted.fulfill()
                    releaseFirstProbe.wait()
                    return (unavailableURL, false)
                }
                return (availableURL, false)
            },
            resourceExists: { $0 == availableURL }
        )
        try bookmarkStore.save(bookmarkData: Data([1]), for: item.id)
        let model = PlayerAppModel(
            libraryStore: libraryStore,
            bookmarkStore: bookmarkStore,
            formatInspector: { _ in .mvHEVC },
            stereoCheckInstaller: { [] }
        )

        let fullRefresh = Task {
            await model.refreshSourceStatuses()
        }
        await fulfillment(of: [firstProbeStarted], timeout: 1)
        await model.refreshSourceStatus(for: item.id)
        releaseFirstProbe.signal()
        await fullRefresh.value

        XCTAssertEqual(model.sourceStatuses[item.id], .available)
        XCTAssertEqual(attempts.value, 2)
    }

    func testOverlappingFullRefreshRequestsCoalesceIntoFollowupPass() async throws {
        let unavailableURL = URL(fileURLWithPath: "/tmp/provider-unavailable.mov")
        let availableURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("mov")
        try FileManager.default.createDirectory(
            at: availableURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data().write(to: availableURL)

        let firstProbeStarted = expectation(description: "first probe started")
        let releaseFirstProbe = DispatchSemaphore(value: 0)
        let attempts = LockedAttemptCounter()
        let item = MediaItem(id: "movie-1", title: "Movie", fileName: "movie.mov", format: .mvHEVC)
        let libraryStore = LibraryStore(storageURL: temporaryURL())
        try libraryStore.save([item])
        let bookmarkStore = BookmarkStore(
            storageURL: temporaryURL(),
            resolveBookmark: { _ in
                if attempts.next() == 1 {
                    firstProbeStarted.fulfill()
                    releaseFirstProbe.wait()
                    return (unavailableURL, false)
                }
                return (availableURL, false)
            },
            resourceExists: { $0 == availableURL }
        )
        try bookmarkStore.save(bookmarkData: Data([1]), for: item.id)
        let model = PlayerAppModel(
            libraryStore: libraryStore,
            bookmarkStore: bookmarkStore,
            formatInspector: { _ in .mvHEVC },
            stereoCheckInstaller: { [] }
        )

        let firstRefresh = Task {
            await model.refreshSourceStatuses()
        }
        await fulfillment(of: [firstProbeStarted], timeout: 1)
        await model.refreshSourceStatuses()
        releaseFirstProbe.signal()
        await firstRefresh.value

        XCTAssertEqual(model.sourceStatuses[item.id], .available)
        XCTAssertEqual(attempts.value, 2)
    }

    func testLocatePreservesLibraryIdentityAndRestoresAvailability() async throws {
        let replacementURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("mov")
        try FileManager.default.createDirectory(
            at: replacementURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data().write(to: replacementURL)

        let item = MediaItem(id: "movie-1", title: "Movie", fileName: "old.mov", format: .mvHEVC)
        let libraryStore = LibraryStore(storageURL: temporaryURL())
        try libraryStore.save([item])
        let bookmarkStore = BookmarkStore(storageURL: temporaryURL())
        let model = PlayerAppModel(
            libraryStore: libraryStore,
            bookmarkStore: bookmarkStore,
            formatInspector: { _ in .sideBySide },
            stereoCheckInstaller: { [] }
        )

        let located = await model.locate(
            itemID: item.id,
            at: replacementURL,
            shouldShowDetails: false
        )

        XCTAssertTrue(located)
        XCTAssertEqual(model.library.items.count, 1)
        XCTAssertEqual(model.library.items[0].id, item.id)
        XCTAssertEqual(model.library.items[0].title, item.title)
        XCTAssertEqual(model.library.items[0].fileName, replacementURL.lastPathComponent)
        XCTAssertEqual(model.library.items[0].format, .sideBySide)
        XCTAssertEqual(model.sourceStatuses[item.id], .available)
        XCTAssertNotNil(bookmarkStore.bookmarkData(for: item.id))
        XCTAssertFalse(model.isShowingDetails)
    }

    func testLateFullRefreshCannotOverwriteSuccessfulLocate() async throws {
        let oldURL = URL(fileURLWithPath: "/tmp/offline-old.mov")
        let replacementURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("mov")
        try FileManager.default.createDirectory(
            at: replacementURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data().write(to: replacementURL)

        let resolverStarted = expectation(description: "old source probe started")
        let releaseResolver = DispatchSemaphore(value: 0)
        let item = MediaItem(id: "movie-1", title: "Movie", fileName: "old.mov", format: .mvHEVC)
        let libraryStore = LibraryStore(storageURL: temporaryURL())
        try libraryStore.save([item])
        let bookmarkStore = BookmarkStore(
            storageURL: temporaryURL(),
            resolveBookmark: { _ in
                resolverStarted.fulfill()
                releaseResolver.wait()
                return (oldURL, true)
            },
            bookmarkDataForURL: { _ in Data([2]) },
            resourceExists: { _ in true }
        )
        try bookmarkStore.save(bookmarkData: Data([1]), for: item.id)
        let model = PlayerAppModel(
            libraryStore: libraryStore,
            bookmarkStore: bookmarkStore,
            formatInspector: { _ in .mvHEVC },
            stereoCheckInstaller: { [] }
        )

        let refreshTask = Task {
            await model.refreshSourceStatuses()
        }
        await fulfillment(of: [resolverStarted], timeout: 1)

        let located = await model.locate(itemID: item.id, at: replacementURL)
        let locatedBookmarkData = bookmarkStore.bookmarkData(for: item.id)
        releaseResolver.signal()
        await refreshTask.value

        XCTAssertTrue(located)
        XCTAssertEqual(model.sourceStatuses[item.id], .available)
        XCTAssertEqual(model.library.items.count, 1)
        XCTAssertEqual(model.library.items[0].fileName, replacementURL.lastPathComponent)
        XCTAssertEqual(bookmarkStore.bookmarkData(for: item.id), locatedBookmarkData)
        XCTAssertNotEqual(bookmarkStore.bookmarkData(for: item.id), Data([2]))
    }

    func testBootstrapIndexesSupportedMoviesWithoutOpeningDetails() async throws {
        let documentsURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerDocuments")
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: documentsURL, withIntermediateDirectories: true)
        let movieURL = documentsURL.appendingPathComponent("Example.mov")
        let ignoredURL = documentsURL.appendingPathComponent("Notes.txt")
        let discFolder = documentsURL.appendingPathComponent("Disc", isDirectory: true)
        let otherFolder = documentsURL.appendingPathComponent("Other", isDirectory: true)
        try Data().write(to: movieURL)
        try Data().write(to: ignoredURL)
        try FileManager.default.createDirectory(at: discFolder.appendingPathComponent("BDMV"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: otherFolder, withIntermediateDirectories: true)
        try Data().write(to: discFolder.appendingPathComponent("BDMV/index.bdmv"))
        let disc = Bluray3DTitle(
            id: "playlist:00005", playlist: 5, durationSeconds: 60, isMainFeature: true,
            baseViewIsRightEye: false, unsupportedReason: nil, audioTracks: [], subtitleTracks: []
        )

        let model = PlayerAppModel(
            libraryStore: LibraryStore(storageURL: temporaryURL()),
            bookmarkStore: BookmarkStore(storageURL: temporaryURL()),
            documentsURL: documentsURL,
            formatInspector: { _ in .mvHEVC },
            blurayInspector: { _ in Bluray3DSourceInfo(container: .discFolder, titles: [disc]) },
            stereoCheckInstaller: { [] }
        )

        await model.bootstrap()

        XCTAssertEqual(model.library.items.map(\.fileName).sorted(), ["Disc", "Example.mov"])
        XCTAssertEqual(model.sourceTitle(for: model.library.items[0]), "On My Vision Pro")
        XCTAssertEqual(Set(model.sourceStatuses.values), [.available])
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

    func testAddingBluraySourcesStoresTheirTitleAndWaitsForHeadsetPlayback() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let disc = directory.appendingPathComponent("Rainforest.iso")
        let encrypted = directory.appendingPathComponent("Encrypted.iso")
        try Data().write(to: disc)
        try Data().write(to: encrypted)
        func title(_ reason: Bluray3DUnsupportedReason?) -> Bluray3DTitle {
            Bluray3DTitle(
                id: "playlist:00005", playlist: 5, durationSeconds: 2_400, isMainFeature: true,
                baseViewIsRightEye: false, unsupportedReason: reason, audioTracks: [], subtitleTracks: []
            )
        }
        let model = PlayerAppModel(
            libraryStore: LibraryStore(storageURL: temporaryURL()),
            bookmarkStore: BookmarkStore(storageURL: temporaryURL()),
            documentsURL: directory.appendingPathComponent("Documents"),
            formatInspector: { _ in XCTFail("Blu-ray sources are not AVFoundation movies"); return .unsupported },
            blurayInspector: { url in
                Bluray3DSourceInfo(
                    container: .discImage,
                    titles: [title(url.lastPathComponent == "Encrypted.iso" ? .encrypted : nil)]
                )
            },
            stereoCheckInstaller: { [] }
        )

        await model.importMovie(from: disc)
        await model.importMovie(from: encrypted)

        let items = Dictionary(uniqueKeysWithValues: model.library.items.map { ($0.fileName, $0) })
        let playable = try XCTUnwrap(items["Rainforest.iso"])
        XCTAssertEqual(playable.format, .bluray3D)
        XCTAssertEqual(playable.bluray3DTitle, title(nil))
        guard case .planned = model.playbackAvailability(for: playable) else {
            return XCTFail("A playable Blu-ray 3D title waits for headset playback")
        }
        XCTAssertEqual(
            model.playbackAvailability(for: try XCTUnwrap(items["Encrypted.iso"])),
            .unavailable(Bluray3DUnsupportedReason.encrypted.message)
        )
        model.formatFilter = .bluray3D
        XCTAssertEqual(Set(model.visibleItems.map(\.fileName)), ["Rainforest.iso", "Encrypted.iso"])
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

        for _ in 0 ..< 300 where item.status == .unknown {
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
        for _ in 0 ..< 50 where session.player.rate == 0 {
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        XCTAssertGreaterThan(session.player.rate, 0)

        session.toggleEyeSwap()
        for _ in 0 ..< 100 where session.isChangingEyeOrder || !session.isEyeSwapped {
            try await Task.sleep(nanoseconds: 100_000_000)
        }

        XCTAssertTrue(session.isEyeSwapped, session.failureMessage ?? "Eye order did not reverse.")
        XCTAssertFalse(session.isChangingEyeOrder)
        XCTAssertTrue(session.isReady)
        for _ in 0 ..< 50 where session.player.rate == 0 {
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        XCTAssertGreaterThan(session.player.rate, 0)
        session.finish()
    }

    /// A shared movie read through the same byte-range loader as Movie Sharing, from a local file.
    private func makeSharedPlayback(named name: String) throws -> SharedMoviePlayback {
        let destinationDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerSharedResumeFixture")
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let installedCheck = try BuiltInStereoChecks.install(destinationDirectory: destinationDirectory)[0]
        let data = try Data(contentsOf: installedCheck.url)
        let movie = SharedMovie(
            id: MovieLibraryContract.digest(Data("\(UUID())/\(name)".utf8)),
            rootID: UUID().uuidString,
            // The bundled fixture is side-by-side; the name tells the format inspector so.
            fileName: "\(name) SBS.mov",
            byteCount: Int64(data.count),
            revision: MovieLibraryContract.digest(Data(UUID().uuidString.utf8)),
            resumeKey: MovieLibraryContract.digest(Data("\(name):\(data.count)".utf8))
        )
        return SharedMoviePlayback(movie: movie) { _, offset, count in
            data.subdata(in: Int(offset) ..< Int(offset) + count)
        }
    }

    @MainActor
    private func startSharedPlayback(
        _ playback: SharedMoviePlayback,
        session: MVHEVCPlayerSession,
        resumeStore: ResumeStore
    ) async throws {
        await session.prepare(
            mediaItem: playback.mediaItem,
            bookmarkStore: BookmarkStore(storageURL: temporaryURL()),
            resumeStore: resumeStore,
            sharedMovie: playback
        )
        for _ in 0 ..< 100 where !session.isReady {
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        XCTAssertTrue(session.isReady, session.failureMessage ?? "Shared movie did not become ready.")
    }

    @MainActor
    func testSharedMovieSavesItsPositionWhilePlayingWithoutAnyEndEvent() async throws {
        let playback = try makeSharedPlayback(named: "Ocean Dreams")
        let storageURL = temporaryURL()
        let resumeStore = ResumeStore(storageURL: storageURL)
        let session = MVHEVCPlayerSession()
        try await startSharedPlayback(playback, session: session, resumeStore: resumeStore)

        session.play()
        for _ in 0 ..< 50 where resumeStore.resumeTime(for: playback.movie.resumeID) == nil {
            try await Task.sleep(nanoseconds: 100_000_000)
        }

        // A forced quit runs no end path; the position saved during playback survives it.
        let reloaded = ResumeStore(storageURL: storageURL)
        XCTAssertNotNil(reloaded.resumeTime(for: playback.movie.resumeID))
        session.finish()
    }

    @MainActor
    func testSharedMovieSavesWhenTheAppGoesToTheBackgroundAndWhenPlaybackIsClosed() async throws {
        let playback = try makeSharedPlayback(named: "Rainforest")
        let resumeStore = ResumeStore(storageURL: temporaryURL())
        let session = MVHEVCPlayerSession()
        try await startSharedPlayback(playback, session: session, resumeStore: resumeStore)

        session.seek(to: 20)
        for _ in 0 ..< 50 where session.currentTime < 19 {
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        session.handleScenePhase(.inactive)
        session.handleScenePhase(.background)
        XCTAssertEqual(try XCTUnwrap(resumeStore.resumeTime(for: playback.movie.resumeID)), session.currentTime, accuracy: 1)

        session.handleScenePhase(.active)
        session.seek(to: 30)
        for _ in 0 ..< 50 where session.currentTime < 29 {
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        session.finish()
        XCTAssertEqual(try XCTUnwrap(resumeStore.resumeTime(for: playback.movie.resumeID)), 30, accuracy: 1)
    }

    @MainActor
    func testSharedMovieCarriesAPositionSavedUnderTheOldKeyAndResumesThere() async throws {
        let playback = try makeSharedPlayback(named: "Space Station")
        let resumeStore = ResumeStore(storageURL: temporaryURL())
        try resumeStore.setResumeTime(20, for: playback.movie.legacyResumeID)
        let session = MVHEVCPlayerSession()

        try await startSharedPlayback(playback, session: session, resumeStore: resumeStore)
        for _ in 0 ..< 50 where session.currentTime < 19 {
            try await Task.sleep(nanoseconds: 100_000_000)
        }

        XCTAssertEqual(session.currentTime, 20, accuracy: 1.5)
        XCTAssertNil(resumeStore.resumeTime(for: playback.movie.legacyResumeID))
        XCTAssertNotNil(resumeStore.resumeTime(for: playback.movie.resumeID))
        session.finish()
    }

    @MainActor
    func testInactiveSceneKeepsPlayingAndSavesThePositionWhileBackgroundPauses() async throws {
        let destinationDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerScenePhaseFixture")
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
        session.play()
        for _ in 0 ..< 50 where session.currentTime < 1 {
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        XCTAssertGreaterThan(session.player.rate, 0)

        // Control Center or another window makes the scene inactive: playback continues, and the
        // position is saved in case the app is closed from there.
        session.handleScenePhase(.inactive)
        try await Task.sleep(nanoseconds: 250_000_000)
        XCTAssertGreaterThan(session.player.rate, 0)
        XCTAssertTrue(session.canControlPlayback)
        let savedWhileInactive = try XCTUnwrap(resumeStore.resumeTime(for: installedCheck.item.id))
        XCTAssertGreaterThan(savedWhileInactive, 0)

        session.handleScenePhase(.active)
        try await Task.sleep(nanoseconds: 250_000_000)
        XCTAssertGreaterThan(session.player.rate, 0)

        // Leaving the app pauses and saves.
        session.handleScenePhase(.inactive)
        session.handleScenePhase(.background)
        XCTAssertEqual(session.player.rate, 0)
        let savedInBackground = try XCTUnwrap(resumeStore.resumeTime(for: installedCheck.item.id))
        XCTAssertGreaterThanOrEqual(savedInBackground, savedWhileInactive)
        session.finish()
    }

    @MainActor
    func testBackgroundingDuringInitialPreparationPreventsAutoplay() async throws {
        let destinationDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerInactivePreparationFixture")
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let installedCheck = try BuiltInStereoChecks.install(destinationDirectory: destinationDirectory)[0]
        let bookmarkStore = BookmarkStore(storageURL: temporaryURL())
        let resumeStore = ResumeStore(storageURL: temporaryURL())
        try bookmarkStore.save(url: installedCheck.url, for: installedCheck.item.id)
        let session = MVHEVCPlayerSession()

        let preparationTask = Task {
            await session.prepare(
                mediaItem: installedCheck.item,
                bookmarkStore: bookmarkStore,
                resumeStore: resumeStore
            )
        }
        await Task.yield()
        XCTAssertTrue(session.isLoading)
        session.handleScenePhase(.background)
        await preparationTask.value

        for _ in 0 ..< 100 where !session.isReady {
            try await Task.sleep(nanoseconds: 100_000_000)
        }

        XCTAssertTrue(session.isReady, session.failureMessage ?? "Packed stereo session did not become ready.")
        XCTAssertEqual(session.player.rate, 0)

        session.handleScenePhase(.active)
        try await Task.sleep(nanoseconds: 250_000_000)

        XCTAssertEqual(session.player.rate, 0)
        XCTAssertTrue(session.canControlPlayback)

        session.play()
        for _ in 0 ..< 50 where session.player.rate == 0 {
            try await Task.sleep(nanoseconds: 100_000_000)
        }

        XCTAssertGreaterThan(session.player.rate, 0)
        session.finish()
    }

    @MainActor
    func testBackgroundingDuringEyeOrderChangePreventsResume() async throws {
        let destinationDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerInactiveEyeOrderFixture")
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

        session.play()
        for _ in 0 ..< 50 where session.player.rate == 0 {
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        XCTAssertGreaterThan(session.player.rate, 0)

        session.toggleEyeSwap()
        XCTAssertTrue(session.isChangingEyeOrder)
        session.handleScenePhase(.background)

        for _ in 0 ..< 100 where session.isChangingEyeOrder || !session.isEyeSwapped {
            try await Task.sleep(nanoseconds: 100_000_000)
        }

        XCTAssertTrue(session.isEyeSwapped, session.failureMessage ?? "Eye order did not reverse.")
        XCTAssertFalse(session.isChangingEyeOrder)
        XCTAssertEqual(session.player.rate, 0)

        session.handleScenePhase(.active)
        try await Task.sleep(nanoseconds: 250_000_000)

        XCTAssertEqual(session.player.rate, 0)
        session.finish()
    }

    private func temporaryURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("json")
    }

    private final class LockedAttemptCounter: @unchecked Sendable {
        private let lock = NSLock()
        private var storedValue = 0

        var value: Int {
            lock.lock()
            defer { lock.unlock() }
            return storedValue
        }

        func next() -> Int {
            lock.lock()
            storedValue += 1
            let value = storedValue
            lock.unlock()
            return value
        }
    }
}

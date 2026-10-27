import XCTest
@testable import BDToAVPPlayer

@MainActor
final class LocationLibraryModelTests: XCTestCase {
    private var directories: [URL] = []

    override func tearDown() {
        for directory in directories {
            try? FileManager.default.removeItem(at: directory)
        }
        directories = []
        super.tearDown()
    }

    func testAddingAShareListsOnlyPlayableTitlesWithPlainReasonsForTheRest() async throws {
        let share = try makeShare()
        let fixture = makeFixture()
        let locations = fixture.locations(inspector: Self.shareInspector)

        let locationID = try await XCTUnwrapAsync(await locations.add(folderURL: share))

        XCTAssertEqual(locations.statuses[locationID], .available)
        XCTAssertEqual(locations.location(id: locationID)?.name, share.lastPathComponent)
        XCTAssertEqual(
            locations.titles[locationID]?.map(\.title).sorted(),
            ["Avatar", "Movie", "Rainforest", "Rip"]
        )
        XCTAssertEqual(
            Dictionary(uniqueKeysWithValues: (locations.unplayableFiles[locationID] ?? []).map { ($0.relativePath, $0.reason) }),
            [
                "Encrypted.iso": Bluray3DUnsupportedReason.encrypted.message,
                "Flat.mkv": "Not a 3D format this player supports.",
                "Nested/Flat.mp4": "Not a 3D format this player supports.",
            ]
        )
        XCTAssertFalse(locations.truncatedLocations.contains(locationID))
    }

    func testLocationTitlesPlayThroughTheAppModelAndOpenInsideTheShare() async throws {
        let share = try makeShare()
        let fixture = makeFixture()
        let model = PlayerAppModel(
            libraryStore: LibraryStore(storageURL: fixture.directory.appendingPathComponent("library.json")),
            bookmarkStore: fixture.bookmarkStore,
            locationStore: fixture.locationStore,
            documentsURL: fixture.directory.appendingPathComponent("Documents"),
            formatInspector: Self.shareInspector.formatInspector,
            blurayInspector: Self.shareInspector.blurayInspector,
            stereoCheckInstaller: { [] }
        )
        let locationID = try await XCTUnwrapAsync(await model.locations.add(folderURL: share))
        let itemID = LocationLibraryModel.itemID(locationID: locationID, relativePath: "Nested/Movie.mov")

        let item = try XCTUnwrap(model.item(id: itemID))
        XCTAssertEqual(item.format, .mvHEVC)
        XCTAssertEqual(model.sourceTitle(for: item), share.lastPathComponent)
        XCTAssertEqual(model.playbackAvailability(for: item), .playable)
        XCTAssertTrue(model.isLocationItem(itemID))
        XCTAssertTrue(model.library.items.isEmpty, "Location titles are not added to the local library")

        let lease = try await model.bookmarkStore.open(id: itemID)
        defer { lease.close() }
        XCTAssertEqual(
            lease.url.resolvingSymlinksInPath().path,
            share.appendingPathComponent("Nested/Movie.mov").resolvingSymlinksInPath().path
        )
    }

    func testLocationsPersistAndReopenWithoutReadingUnchangedFilesAgain() async throws {
        let share = try makeShare()
        let fixture = makeFixture()
        let locations = fixture.locations(inspector: Self.shareInspector)
        let locationID = try await XCTUnwrapAsync(await locations.add(folderURL: share))
        let firstTitles = locations.titles[locationID]

        let relaunched = LocationLibraryModel(
            store: fixture.locationStore,
            bookmarkStore: BookmarkStore(storageURL: fixture.bookmarksURL),
            inspector: MediaSourceInspector(
                formatInspector: { url in XCTFail("\(url.lastPathComponent) was inspected again"); return .unsupported },
                blurayInspector: { url in XCTFail("\(url.lastPathComponent) was inspected again"); throw Bluray3DSourceError.openFailed }
            )
        )
        XCTAssertEqual(relaunched.locations.map(\.id), [locationID])
        XCTAssertEqual(relaunched.statuses[locationID], .notChecked)

        await relaunched.refresh(locationID)

        XCTAssertEqual(relaunched.statuses[locationID], .available)
        XCTAssertEqual(relaunched.titles[locationID], firstTitles)
    }

    func testAnswersFromAnOlderReaderAreInspectedAgain() async throws {
        let share = try makeShare()
        let fixture = makeFixture()
        let locations = fixture.locations(inspector: Self.shareInspector)
        let locationID = try await XCTUnwrapAsync(await locations.add(folderURL: share))
        // As an earlier build saved Rainforest: refused because its title spans several clips.
        var stored = fixture.locationStore.load()
        stored[0].inspectedFiles = stored[0].inspectedFiles.map { file in
            guard file.relativePath == "Rainforest.iso", let title = file.inspection.bluray3DTitle else { return file }
            let refused = Bluray3DTitle(
                id: title.id, playlist: title.playlist, durationSeconds: title.durationSeconds,
                isMainFeature: title.isMainFeature, baseViewIsRightEye: title.baseViewIsRightEye,
                unsupportedReason: .multipleClips, audioTracks: [], subtitleTracks: []
            )
            return InspectedFile(
                relativePath: file.relativePath, size: file.size, modified: file.modified, fingerprint: file.fingerprint,
                inspection: MediaSourceInspection(format: .bluray3D, bluray3DTitle: refused, readerVersion: nil)
            )
        }
        try fixture.locationStore.save(stored)
        let inspected = AccessProbe()
        let relaunched = LocationLibraryModel(
            store: fixture.locationStore,
            bookmarkStore: BookmarkStore(storageURL: fixture.bookmarksURL),
            inspector: MediaSourceInspector(
                formatInspector: Self.shareInspector.formatInspector,
                blurayInspector: { url in
                    XCTAssertEqual(url.lastPathComponent, "Rainforest.iso")
                    _ = inspected.recordInspection()
                    return try await Self.shareInspector.blurayInspector(url)
                }
            )
        )

        await relaunched.refresh(locationID)

        XCTAssertEqual(inspected.inspections, 1)
        XCTAssertTrue(relaunched.titles[locationID]?.contains { $0.title == "Rainforest" } ?? false)
    }

    func testRefreshPicksUpAddedAndRemovedFiles() async throws {
        let share = try makeShare()
        let fixture = makeFixture()
        let locations = fixture.locations(inspector: Self.shareInspector)
        let locationID = try await XCTUnwrapAsync(await locations.add(folderURL: share))

        try Data().write(to: share.appendingPathComponent("Added.mov"))
        try FileManager.default.removeItem(at: share.appendingPathComponent("Rainforest.iso"))
        await locations.refresh(locationID)

        let titles = Set(locations.titles[locationID]?.map(\.title) ?? [])
        XCTAssertTrue(titles.contains("Added"))
        XCTAssertFalse(titles.contains("Rainforest"))
    }

    func testAnUnreachableShareSaysSoAndCanBeChosenAgain() async throws {
        let share = try makeShare()
        let fixture = makeFixture()
        let model = PlayerAppModel(
            libraryStore: LibraryStore(storageURL: fixture.directory.appendingPathComponent("library.json")),
            bookmarkStore: fixture.bookmarkStore,
            locationStore: fixture.locationStore,
            documentsURL: fixture.directory.appendingPathComponent("Documents"),
            formatInspector: Self.shareInspector.formatInspector,
            blurayInspector: Self.shareInspector.blurayInspector,
            stereoCheckInstaller: { [] }
        )
        let locations = model.locations
        let locationID = try await XCTUnwrapAsync(await locations.add(folderURL: share))
        let itemID = LocationLibraryModel.itemID(locationID: locationID, relativePath: "Rainforest.iso")
        let item = try XCTUnwrap(model.item(id: itemID))

        try FileManager.default.removeItem(at: share)
        await locations.refresh(locationID)

        XCTAssertEqual(locations.statuses[locationID], .unreachable)
        XCTAssertEqual(locations.titles[locationID], [])
        XCTAssertEqual(model.sourceStatus(for: itemID), .unavailable)
        XCTAssertNotEqual(model.playbackAvailability(for: item), .playable)

        let replacement = try makeShare()
        await locations.reconnect(locationID, folderURL: replacement)

        XCTAssertEqual(locations.statuses[locationID], .available)
        XCTAssertNotNil(model.item(id: itemID))
    }

    func testAStalledInspectionKeepsFolderAccessUntilItEndsAndBlocksNewScans() async throws {
        let share = try makeShare()
        let fixture = makeFixture()
        let access = AccessProbe()
        let bookmarkStore = BookmarkStore(storageURL: fixture.bookmarksURL, makeLease: { url in
            SecurityScopedResourceLease(url: url, startAccessing: { access.started() }, stopAccessing: { access.stopped() })
        })
        let locations = LocationLibraryModel(
            store: fixture.locationStore,
            bookmarkStore: bookmarkStore,
            inspector: MediaSourceInspector(
                formatInspector: { _ in
                    // The first read stalls past the timeout, then the share answers again.
                    if access.recordInspection() == 1 {
                        try await Task.sleep(for: .milliseconds(600))
                        access.recordStalledReadFinished()
                    }
                    return .mvHEVC
                },
                blurayInspector: { _ in throw Bluray3DSourceError.unrecognized }
            ),
            inspectionTimeout: .milliseconds(100)
        )

        let locationID = try await XCTUnwrapAsync(await locations.add(folderURL: share))
        XCTAssertEqual(locations.statuses[locationID], .unreachable)
        XCTAssertTrue(locations.hasOutstandingOperations(locationID))

        await locations.refresh(locationID)
        XCTAssertEqual(access.inspections, 1, "A new scan started while the stalled read was still running")

        try await waitUntil { !locations.hasOutstandingOperations(locationID) }
        XCTAssertEqual(access.activeAtStalledReadEnd, true, "Folder access closed before the stalled read ended")
        XCTAssertEqual(access.active, 0)

        await locations.refresh(locationID)
        XCTAssertEqual(locations.statuses[locationID], .available)
        XCTAssertEqual(access.active, 0)
    }

    func testChangingAFileInsideADiscFolderInspectsItAgain() async throws {
        let share = try makeDirectory()
        let disc = share.appendingPathComponent("Avatar", isDirectory: true)
        try makeDisc(at: disc)
        let playlists = disc.appendingPathComponent("BDMV/PLAYLIST", isDirectory: true)
        try FileManager.default.createDirectory(at: playlists, withIntermediateDirectories: true)
        try Data([1]).write(to: playlists.appendingPathComponent("00001.mpls"))
        let fixture = makeFixture()
        let counter = AccessProbe()
        let inspector = MediaSourceInspector(
            formatInspector: { _ in .unsupported },
            blurayInspector: { url in
                _ = counter.recordInspection()
                return try await Self.shareInspector.blurayInspector(url)
            }
        )
        let locations = fixture.locations(inspector: inspector)
        let locationID = try await XCTUnwrapAsync(await locations.add(folderURL: share))
        XCTAssertEqual(counter.inspections, 1)

        // Rewriting a playlist leaves the disc folder's own date unchanged.
        let folderDate = try disc.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate
        try Data([1, 2, 3]).write(to: playlists.appendingPathComponent("00001.mpls"))
        XCTAssertEqual(try disc.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate, folderDate)
        let relaunched = LocationLibraryModel(
            store: fixture.locationStore,
            bookmarkStore: BookmarkStore(storageURL: fixture.bookmarksURL),
            inspector: inspector
        )
        await relaunched.refresh(locationID)

        XCTAssertEqual(counter.inspections, 2, "A changed playlist reused the old inspection")
    }

    func testScannerStopsWithinOneWideFolderAtItsBudgetAndLimit() throws {
        let root = try makeDirectory()
        for index in 0..<20 {
            try Data().write(to: root.appendingPathComponent("Movie \(index).mov"))
        }

        let outOfTime = try LocationScanner.scan(root: root, budget: .zero)
        XCTAssertTrue(outOfTime.candidates.isEmpty)
        XCTAssertTrue(outOfTime.isTruncated)

        let limited = try LocationScanner.scan(root: root, limit: 5)
        XCTAssertEqual(limited.candidates.count, 5)
        XCTAssertTrue(limited.isTruncated)
    }

    func testRemovingALocationForgetsItsAccess() async throws {
        let share = try makeShare()
        let fixture = makeFixture()
        let locations = fixture.locations(inspector: Self.shareInspector)
        let locationID = try await XCTUnwrapAsync(await locations.add(folderURL: share))

        locations.remove(locationID)

        XCTAssertTrue(locations.locations.isEmpty)
        XCTAssertTrue(fixture.locationStore.load().isEmpty)
        XCTAssertNil(fixture.bookmarkStore.bookmarkData(for: LocationLibraryModel.bookmarkID(for: locationID)))
        let itemID = LocationLibraryModel.itemID(locationID: locationID, relativePath: "Rainforest.iso")
        XCTAssertNil(fixture.bookmarkStore.bookmarkData(for: itemID))
    }

    func testScannerReadsAFolderThatIsItselfADisc() throws {
        let disc = try makeDirectory()
        try makeDisc(at: disc)

        let listing = try LocationScanner.scan(root: disc)

        XCTAssertEqual(listing.candidates.map(\.relativePath), [""])
    }

    func testScannerStopsAtItsDepthAndSaysSo() throws {
        let root = try makeDirectory()
        var folder = root
        for level in 1...(LocationScanner.maximumDepth + 1) {
            folder = folder.appendingPathComponent("Level\(level)", isDirectory: true)
        }
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try Data().write(to: folder.appendingPathComponent("TooDeep.mov"))
        try Data().write(to: root.appendingPathComponent("Level1/Shallow.mov"))

        let listing = try LocationScanner.scan(root: root)

        XCTAssertEqual(listing.candidates.map(\.fileName), ["Shallow.mov"])
        XCTAssertTrue(listing.isTruncated)
    }

    func testScannerSaysWhatItCouldNotReadInsteadOfListingNothing() throws {
        let root = try makeDirectory()
        try FileManager.default.createDirectory(
            at: root.appendingPathComponent("Locked", isDirectory: true), withIntermediateDirectories: true
        )
        try Data().write(to: root.appendingPathComponent("Movie.mov"))

        let listing = try LocationScanner.scan(root: root) { url in
            guard url.lastPathComponent != "Locked" else { throw CocoaError(.fileReadNoPermission) }
            return try LocationScanner.coordinatedContents(of: url)
        }
        XCTAssertEqual(listing.candidates.map(\.fileName), ["Movie.mov"])
        XCTAssertEqual(listing.unreadableCount, 1)

        let empty = try LocationScanner.scan(root: try makeDirectory())
        XCTAssertTrue(empty.folderLooksEmpty)
    }

    func testAnEmptyFolderIsShownAsEmptyRatherThanAsHavingNoMovies() async throws {
        let fixture = makeFixture()
        let locations = fixture.locations(inspector: Self.shareInspector)

        let locationID = try await XCTUnwrapAsync(await locations.add(folderURL: try makeDirectory()))

        XCTAssertEqual(locations.statuses[locationID], .available)
        XCTAssertTrue(locations.emptyFolderLocations.contains(locationID))
    }

    func testLocationsAreNamedAsFilesNamesThem() {
        // A share root is a mount folder with a random prefix; Files shows the share's name.
        XCTAssertEqual(
            LocationLibraryModel.displayName(folderName: "fHqWGQmedia", localizedName: "fHqWGQmedia", volumeName: "media"),
            "media"
        )
        XCTAssertEqual(
            LocationLibraryModel.displayName(folderName: "Testers", localizedName: "Testers", volumeName: nil),
            "Testers"
        )
        XCTAssertEqual(LocationLibraryModel.displayName(folderName: "Movies", localizedName: nil, volumeName: ""), "Movies")
        XCTAssertEqual(LocationLibraryModel.displayName(folderName: "/", localizedName: nil, volumeName: nil), "Location")
    }

    // MARK: Fixtures

    /// Formats by file name, standing in for AVFoundation and libbluray.
    nonisolated private static let shareInspector = MediaSourceInspector(
        formatInspector: { url in
            url.pathExtension == "mov" ? .mvHEVC : .unsupported
        },
        blurayInspector: { url in
            if url.lastPathComponent == "Flat.mkv" {
                throw Bluray3DSourceError.unrecognized
            }
            let reason: Bluray3DUnsupportedReason? = url.lastPathComponent == "Encrypted.iso" ? .encrypted : nil
            return Bluray3DSourceInfo(
                container: .discImage,
                titles: [Bluray3DTitle(
                    id: "playlist:00001", playlist: 1, durationSeconds: 5_400, isMainFeature: true,
                    baseViewIsRightEye: false, unsupportedReason: reason, audioTracks: [], subtitleTracks: []
                )]
            )
        }
    )

    private struct Fixture {
        let directory: URL
        let bookmarksURL: URL
        let bookmarkStore: BookmarkStore
        let locationStore: LocationStore

        @MainActor
        func locations(inspector: MediaSourceInspector) -> LocationLibraryModel {
            LocationLibraryModel(store: locationStore, bookmarkStore: bookmarkStore, inspector: inspector)
        }
    }

    private func makeFixture() -> Fixture {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        directories.append(directory)
        let bookmarksURL = directory.appendingPathComponent("bookmarks.json")
        return Fixture(
            directory: directory,
            bookmarksURL: bookmarksURL,
            bookmarkStore: BookmarkStore(storageURL: bookmarksURL),
            locationStore: LocationStore(storageURL: directory.appendingPathComponent("locations.json"))
        )
    }

    /// A share like the testers folder: discs, rips, a movie in a subfolder, and files to skip.
    private func makeShare() throws -> URL {
        let share = try makeDirectory()
        for name in [
            "Rainforest.iso", "Encrypted.iso", "Rip.mkv", "Flat.mkv", "Notes.txt", ".Hidden.mov",
            "@eaDir/Thumb.mov", "Nested/Movie.mov", "Nested/Flat.mp4",
        ] {
            let url = share.appendingPathComponent(name)
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data().write(to: url)
        }
        try makeDisc(at: share.appendingPathComponent("Avatar", isDirectory: true))
        return share
    }

    private func makeDisc(at url: URL) throws {
        let bdmv = url.appendingPathComponent("BDMV", isDirectory: true)
        try FileManager.default.createDirectory(at: bdmv, withIntermediateDirectories: true)
        try Data().write(to: bdmv.appendingPathComponent("index.bdmv"))
    }

    private func makeDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerShares")
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        directories.append(directory)
        return directory
    }

    private func waitUntil(timeout: Duration = .seconds(5), _ condition: () -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: timeout)
        while !condition() {
            guard ContinuousClock.now < deadline else { return XCTFail("Timed out waiting") }
            try await Task.sleep(for: .milliseconds(20))
        }
    }

    private func XCTUnwrapAsync<T>(_ value: T?, file: StaticString = #filePath, line: UInt = #line) async throws -> T {
        try XCTUnwrap(value, file: file, line: line)
    }
}

/// Counts folder access and inspections from any thread.
private final class AccessProbe: @unchecked Sendable {
    private let lock = NSLock()
    private var activeCount = 0
    private var inspectionCount = 0
    private var stalledReadSawAccess: Bool?

    var active: Int { lock.withLock { activeCount } }
    var inspections: Int { lock.withLock { inspectionCount } }
    var activeAtStalledReadEnd: Bool? { lock.withLock { stalledReadSawAccess } }

    func started() -> Bool {
        lock.withLock { activeCount += 1 }
        return true
    }

    func stopped() {
        lock.withLock { activeCount -= 1 }
    }

    func recordInspection() -> Int {
        lock.withLock {
            inspectionCount += 1
            return inspectionCount
        }
    }

    func recordStalledReadFinished() {
        lock.withLock { stalledReadSawAccess = activeCount > 0 }
    }
}

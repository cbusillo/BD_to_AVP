import XCTest
@testable import BDToAVPPlayer

final class LibraryStoreTests: XCTestCase {
    func testCatalogRoundTripPreservesMetadataAndOrder() throws {
        let store = LibraryStore(storageURL: temporaryURL())
        let items = [
            MediaItem(
                id: "movie-1",
                title: "A Movie",
                fileName: "a.mov",
                format: .mvHEVC
            ),
            MediaItem(
                id: "movie-2",
                title: "B Movie",
                fileName: "b.mov",
                format: .unsupported
            )
        ]

        try store.save(items)

        XCTAssertEqual(store.load(), items)
    }

    func testCatalogUpsertReplacesExistingItemWithoutCreatingDuplicate() throws {
        let store = LibraryStore(storageURL: temporaryURL())
        let original = MediaItem(
            id: "movie-1",
            title: "Original",
            fileName: "original.mov",
            format: .sideBySide
        )
        let replacement = MediaItem(
            id: "movie-1",
            title: "Replacement",
            fileName: "replacement.mov",
            format: .mvHEVC
        )

        try store.upsert(original)
        try store.upsert(replacement)

        XCTAssertEqual(store.load(), [replacement])
    }

    func testCorruptCatalogLoadsAsEmptyLibrary() throws {
        let storageURL = temporaryURL()
        try FileManager.default.createDirectory(
            at: storageURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data("not json".utf8).write(to: storageURL)

        XCTAssertTrue(LibraryStore(storageURL: storageURL).load().isEmpty)
    }

    private func temporaryURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("json")
    }
}

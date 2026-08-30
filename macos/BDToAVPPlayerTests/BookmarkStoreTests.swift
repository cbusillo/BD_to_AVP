import XCTest
@testable import BDToAVPPlayer

final class BookmarkStoreTests: XCTestCase {
    func testBookmarkDataJSONRoundTripPreservesOpaqueBytes() throws {
        let source: [String: Data] = [
            "first": Data([0, 1, 2, 255]),
            "second": Data("bookmark".utf8)
        ]

        let encoded = try BookmarkStore.encodeBookmarks(source)
        let decoded = try BookmarkStore.decodeBookmarks(encoded)

        XCTAssertEqual(decoded, source)
    }

    func testBookmarkDataPersistsThroughStoreReload() throws {
        let storageURL = temporaryURL()
        let store = BookmarkStore(storageURL: storageURL)
        let bookmarkData = Data([9, 8, 7, 6])

        try store.save(bookmarkData: bookmarkData, for: "movie-1")

        let reloaded = BookmarkStore(storageURL: storageURL)

        XCTAssertEqual(reloaded.bookmarkData(for: "movie-1"), bookmarkData)
    }

    func testMissingBookmarkIsReportedWithoutResolving() throws {
        let store = BookmarkStore(storageURL: temporaryURL())

        XCTAssertThrowsError(try store.resolve(id: "missing")) { error in
            XCTAssertEqual(error as? BookmarkStoreError, .missingBookmark("missing"))
        }
    }

    func testInvalidIdentifierIsRejected() {
        let store = BookmarkStore(storageURL: temporaryURL())

        XCTAssertThrowsError(try store.save(bookmarkData: Data([1]), for: "")) { error in
            XCTAssertEqual(error as? BookmarkStoreError, .invalidIdentifier)
        }
    }

    func testScopedLeaseStopsExactlyOnceAfterStart() {
        var starts = 0
        var stops = 0
        let lease = SecurityScopedResourceLease(
            url: URL(fileURLWithPath: "/tmp/movie.mov"),
            startAccessing: {
                starts += 1
                return true
            },
            stopAccessing: {
                stops += 1
            }
        )

        lease.close()
        lease.close()

        XCTAssertEqual(starts, 1)
        XCTAssertEqual(stops, 1)
    }

    func testOpeningStaleBookmarkRefreshesDataAndKeepsLeaseBalanced() throws {
        let storageURL = temporaryURL()
        let sourceURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: sourceURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: sourceURL)

        var starts = 0
        var stops = 0
        let refreshedBookmark = Data([4, 5, 6])
        let store = BookmarkStore(
            storageURL: storageURL,
            resolveBookmark: { _ in (sourceURL, true) },
            bookmarkDataForURL: { url in
                XCTAssertEqual(url, sourceURL)
                return refreshedBookmark
            },
            makeLease: { url in
                SecurityScopedResourceLease(
                    url: url,
                    startAccessing: {
                        starts += 1
                        return true
                    },
                    stopAccessing: {
                        stops += 1
                    }
                )
            }
        )
        try store.save(bookmarkData: Data([1, 2, 3]), for: "movie-1")

        let lease = try store.open(id: "movie-1")

        XCTAssertEqual(store.bookmarkData(for: "movie-1"), refreshedBookmark)
        XCTAssertEqual(starts, 1)
        XCTAssertEqual(stops, 0)
        lease.close()
        XCTAssertEqual(stops, 1)
    }

    func testOpeningStaleBookmarkClosesLeaseAndReportsStaleWhenRefreshFails() throws {
        let storageURL = temporaryURL()
        let sourceURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: sourceURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: sourceURL)

        var starts = 0
        var stops = 0
        let store = BookmarkStore(
            storageURL: storageURL,
            resolveBookmark: { _ in (sourceURL, true) },
            bookmarkDataForURL: { _ in throw BookmarkRefreshError.failed },
            makeLease: { url in
                SecurityScopedResourceLease(
                    url: url,
                    startAccessing: {
                        starts += 1
                        return true
                    },
                    stopAccessing: {
                        stops += 1
                    }
                )
            }
        )
        try store.save(bookmarkData: Data([1, 2, 3]), for: "movie-1")

        XCTAssertThrowsError(try store.open(id: "movie-1")) { error in
            XCTAssertEqual(error as? BookmarkStoreError, .staleBookmark("movie-1"))
        }
        XCTAssertEqual(starts, 1)
        XCTAssertEqual(stops, 1)
    }

    private enum BookmarkRefreshError: Error {
        case failed
    }

    private func temporaryURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("json")
    }
}

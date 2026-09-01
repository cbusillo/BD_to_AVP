import XCTest
@testable import BDToAVPPlayer

@MainActor
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

    func testMissingBookmarkIsReportedWithoutResolving() async throws {
        let store = BookmarkStore(storageURL: temporaryURL())

        do {
            _ = try await store.open(id: "missing")
            XCTFail("Expected missing bookmark failure")
        } catch {
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
        let starts = LockedCounter()
        let stops = LockedCounter()
        let lease = SecurityScopedResourceLease(
            url: URL(fileURLWithPath: "/tmp/movie.mov"),
            startAccessing: {
                starts.increment()
                return true
            },
            stopAccessing: {
                stops.increment()
            }
        )

        lease.close()
        lease.close()

        XCTAssertEqual(starts.value, 1)
        XCTAssertEqual(stops.value, 1)
    }

    func testOpeningStaleBookmarkRefreshesDataAndKeepsLeaseBalanced() async throws {
        let storageURL = temporaryURL()
        let sourceURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: sourceURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: sourceURL)

        let starts = LockedCounter()
        let stops = LockedCounter()
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
                        starts.increment()
                        return true
                    },
                    stopAccessing: {
                        stops.increment()
                    }
                )
            }
        )
        try store.save(bookmarkData: Data([1, 2, 3]), for: "movie-1")

        let lease = try await store.open(id: "movie-1")

        XCTAssertEqual(store.bookmarkData(for: "movie-1"), refreshedBookmark)
        XCTAssertEqual(starts.value, 1)
        XCTAssertEqual(stops.value, 0)
        lease.close()
        XCTAssertEqual(stops.value, 1)
    }

    func testOpeningStaleBookmarkRefreshesDataAndReturnsURL() async throws {
        let storageURL = temporaryURL()
        let sourceURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: sourceURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: sourceURL)

        let starts = LockedCounter()
        let stops = LockedCounter()
        let refreshedBookmark = Data([4, 5, 6])
        let store = BookmarkStore(
            storageURL: storageURL,
            resolveBookmark: { _ in (sourceURL, true) },
            bookmarkDataForURL: { _ in refreshedBookmark },
            makeLease: { url in
                SecurityScopedResourceLease(
                    url: url,
                    startAccessing: {
                        starts.increment()
                        return true
                    },
                    stopAccessing: {
                        stops.increment()
                    }
                )
            }
        )
        try store.save(bookmarkData: Data([1, 2, 3]), for: "movie-1")

        let lease = try await store.open(id: "movie-1")
        let resolvedURL = lease.url
        lease.close()

        XCTAssertEqual(resolvedURL, sourceURL)
        XCTAssertEqual(store.bookmarkData(for: "movie-1"), refreshedBookmark)
        XCTAssertEqual(starts.value, 1)
        XCTAssertEqual(stops.value, 1)
    }

    func testOpeningStaleBookmarkClosesLeaseAndReportsStaleWhenRefreshFails() async throws {
        let storageURL = temporaryURL()
        let sourceURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: sourceURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: sourceURL)

        let starts = LockedCounter()
        let stops = LockedCounter()
        let store = BookmarkStore(
            storageURL: storageURL,
            resolveBookmark: { _ in (sourceURL, true) },
            bookmarkDataForURL: { _ in throw BookmarkRefreshError.failed },
            makeLease: { url in
                SecurityScopedResourceLease(
                    url: url,
                    startAccessing: {
                        starts.increment()
                        return true
                    },
                    stopAccessing: {
                        stops.increment()
                    }
                )
            }
        )
        try store.save(bookmarkData: Data([1, 2, 3]), for: "movie-1")

        do {
            _ = try await store.open(id: "movie-1")
            XCTFail("Expected stale bookmark failure")
        } catch {
            XCTAssertEqual(error as? BookmarkStoreError, .staleBookmark("movie-1"))
        }
        XCTAssertEqual(starts.value, 1)
        XCTAssertEqual(stops.value, 1)
    }

    func testOpeningBookmarkDoesNotBlockMainActorWhileProviderResolves() async throws {
        let sourceURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: sourceURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: sourceURL)

        let resolverStarted = expectation(description: "resolver started")
        let releaseResolver = DispatchSemaphore(value: 0)
        let store = BookmarkStore(
            storageURL: temporaryURL(),
            resolveBookmark: { _ in
                resolverStarted.fulfill()
                releaseResolver.wait()
                return (sourceURL, false)
            }
        )
        try store.save(bookmarkData: Data([1]), for: "movie-1")

        let openTask = Task {
            try await store.open(id: "movie-1")
        }
        await fulfillment(of: [resolverStarted], timeout: 1)

        var mainActorHeartbeat = false
        Task { @MainActor in
            mainActorHeartbeat = true
        }
        await Task.yield()

        XCTAssertTrue(mainActorHeartbeat)
        releaseResolver.signal()
        let lease = try await openTask.value
        lease.close()
    }

    func testCancellingBookmarkOpenBeforeResolutionAvoidsStartingResourceAccess() async throws {
        let sourceURL = URL(fileURLWithPath: "/tmp/cancelled.mov")
        let resolverStarted = expectation(description: "resolver started")
        let releaseResolver = DispatchSemaphore(value: 0)
        let starts = LockedCounter()
        let store = BookmarkStore(
            storageURL: temporaryURL(),
            resolveBookmark: { _ in
                resolverStarted.fulfill()
                releaseResolver.wait()
                return (sourceURL, false)
            },
            makeLease: { url in
                SecurityScopedResourceLease(
                    url: url,
                    startAccessing: {
                        starts.increment()
                        return true
                    },
                    stopAccessing: {}
                )
            },
            resourceExists: { _ in true }
        )
        try store.save(bookmarkData: Data([1]), for: "movie-1")

        let openTask = Task {
            try await store.open(id: "movie-1")
        }
        await fulfillment(of: [resolverStarted], timeout: 1)
        openTask.cancel()
        releaseResolver.signal()

        do {
            _ = try await openTask.value
            XCTFail("Expected cancellation")
        } catch is CancellationError {
        }
        XCTAssertEqual(starts.value, 0)
    }

    private enum BookmarkRefreshError: Error {
        case failed
    }

    private final class LockedCounter: @unchecked Sendable {
        private let lock = NSLock()
        private var storedValue = 0

        var value: Int {
            lock.lock()
            defer { lock.unlock() }
            return storedValue
        }

        func increment() {
            lock.lock()
            storedValue += 1
            lock.unlock()
        }
    }

    private func temporaryURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("json")
    }
}

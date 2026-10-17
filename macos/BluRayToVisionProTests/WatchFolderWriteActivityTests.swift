import Darwin
import Foundation
import XCTest
@testable import BluRayToVisionPro

final class WatchFolderWriteActivityTests: XCTestCase {
    private var temporaryURLs: [URL] = []

    override func tearDown() {
        for url in temporaryURLs {
            try? FileManager.default.removeItem(at: url)
        }
        temporaryURLs = []
        super.tearDown()
    }

    // MARK: - Admission while a file is open for writing

    @MainActor
    func testFileOpenForWritingIsHeldUntilTheWriterClosesAndItSettlesAgain() async throws {
        let openFiles = StubOpenFiles()
        let harness = try makeHarness(openFiles: openFiles)
        let folder = try harness.controller.addWatchFolder(at: harness.directory)
        let movieURL = harness.directory.appendingPathComponent("title_t00.mkv")
        try writeFile(movieURL, size: 4)
        let otherURL = harness.directory.appendingPathComponent("Other.bin")
        try writeFile(otherURL, size: 4)
        openFiles.snapshot = .available([
            try openFile(otherURL, processName: "Finder"),
            try openFile(movieURL, processName: "makemkvcon"),
        ])
        let start = harness.clock.now

        _ = try await harness.controller.scan(folder: folder)
        harness.clock.advance(by: 60)
        let heldOutcome = try await harness.controller.scan(folder: folder)

        XCTAssertTrue(harness.queueStore.items.isEmpty)
        XCTAssertEqual(heldOutcome, .scanned(WatchFolderScanReport(
            admittedCount: 0,
            duplicateCount: 0,
            pendingCount: 1,
            nextEvaluationAt: start.addingTimeInterval(120),
            stillBeingWrittenCount: 1
        )))
        XCTAssertEqual(
            harness.controller.writeHoldNotices(for: folder).map(\.hold),
            [.openForWriting(processName: "makemkvcon")]
        )

        // The writer finishes; the file must still stay unchanged for a full interval from the check.
        openFiles.snapshot = .available([])
        harness.clock.advance(by: 59)
        _ = try await harness.controller.scan(folder: folder)
        XCTAssertTrue(harness.queueStore.items.isEmpty)

        harness.clock.advance(by: 1)
        _ = try await harness.controller.scan(folder: folder)

        let item = try XCTUnwrap(harness.queueStore.items.first)
        XCTAssertEqual(item.intent.source.displayName, "title_t00.mkv")
        XCTAssertEqual(item.admission?.arrivedAt, start)
        XCTAssertEqual(item.admission?.settledAt, start.addingTimeInterval(120))
        XCTAssertTrue(harness.controller.writeHoldNotices(for: folder).isEmpty)
    }

    @MainActor
    func testUnreadableOpenFilesFallBackToTheSettleRuleAndSayWhy() async throws {
        let openFiles = StubOpenFiles()
        openFiles.snapshot = .unavailable(reason: "Not permitted.")
        let harness = try makeHarness(openFiles: openFiles)
        let folder = try harness.controller.addWatchFolder(at: harness.directory)
        try writeFile(harness.directory.appendingPathComponent("Movie.mkv"), size: 4)

        _ = try await harness.controller.scan(folder: folder)
        harness.clock.advance(by: 60)
        _ = try await harness.controller.scan(folder: folder)

        XCTAssertEqual(harness.queueStore.items.count, 1)
        XCTAssertEqual(harness.controller.writeCheckFallbackReasons[folder.id], "Not permitted.")
    }

    @MainActor
    func testUnfinishedMakeMKVTitleIsHeldEvenWithoutOpenFileInformation() async throws {
        let openFiles = StubOpenFiles()
        openFiles.snapshot = .unavailable(reason: "Not permitted.")
        let harness = try makeHarness(openFiles: openFiles)
        let folder = try harness.controller.addWatchFolder(at: harness.directory)
        let movieURL = harness.directory.appendingPathComponent("title_t00.mkv")
        try (WatchFolderWriteCompletionCheck.makeMKVUnfinalizedMarker + Data(repeating: 0, count: 64)).write(to: movieURL)

        _ = try await harness.controller.scan(folder: folder)
        harness.clock.advance(by: 60)
        _ = try await harness.controller.scan(folder: folder)
        harness.clock.advance(by: 60)
        _ = try await harness.controller.scan(folder: folder)

        XCTAssertTrue(harness.queueStore.items.isEmpty)
        XCTAssertEqual(harness.controller.writeHoldNotices(for: folder).map(\.hold), [.unfinishedMakeMKVRip])

        // Finishing the title replaces the placeholder with the Matroska header.
        try (Data([0x1A, 0x45, 0xDF, 0xA3]) + Data(repeating: 0, count: 112)).write(to: movieURL)
        harness.clock.advance(by: 1)
        _ = try await harness.controller.scan(folder: folder)
        harness.clock.advance(by: 60)
        _ = try await harness.controller.scan(folder: folder)

        XCTAssertEqual(harness.queueStore.items.count, 1)
        XCTAssertTrue(harness.controller.writeHoldNotices(for: folder).isEmpty)
    }

    // MARK: - Blu-ray folders

    func testBluRayFolderIsHeldWhileAnyFileInsideItIsOpenForWriting() throws {
        let directory = try makeDirectory()
        let disc = directory.appendingPathComponent("Movie Disc", isDirectory: true)
        let stream = disc.appendingPathComponent("BDMV/STREAM/00001.m2ts")
        try writeFile(stream, size: 8)
        let sibling = directory.appendingPathComponent("Movie Disc 2/BDMV/STREAM/00001.m2ts")
        try writeFile(sibling, size: 8)
        let candidate = try XCTUnwrap(
            WatchFolderCandidateScanner.candidates(in: directory, includesSubfolders: false)
                .first { $0.url.lastPathComponent == "Movie Disc" }
        )
        XCTAssertEqual(candidate.kind, .bluRayFolder)

        let siblingOnly = StubOpenFiles()
        siblingOnly.snapshot = .available([try openFile(sibling, processName: "makemkvcon")])
        XCTAssertEqual(WatchFolderWriteCompletionCheck(openFiles: siblingOnly).check([candidate]).holds, [:])

        let inside = StubOpenFiles()
        inside.snapshot = .available([try openFile(stream, processName: "makemkvcon")])
        XCTAssertEqual(
            WatchFolderWriteCompletionCheck(openFiles: inside).check([candidate]).holds,
            [candidate.fingerprint.path: .openForWriting(processName: "makemkvcon")]
        )
    }

    // MARK: - Real open-file information

    func testKernelReportsThisProcessWritingAFileButNotReadingOne() throws {
        let directory = try makeDirectory()
        let movieURL = directory.appendingPathComponent("Movie.mkv")
        try writeFile(movieURL, size: 4)
        let candidate = try XCTUnwrap(
            WatchFolderCandidateScanner.candidates(in: directory, includesSubfolders: false).first
        )
        let check = WatchFolderWriteCompletionCheck.system

        let reader = try FileHandle(forReadingFrom: movieURL)
        XCTAssertNil(check.check([candidate]).holds[candidate.fingerprint.path])
        try reader.close()

        let writer = try FileHandle(forWritingTo: movieURL)
        let whileWriting = check.check([candidate])
        try writer.close()

        XCTAssertNil(whileWriting.fallbackReason)
        guard case .openForWriting = whileWriting.holds[candidate.fingerprint.path] else {
            return XCTFail("Expected the open writer to hold the file, got \(whileWriting)")
        }
        XCTAssertEqual(check.check([candidate]), WatchFolderWriteCheckResult())
    }

    // MARK: - Helpers

    private final class StubOpenFiles: OpenWritableFilesProviding, @unchecked Sendable {
        private let lock = NSLock()
        private var storedSnapshot = OpenWritableFilesSnapshot.available([])

        var snapshot: OpenWritableFilesSnapshot {
            get { lock.withLock { storedSnapshot } }
            set { lock.withLock { storedSnapshot = newValue } }
        }

        func openWritableFiles() -> OpenWritableFilesSnapshot {
            snapshot
        }
    }

    private final class TestClock {
        var now = Date(timeIntervalSince1970: 1_800_000_000)

        func advance(by interval: TimeInterval) {
            now = now.addingTimeInterval(interval)
        }
    }

    private struct Harness {
        let directory: URL
        let clock: TestClock
        let queueStore: ConversionQueueStore
        let controller: QueueAutomationController
    }

    @MainActor
    private func makeHarness(openFiles: StubOpenFiles) throws -> Harness {
        let root = try makeDirectory()
        let directory = root.appendingPathComponent("Watched", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let clock = TestClock()
        let queueStore = ConversionQueueStore.inMemory()
        let controller = QueueAutomationController(
            store: QueueAutomationStore.inMemory(),
            admitter: ConversionViewModel(durableQueueStore: queueStore),
            draftFactory: { _ in
                QueueDraftFactory(
                    profile: BuiltInProfile.balanced.profile,
                    destinationURL: root.appendingPathComponent("Output", isDirectory: true),
                    jobOptions: JobOptions()
                )
            },
            now: { clock.now },
            resolveBookmark: { data in (URL(fileURLWithPath: String(decoding: data, as: UTF8.self), isDirectory: true), false) },
            createBookmark: { url in Data(url.path.utf8) },
            writeCompletionCheck: WatchFolderWriteCompletionCheck(openFiles: openFiles)
        )
        return Harness(directory: directory, clock: clock, queueStore: queueStore, controller: controller)
    }

    /// Describes `url` the way the kernel reports an open file: resolved path, device, and inode.
    private func openFile(_ url: URL, processName: String) throws -> OpenWritableFile {
        var status = stat()
        XCTAssertEqual(stat(url.path, &status), 0)
        let resolved = try XCTUnwrap(realpath(url.path, nil))
        defer { free(resolved) }
        return OpenWritableFile(
            path: String(cString: resolved),
            device: UInt32(bitPattern: status.st_dev),
            inode: UInt64(status.st_ino),
            processID: 1,
            processName: processName
        )
    }

    private func makeDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("WatchFolderWriteActivityTests.\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        temporaryURLs.append(url)
        return url
    }

    private func writeFile(_ url: URL, size: Int) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(repeating: 0, count: size).write(to: url)
    }
}

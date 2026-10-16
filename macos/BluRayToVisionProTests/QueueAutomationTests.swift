import Foundation
import XCTest
@testable import BluRayToVisionPro

final class QueueAutomationTests: XCTestCase {
    private var temporaryURLs: [URL] = []

    override func tearDown() {
        for url in temporaryURLs {
            try? FileManager.default.removeItem(at: url)
        }
        temporaryURLs = []
        super.tearDown()
    }

    // MARK: - Queue file compatibility

    @MainActor
    func testQueueFileWrittenBeforeWatchFoldersLoadsAndSurvivesAnUpdate() async throws {
        let fixtureURL = try XCTUnwrap(
            Bundle(for: Self.self).url(forResource: "queue-v3-before-watch-folders", withExtension: "json")
        )
        let fileURL = try makeDirectory().appendingPathComponent("queue.json")
        try FileManager.default.copyItem(at: fixtureURL, to: fileURL)

        let store = ConversionQueueStore(fileURL: fileURL)

        XCTAssertNil(store.loadErrorMessage)
        XCTAssertFalse(store.writesBlocked)
        XCTAssertEqual(store.items.map(\.state), [.waiting, .failed])
        XCTAssertEqual(store.items.map(\.admission), [nil, nil])
        XCTAssertNoThrow(try store.items.map(PersistentQueueItem.init(item:)))

        let loadedItems = store.items
        try await store.mutateItems { _ in }
        let reloaded = ConversionQueueStore(fileURL: fileURL)
        XCTAssertNil(reloaded.loadErrorMessage)
        XCTAssertEqual(reloaded.items, loadedItems)
    }

    @MainActor
    func testHeldItemsKeepStateAndProvenanceAcrossRelaunch() async throws {
        let fileURL = try makeDirectory().appendingPathComponent("queue.json")
        let viewModel = ConversionViewModel(durableQueueStore: ConversionQueueStore(fileURL: fileURL))
        let admission = makeAdmission()
        _ = try await viewModel.admitAutomatedPersistentQueueDrafts([
            AutomatedQueueDraft(draft: makeDraft("Watched.mkv"), admission: admission, holdForReview: true),
        ])

        let reloaded = ConversionQueueStore(fileURL: fileURL)

        XCTAssertNil(reloaded.loadErrorMessage)
        XCTAssertEqual(reloaded.items.map(\.state), [.heldForReview])
        XCTAssertEqual(reloaded.items.first?.admission, admission)
        XCTAssertEqual(try reloaded.items.map(PersistentQueueItem.init(item:)).map(\.status), [.heldForReview])
    }

    // MARK: - Admission, release, and removal

    @MainActor
    func testAutomatedAdmissionHoldsNewSourcesAndSharesDuplicateProtectionWithManualAdds() async throws {
        let store = ConversionQueueStore.inMemory()
        let viewModel = ConversionViewModel(durableQueueStore: store)
        _ = try await viewModel.appendPersistentQueueDrafts([makeDraft("Manual.mkv")])
        let manualID = try XCTUnwrap(store.items.first?.id)
        let admission = makeAdmission()

        let result = try await viewModel.admitAutomatedPersistentQueueDrafts([
            AutomatedQueueDraft(draft: makeDraft("Manual.mkv"), admission: admission, holdForReview: true),
            AutomatedQueueDraft(draft: makeDraft("Watched.mkv"), admission: admission, holdForReview: true),
            AutomatedQueueDraft(draft: makeDraft("Watched.mkv"), admission: admission, holdForReview: true),
        ])

        XCTAssertEqual(result.addedCount, 1)
        XCTAssertEqual(result.duplicateCount, 2)
        XCTAssertEqual(store.items.map(\.state), [.waiting, .heldForReview])
        XCTAssertEqual(store.items.map(\.admission), [nil, admission])
        XCTAssertEqual(viewModel.selectedPersistentQueueItemID, manualID)
    }

    @MainActor
    func testAutomaticStartPolicyAdmitsSourcesAsWaiting() async throws {
        let store = ConversionQueueStore.inMemory()
        let viewModel = ConversionViewModel(durableQueueStore: store)

        _ = try await viewModel.admitAutomatedPersistentQueueDrafts([
            AutomatedQueueDraft(draft: makeDraft("Watched.mkv"), admission: makeAdmission(), holdForReview: false),
        ])

        XCTAssertEqual(store.items.map(\.state), [.waiting])
        XCTAssertNotNil(store.items.first?.admission)
    }

    @MainActor
    func testStoreRejectsHeldItemWithoutAdmissionProvenance() async throws {
        let store = ConversionQueueStore.inMemory()
        let item = DurableConversionQueueItem(
            ordinal: 0,
            origin: .singleSource,
            intent: DurableQueueItemIntent(draft: makeDraft("Watched.mkv")),
            state: .heldForReview
        )

        do {
            try await store.appendAdmittedItems([item])
            XCTFail("Expected a held item without provenance to be rejected")
        } catch {
            XCTAssertEqual(error as? ConversionQueueStoreError, .invalidDocument)
        }
        XCTAssertTrue(store.items.isEmpty)
    }

    @MainActor
    func testReleaseMovesOnlyHeldItemsToWaiting() async throws {
        let store = ConversionQueueStore.inMemory()
        let viewModel = ConversionViewModel(durableQueueStore: store)
        _ = try await viewModel.appendPersistentQueueDrafts([makeDraft("Manual.mkv")])
        let admission = makeAdmission()
        _ = try await viewModel.admitAutomatedPersistentQueueDrafts([
            AutomatedQueueDraft(draft: makeDraft("Watched.mkv"), admission: admission, holdForReview: true),
        ])
        let manualID = store.items[0].id
        let heldID = store.items[1].id

        do {
            try await viewModel.releaseHeldPersistentQueueItems([manualID, heldID])
            XCTFail("Expected releasing a waiting item to be rejected")
        } catch {
            XCTAssertEqual(error as? ConversionQueueStoreError, .invalidDocument)
        }
        XCTAssertEqual(store.items.map(\.state), [.waiting, .heldForReview])

        try await viewModel.releaseHeldPersistentQueueItems([heldID])

        XCTAssertEqual(store.items.map(\.state), [.waiting, .waiting])
        XCTAssertEqual(store.items[1].admission, admission)
    }

    @MainActor
    func testHeldItemCanBeRemovedAndRestored() async throws {
        let store = ConversionQueueStore.inMemory()
        let viewModel = ConversionViewModel(durableQueueStore: store)
        _ = try await viewModel.admitAutomatedPersistentQueueDrafts([
            AutomatedQueueDraft(draft: makeDraft("Watched.mkv"), admission: makeAdmission(), holdForReview: true),
        ])
        let held = store.items[0]

        let token = try await viewModel.removePersistentQueueItems([held.id])
        XCTAssertTrue(store.items.isEmpty)

        try await viewModel.restorePersistentQueueItems(token)
        XCTAssertEqual(store.items, [held])
    }

    func testHeldItemsNeitherStartNorBlockStartingWaitingItems() throws {
        let held = PersistentQueueItem(ordinal: 0, draft: makeDraft("Watched.mkv"), status: .heldForReview)
        let waiting = PersistentQueueItem(ordinal: 1, draft: makeDraft("Manual.mkv"), status: .waiting)

        let heldOnly = commandState(items: [held], selectedItemID: held.id)
        XCTAssertFalse(heldOnly.canStart)
        XCTAssertTrue(heldOnly.canRemoveSelectedItem)
        XCTAssertFalse(heldOnly.canMoveDown)
        XCTAssertNotNil(heldOnly.selectedItemLockReason)

        XCTAssertTrue(commandState(items: [held, waiting], selectedItemID: nil).canStart)
    }

    func testHeldItemsAreNotRunOutcomesOrStorageCommitments() {
        let held = PersistentQueueItem(ordinal: 0, draft: makeDraft("Watched.mkv"), status: .heldForReview)

        XCTAssertFalse(PersistentQueueOutcomeSummary(items: [held]).hasAnyResults)
        XCTAssertTrue(QueueStorageSummary(items: [held]).destinations.isEmpty)
    }

    // MARK: - Draft defaults

    func testDraftFactoryAppliesProfileDefaultsAndNeverRemovesPhysicalDiscs() {
        var jobOptions = JobOptions()
        jobOptions.removeOriginalAfterSuccess = true
        let profile = BuiltInProfile.balanced.profile
        let factory = QueueDraftFactory(
            profile: profile,
            destinationURL: URL(fileURLWithPath: "/tmp/Output"),
            jobOptions: jobOptions
        )
        let file = ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/tmp/Movie.mkv"))
        let disc = ConversionSource(kind: .physicalDisc, url: URL(fileURLWithPath: "/Volumes/MOVIE"))

        let drafts = factory.drafts(for: [file, disc])

        XCTAssertEqual(drafts.map(\.source), [file, disc])
        XCTAssertEqual(drafts.map(\.profile), [profile, profile])
        XCTAssertEqual(drafts.map(\.options.encoding), [profile.options, profile.options])
        XCTAssertEqual(drafts.map(\.options.job.removeOriginalAfterSuccess), [true, false])
        XCTAssertEqual(Set(drafts.map(\.destinationURL)), [URL(fileURLWithPath: "/tmp/Output")])
    }

    // MARK: - Seen-sources record

    @MainActor
    func testSeenSourcesPersistExactlyAndCanBeForgotten() throws {
        let fileURL = try makeDirectory().appendingPathComponent("queue.automation.json")
        let store = QueueAutomationStore(fileURL: fileURL)
        let folder = WatchFolder(bookmark: Data("folder".utf8), displayName: "Incoming")
        try store.saveWatchFolder(folder)
        let fingerprint = WatchFolderSourceFingerprint(
            path: "/Volumes/Media/Incoming/Movie.mkv",
            sizeBytes: 42,
            modifiedAt: Date(timeIntervalSince1970: 1_800_000_000.123_456)
        )
        let other = WatchFolderSourceFingerprint(path: "/Volumes/Media/Incoming/Other.mkv", sizeBytes: 7, modifiedAtMilliseconds: 1)
        try store.recordSeen([fingerprint, other], watchFolderID: folder.id, at: Date())

        let reloaded = QueueAutomationStore(fileURL: fileURL)
        XCTAssertNil(reloaded.loadErrorMessage)
        XCTAssertEqual(reloaded.watchFolders, [folder])
        XCTAssertTrue(reloaded.hasSeen(fingerprint))
        XCTAssertFalse(reloaded.hasSeen(WatchFolderSourceFingerprint(
            path: fingerprint.path,
            sizeBytes: fingerprint.sizeBytes + 1,
            modifiedAtMilliseconds: fingerprint.modifiedAtMilliseconds
        )))

        try reloaded.forgetSeenSource(path: fingerprint.path)
        XCTAssertFalse(reloaded.hasSeen(fingerprint))
        XCTAssertTrue(reloaded.hasSeen(other))

        try reloaded.removeWatchFolder(id: folder.id)
        XCTAssertFalse(QueueAutomationStore(fileURL: fileURL).hasSeen(other))
    }

    @MainActor
    func testUnreadableAutomationSettingsBlockWritesWithoutReplacingThem() throws {
        let fileURL = try makeDirectory().appendingPathComponent("queue.automation.json")
        let original = Data("{\"version\": 99}".utf8)
        try original.write(to: fileURL)

        let store = QueueAutomationStore(fileURL: fileURL)

        XCTAssertTrue(store.writesBlocked)
        XCTAssertThrowsError(try store.saveWatchFolder(WatchFolder(bookmark: Data(), displayName: "Incoming")))
        XCTAssertEqual(try Data(contentsOf: fileURL), original)
    }

    func testWatchFolderWithoutOptionalSettingsDecodesWithSafeDefaults() throws {
        let json = """
        {"id": "6A51BC62-1262-4C1C-8CD5-7787B41CBA7D", "bookmark": "AA==", "displayName": "Incoming"}
        """

        let folder = try JSONDecoder().decode(WatchFolder.self, from: Data(json.utf8))

        XCTAssertEqual(folder.mode, .holdForReview)
        XCTAssertTrue(folder.isEnabled)
        XCTAssertFalse(folder.includesSubfolders)
        XCTAssertNil(folder.profileID)
    }

    // MARK: - Settling

    func testCandidateSettlesOnlyAfterStayingUnchangedForTheInterval() {
        var evaluator = WatchFolderSettleEvaluator(settleInterval: 60)
        let start = Date(timeIntervalSince1970: 1_800_000_000)
        let candidate = makeCandidate(size: 10)

        let first = evaluator.evaluate([candidate], at: start)
        XCTAssertTrue(first.settled.isEmpty)
        XCTAssertEqual(first.nextEvaluationAt, start.addingTimeInterval(60))

        XCTAssertTrue(evaluator.evaluate([candidate], at: start.addingTimeInterval(59)).settled.isEmpty)

        let settled = evaluator.evaluate([candidate], at: start.addingTimeInterval(60))
        XCTAssertEqual(settled.settled, [SettledWatchFolderCandidate(
            candidate: candidate,
            arrivedAt: start,
            settledAt: start.addingTimeInterval(60)
        )])
        XCTAssertNil(settled.nextEvaluationAt)
    }

    func testChangingSizeOrModificationTimeRestartsSettling() {
        var evaluator = WatchFolderSettleEvaluator(settleInterval: 60)
        let start = Date(timeIntervalSince1970: 1_800_000_000)
        _ = evaluator.evaluate([makeCandidate(size: 10)], at: start)

        let grown = evaluator.evaluate([makeCandidate(size: 20)], at: start.addingTimeInterval(50))
        XCTAssertTrue(grown.settled.isEmpty)
        XCTAssertEqual(grown.nextEvaluationAt, start.addingTimeInterval(110))

        let touched = evaluator.evaluate([makeCandidate(size: 20, modified: 5)], at: start.addingTimeInterval(100))
        XCTAssertTrue(touched.settled.isEmpty)

        let settled = evaluator.evaluate([makeCandidate(size: 20, modified: 5)], at: start.addingTimeInterval(160))
        XCTAssertEqual(settled.settled.map(\.arrivedAt), [start])
    }

    func testCandidateThatDisappearsOrSeesTheClockGoBackwardStartsOver() {
        var evaluator = WatchFolderSettleEvaluator(settleInterval: 60)
        let start = Date(timeIntervalSince1970: 1_800_000_000)
        let candidate = makeCandidate(size: 10)
        _ = evaluator.evaluate([candidate], at: start)

        _ = evaluator.evaluate([], at: start.addingTimeInterval(30))
        let returned = evaluator.evaluate([candidate], at: start.addingTimeInterval(70))
        XCTAssertTrue(returned.settled.isEmpty)

        let rewound = evaluator.evaluate([candidate], at: start.addingTimeInterval(10))
        XCTAssertTrue(rewound.settled.isEmpty)
        XCTAssertEqual(evaluator.evaluate([candidate], at: start.addingTimeInterval(70)).settled.count, 1)
    }

    // MARK: - Candidate detection

    func testScannerFindsMoviesAndSkipsPartialHiddenAndUnsupportedFiles() throws {
        let directory = try makeDirectory()
        for name in ["Movie.mkv", "Disc.iso", "Clip.m2ts", "Movie.mkv.part", "Download.tmp", ".Hidden.mkv", "Notes.txt", "Backup.mkv~"] {
            try writeFile(directory.appendingPathComponent(name), size: 4)
        }

        let names = try WatchFolderCandidateScanner.candidates(in: directory, includesSubfolders: false)
            .map(\.url.lastPathComponent)

        XCTAssertEqual(Set(names), ["Movie.mkv", "Disc.iso", "Clip.m2ts"])
    }

    func testScannerReportsBluRayFoldersWholeAndOnlyDescendsWhenAsked() throws {
        let directory = try makeDirectory()
        let disc = directory.appendingPathComponent("Movie Disc", isDirectory: true)
        try writeFile(disc.appendingPathComponent("BDMV/STREAM/00001.m2ts"), size: 8)
        try writeFile(directory.appendingPathComponent("Season/Episode.mkv"), size: 4)

        let topLevel = try WatchFolderCandidateScanner.candidates(in: directory, includesSubfolders: false)
        XCTAssertEqual(topLevel.map(\.url.lastPathComponent), ["Movie Disc"])
        XCTAssertEqual(topLevel.map(\.kind), [.bluRayFolder])

        let nested = try WatchFolderCandidateScanner.candidates(in: directory, includesSubfolders: true)
        XCTAssertEqual(Set(nested.map(\.url.lastPathComponent)), ["Movie Disc", "Episode.mkv"])

        let before = try XCTUnwrap(topLevel.first?.fingerprint)
        try writeFile(disc.appendingPathComponent("BDMV/STREAM/00002.m2ts"), size: 8)
        let after = try XCTUnwrap(
            WatchFolderCandidateScanner.candidates(in: directory, includesSubfolders: false).first?.fingerprint
        )
        XCTAssertNotEqual(before, after)
    }

    // MARK: - Scanning a watch folder

    @MainActor
    func testScanAdmitsSettledSourceHeldForReviewWithProvenance() async throws {
        let harness = try makeHarness()
        let folder = try harness.controller.addWatchFolder(at: harness.directory)
        try writeFile(harness.directory.appendingPathComponent("Movie.mkv"), size: 4)
        let start = harness.clock.now

        let firstOutcome = try await harness.controller.scan(folder: folder)
        XCTAssertEqual(firstOutcome, .scanned(WatchFolderScanReport(
            admittedCount: 0,
            duplicateCount: 0,
            pendingCount: 1,
            nextEvaluationAt: start.addingTimeInterval(60)
        )))
        harness.clock.advance(by: 60)
        let outcome = try await harness.controller.scan(folder: folder)

        guard case let .scanned(report) = outcome else {
            return XCTFail("Expected a scan report, got \(outcome)")
        }
        XCTAssertEqual(report.admittedCount, 1)
        XCTAssertEqual(report.pendingCount, 0)
        let item = try XCTUnwrap(harness.queueStore.items.first)
        XCTAssertEqual(item.state, .heldForReview)
        XCTAssertEqual(item.intent.source.displayName, "Movie.mkv")
        XCTAssertEqual(item.admission?.origin, .watchFolder)
        XCTAssertEqual(item.admission?.watchFolderID, folder.id)
        XCTAssertEqual(item.admission?.watchFolderName, folder.displayName)
        XCTAssertEqual(item.admission?.arrivedAt, start)
        XCTAssertEqual(item.admission?.settledAt, start.addingTimeInterval(60))
    }

    @MainActor
    func testScanWaitsForGrowingFileToStopChanging() async throws {
        let harness = try makeHarness()
        let folder = try harness.controller.addWatchFolder(at: harness.directory)
        let movieURL = harness.directory.appendingPathComponent("Movie.mkv")
        try writeFile(movieURL, size: 4)

        _ = try await harness.controller.scan(folder: folder)
        harness.clock.advance(by: 60)
        try writeFile(movieURL, size: 8)
        _ = try await harness.controller.scan(folder: folder)
        XCTAssertTrue(harness.queueStore.items.isEmpty)

        harness.clock.advance(by: 60)
        _ = try await harness.controller.scan(folder: folder)
        XCTAssertEqual(harness.queueStore.items.count, 1)
    }

    @MainActor
    func testRemovedItemIsNotReadmittedUntilItsRecordIsForgotten() async throws {
        let harness = try makeHarness()
        let folder = try harness.controller.addWatchFolder(at: harness.directory)
        try writeFile(harness.directory.appendingPathComponent("Movie.mkv"), size: 4)
        try await scanUntilSettled(harness, folder)
        let admittedID = try XCTUnwrap(harness.queueStore.items.first?.id)

        _ = try await harness.viewModel.removePersistentQueueItems([admittedID])
        try await scanUntilSettled(harness, folder)
        XCTAssertTrue(harness.queueStore.items.isEmpty)

        let restarted = QueueAutomationController(
            store: QueueAutomationStore(fileURL: harness.automationFileURL),
            admitter: harness.viewModel,
            draftFactory: harness.draftFactory,
            now: { harness.clock.now },
            resolveBookmark: Self.resolvePathBookmark,
            createBookmark: Self.createPathBookmark
        )
        try await scanUntilSettled(harness, folder, controller: restarted)
        XCTAssertTrue(harness.queueStore.items.isEmpty)

        try restarted.forgetSeenSources(watchFolderID: folder.id)
        try await scanUntilSettled(harness, folder, controller: restarted)
        XCTAssertEqual(harness.queueStore.items.count, 1)
    }

    @MainActor
    func testAutomaticStartFolderUsesItsProfileAndAdmitsWaitingItems() async throws {
        let directory = try makeDirectory()
        let settings = AppSettings(defaults: try isolatedDefaults(), homeDirectoryURL: directory)
        settings.destinationURL = directory.appendingPathComponent("Output", isDirectory: true)
        let profileStore = ProfileStore(fileURL: directory.appendingPathComponent("profiles.json"))
        let queueStore = ConversionQueueStore.inMemory()
        let viewModel = ConversionViewModel(durableQueueStore: queueStore)
        let clock = TestClock()
        let watched = directory.appendingPathComponent("Watched", isDirectory: true)
        try FileManager.default.createDirectory(at: watched, withIntermediateDirectories: true)
        let controller = QueueAutomationController(
            store: QueueAutomationStore.inMemory(),
            admitter: viewModel,
            draftFactory: { folder in
                QueueDraftFactory(settings: settings, profileStore: profileStore, profileID: folder.profileID)
            },
            now: { clock.now },
            resolveBookmark: Self.resolvePathBookmark,
            createBookmark: Self.createPathBookmark
        )
        let folder = try controller.addWatchFolder(
            at: watched,
            mode: .startAutomatically,
            profileID: BuiltInProfile.originalResolution.id
        )
        try writeFile(watched.appendingPathComponent("Movie.mkv"), size: 4)

        _ = try await controller.scan(folder: folder)
        clock.advance(by: 60)
        _ = try await controller.scan(folder: folder)

        let item = try XCTUnwrap(queueStore.items.first)
        XCTAssertEqual(item.state, .waiting)
        XCTAssertEqual(item.intent.profile.id, BuiltInProfile.originalResolution.id)
        XCTAssertEqual(item.intent.destinationPath, settings.destinationURL.standardizedFileURL.path)
    }

    @MainActor
    func testDisabledAndUnreachableFoldersAdmitNothing() async throws {
        let harness = try makeHarness()
        var folder = try harness.controller.addWatchFolder(at: harness.directory)
        try writeFile(harness.directory.appendingPathComponent("Movie.mkv"), size: 4)

        folder.isEnabled = false
        try harness.controller.updateWatchFolder(folder)
        let disabledOutcome = try await harness.controller.scan(folder: folder)
        XCTAssertEqual(disabledOutcome, .disabled)

        folder.isEnabled = true
        try harness.controller.updateWatchFolder(folder)
        try FileManager.default.removeItem(at: harness.directory)
        let unavailableOutcome = try await harness.controller.scan(folder: folder)
        XCTAssertEqual(unavailableOutcome, .unavailable)
        XCTAssertTrue(harness.queueStore.items.isEmpty)
    }

    // MARK: - Helpers

    private final class TestClock {
        var now = Date(timeIntervalSince1970: 1_800_000_000)

        func advance(by interval: TimeInterval) {
            now = now.addingTimeInterval(interval)
        }
    }

    private struct Harness {
        let directory: URL
        let automationFileURL: URL
        let clock: TestClock
        let queueStore: ConversionQueueStore
        let viewModel: ConversionViewModel
        let draftFactory: QueueAutomationController.DraftFactoryProvider
        let controller: QueueAutomationController
    }

    @MainActor
    private func makeHarness() throws -> Harness {
        let root = try makeDirectory()
        let directory = root.appendingPathComponent("Watched", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let automationFileURL = root.appendingPathComponent("queue.automation.json")
        let clock = TestClock()
        let queueStore = ConversionQueueStore.inMemory()
        let viewModel = ConversionViewModel(durableQueueStore: queueStore)
        let draftFactory: QueueAutomationController.DraftFactoryProvider = { _ in
            QueueDraftFactory(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: root.appendingPathComponent("Output", isDirectory: true),
                jobOptions: JobOptions()
            )
        }
        let controller = QueueAutomationController(
            store: QueueAutomationStore(fileURL: automationFileURL),
            admitter: viewModel,
            draftFactory: draftFactory,
            now: { clock.now },
            resolveBookmark: Self.resolvePathBookmark,
            createBookmark: Self.createPathBookmark
        )
        return Harness(
            directory: directory,
            automationFileURL: automationFileURL,
            clock: clock,
            queueStore: queueStore,
            viewModel: viewModel,
            draftFactory: draftFactory,
            controller: controller
        )
    }

    @MainActor
    private func scanUntilSettled(
        _ harness: Harness,
        _ folder: WatchFolder,
        controller: QueueAutomationController? = nil
    ) async throws {
        let controller = controller ?? harness.controller
        _ = try await controller.scan(folder: folder)
        harness.clock.advance(by: controller.settleInterval)
        _ = try await controller.scan(folder: folder)
    }

    private static func resolvePathBookmark(_ data: Data) throws -> (url: URL, isStale: Bool) {
        (URL(fileURLWithPath: String(decoding: data, as: UTF8.self), isDirectory: true), false)
    }

    private static func createPathBookmark(_ url: URL) throws -> Data {
        Data(url.path.utf8)
    }

    private func makeDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("QueueAutomationTests.\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        temporaryURLs.append(url)
        return url
    }

    private func writeFile(_ url: URL, size: Int) throws {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data(repeating: 0, count: size).write(to: url)
    }

    private func makeCandidate(size: Int64, modified: Int64 = 0) -> WatchFolderCandidate {
        WatchFolderCandidate(
            url: URL(fileURLWithPath: "/Volumes/Media/Incoming/Movie.mkv"),
            kind: .matroska,
            fingerprint: WatchFolderSourceFingerprint(
                path: "/Volumes/Media/Incoming/Movie.mkv",
                sizeBytes: size,
                modifiedAtMilliseconds: modified
            )
        )
    }

    private func makeAdmission() -> DurableQueueAdmission {
        let arrivedAt = Date(timeIntervalSince1970: 1_800_000_000)
        return .watchFolder(
            id: UUID(),
            name: "Incoming",
            arrivedAt: arrivedAt,
            settledAt: arrivedAt.addingTimeInterval(60),
            admittedAt: arrivedAt.addingTimeInterval(61)
        )
    }

    private func makeDraft(_ name: String) -> ConversionDraft {
        ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/Volumes/Media/Incoming/\(name)")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/Output"),
            options: ConversionOptions()
        )
    }

    private func commandState(items: [PersistentQueueItem], selectedItemID: UUID?) -> PersistentQueueCommandState {
        PersistentQueueCommandState(
            items: items,
            selectedItemID: selectedItemID,
            runState: .idle,
            hasActiveWorker: false,
            hasPreviewWorker: false,
            offPeakSchedule: nil,
            insertedDiscs: [],
            removalTokenIsValid: false
        )
    }
}

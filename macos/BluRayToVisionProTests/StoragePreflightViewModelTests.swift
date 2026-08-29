import Foundation
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class StoragePreflightViewModelTests: XCTestCase {
    func testInsufficientStorageIsParkedBeforeWorkerSpawnAndPausesQueue() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let preflight = StubQueueStoragePreflight(verdict: .insufficient(requiredBytes: 12, availableBytes: 4))
        let workerFactory = WorkerFactoryCounter()
        let viewModel = ConversionViewModel(
            clientFactory: {
                workerFactory.count += 1
                throw StoragePreflightTestError.workerSpawned
            },
            durableQueueStore: queueStore,
            sourceAvailabilityResolver: { _ in true },
            queueStoragePreflight: preflight
        )
        let draft = makeDraft(destination: "/Volumes/Full")
        _ = try await viewModel.appendPersistentQueueDrafts([draft])
        let itemID = try XCTUnwrap(queueStore.items.first?.id)

        _ = await viewModel.startPersistentQueue()
        while queueStore.items.first?.state != .failed {
            await Task.yield()
        }
        while viewModel.persistentQueueRunState != .paused {
            await Task.yield()
        }

        XCTAssertEqual(workerFactory.count, 0)
        XCTAssertEqual(queueStore.items.first?.failure?.code, "destination_insufficient_capacity")
        XCTAssertEqual(viewModel.persistentQueueRunState, .paused)
        XCTAssertTrue(queueStore.items.first?.attempts.isEmpty == true)
        XCTAssertEqual(itemID, queueStore.items.first?.id)
    }

    func testUnavailableDestinationContinuesToAnUnrelatedAdoptedItem() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let preflight = StubQueueStoragePreflight(verdicts: [
            "/Volumes/Missing": .unavailable("Destination is disconnected."),
            "/Volumes/Available": .unconfirmed("Free space is advisory."),
        ])
        let workerFactory = WorkerFactoryCounter()
        let viewModel = ConversionViewModel(
            clientFactory: {
                workerFactory.count += 1
                throw StoragePreflightTestError.workerSpawned
            },
            durableQueueStore: queueStore,
            sourceAvailabilityResolver: { _ in true },
            queueStoragePreflight: preflight
        )
        _ = try await viewModel.appendPersistentQueueDrafts([
            makeDraft(destination: "/Volumes/Missing", sourcePath: "/Sources/missing.mkv"),
            makeDraft(destination: "/Volumes/Available", sourcePath: "/Sources/available.mkv"),
        ])

        _ = await viewModel.startPersistentQueue()
        while queueStore.items.contains(where: { $0.state == .waiting || $0.state == .processing }) {
            await Task.yield()
        }

        XCTAssertEqual(workerFactory.count, 1)
        XCTAssertEqual(queueStore.items[0].failure?.code, "destination_unavailable")
        XCTAssertNotEqual(queueStore.items[1].failure?.code, "destination_unavailable")
        XCTAssertTrue(preflight.paths.contains("/Volumes/Available"))
    }

    func testRetryableStorageFailureCanBeReadoptedAfterDestinationRecovers() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let preflight = StubQueueStoragePreflight(verdict: .unavailable("Destination is disconnected."))
        let viewModel = ConversionViewModel(
            clientFactory: { throw StoragePreflightTestError.workerSpawned },
            durableQueueStore: queueStore,
            sourceAvailabilityResolver: { _ in true },
            queueStoragePreflight: preflight
        )
        _ = try await viewModel.appendPersistentQueueDrafts([makeDraft(destination: "/Volumes/Recovering")])
        let itemID = try XCTUnwrap(queueStore.items.first?.id)

        _ = await viewModel.startPersistentQueue()
        while queueStore.items.first?.state != .failed {
            await Task.yield()
        }
        XCTAssertEqual(queueStore.items.first?.failure?.retryable, true)

        preflight.verdict = .unconfirmed("Free space is advisory.")
        let adopted = await viewModel.adoptPersistentQueueItem(itemID)
        XCTAssertTrue(adopted)
        while queueStore.items.first?.state == .waiting {
            await Task.yield()
        }
        XCTAssertNotEqual(queueStore.items.first?.failure?.code, "destination_unavailable")
    }

    func testStorageFailureDestinationChangeReturnsItemToWaiting() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let preflight = StubQueueStoragePreflight(verdict: .unavailable("Destination is disconnected."))
        let viewModel = ConversionViewModel(
            clientFactory: { throw StoragePreflightTestError.workerSpawned },
            durableQueueStore: queueStore,
            sourceAvailabilityResolver: { _ in true },
            queueStoragePreflight: preflight
        )
        _ = try await viewModel.appendPersistentQueueDrafts([makeDraft(destination: "/Volumes/Missing")])
        let itemID = try XCTUnwrap(queueStore.items.first?.id)

        _ = await viewModel.startPersistentQueue()
        while queueStore.items.first?.state != .failed {
            await Task.yield()
        }

        try await viewModel.updatePersistentQueueItemDestination(
            itemID,
            destinationURL: URL(fileURLWithPath: "/Volumes/Recovered", isDirectory: true)
        )

        XCTAssertEqual(queueStore.items.first?.state, .waiting)
        XCTAssertNil(queueStore.items.first?.failure)
        XCTAssertEqual(queueStore.items.first?.intent.destinationPath, "/Volumes/Recovered")
    }

    private func makeDraft(destination: String, sourcePath: String = "/Sources/movie.mkv") -> ConversionDraft {
        var options = ConversionOptions()
        options.encoding.mvHEVC.directFinalBitrate = BitratePreference(mode: .custom, customMbps: 40)
        return ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: sourcePath)),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: destination, isDirectory: true),
            options: options,
            selectedTitle: SourceTitle(
                id: "title-1",
                name: "Movie",
                outputName: "Movie",
                durationSeconds: 3_600,
                resolution: "1920x1080",
                frameRate: "24/1",
                mainFeature: true
            )
        )
    }
}

private enum StoragePreflightTestError: Error {
    case workerSpawned
}

private final class WorkerFactoryCounter: @unchecked Sendable {
    var count = 0
}

private final class StubQueueStoragePreflight: QueueStoragePreflighting, @unchecked Sendable {
    var verdict: QueueStoragePreflightVerdict = .unconfirmed("Free space is advisory.")
    var verdicts: [String: QueueStoragePreflightVerdict] = [:]
    private(set) var paths: [String] = []

    init(verdict: QueueStoragePreflightVerdict) {
        self.verdict = verdict
    }

    init(verdicts: [String: QueueStoragePreflightVerdict]) {
        self.verdicts = verdicts
    }

    func preflight(destinationURL: URL, requiredBytes: Int64?) -> QueueStoragePreflightVerdict {
        paths.append(destinationURL.standardizedFileURL.path)
        return verdicts[destinationURL.standardizedFileURL.path] ?? verdict
    }
}

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
        XCTAssertEqual(viewModel.persistentQueueCompletionRevision, 0)
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
        XCTAssertEqual(viewModel.persistentQueueCompletionRevision, 1)
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

    func testChangedUnavailableDestinationRejoinsRunningQueue() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let preflight = StubQueueStoragePreflight(verdicts: [
            "/Volumes/Missing": .unavailable("Destination is disconnected."),
            "/Volumes/Available": .unconfirmed("Free space is advisory."),
            "/Volumes/Recovered": .unconfirmed("Free space is advisory."),
        ])
        let holdingWorker = HoldingConversionWorkerClient()
        let workerFactory = WorkerFactoryCounter()
        let viewModel = ConversionViewModel(
            clientFactory: {
                workerFactory.count += 1
                if workerFactory.count == 1 {
                    return holdingWorker
                }
                throw StoragePreflightTestError.workerSpawned
            },
            durableQueueStore: queueStore,
            sourceAvailabilityResolver: { _ in true },
            queueStoragePreflight: preflight
        )
        _ = try await viewModel.appendPersistentQueueDrafts([
            makeDraft(destination: "/Volumes/Missing", sourcePath: "/Sources/missing.mkv", inspected: true),
            makeDraft(destination: "/Volumes/Available", sourcePath: "/Sources/available.mkv", inspected: true),
        ])
        let itemID = try XCTUnwrap(queueStore.items.first?.id)

        _ = await viewModel.startPersistentQueue()
        while queueStore.items[0].state != .failed || queueStore.items[1].state != .processing {
            await Task.yield()
        }
        XCTAssertEqual(viewModel.persistentQueueRunState, .running)

        try await viewModel.updatePersistentQueueItemDestination(
            itemID,
            destinationURL: URL(fileURLWithPath: "/Volumes/Recovered", isDirectory: true)
        )
        holdingWorker.release()
        while workerFactory.count < 2 {
            await Task.yield()
        }

        XCTAssertTrue(preflight.paths.contains("/Volumes/Recovered"))
        XCTAssertNotEqual(queueStore.items[0].failure?.code, "destination_unavailable")
    }

    private func makeDraft(
        destination: String,
        sourcePath: String = "/Sources/movie.mkv",
        inspected: Bool = false
    ) -> ConversionDraft {
        var options = ConversionOptions()
        options.encoding.mvHEVC.directFinalBitrate = BitratePreference(mode: .custom, customMbps: 40)
        let selectedTitle = SourceTitle(
            id: "title-1",
            name: "Movie",
            outputName: "Movie",
            durationSeconds: 3_600,
            resolution: "1920x1080",
            frameRate: "24/1",
            mainFeature: true
        )
        return ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: sourcePath)),
            sourceDetails: inspected
                ? SourceInspection(
                    name: "Movie",
                    resolution: "1920x1080",
                    frameRate: "24/1",
                    interlaced: false,
                    durationSeconds: 3_600,
                    titles: [selectedTitle]
                )
                : nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: destination, isDirectory: true),
            options: options,
            selectedTitle: selectedTitle
        )
    }
}

private enum StoragePreflightTestError: Error {
    case workerSpawned
}

private final class WorkerFactoryCounter: @unchecked Sendable {
    var count = 0
}

private final class HoldingConversionWorkerClient: WorkerProcessRunning, @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Void, Never>?
    private var released = false

    func run(
        job: WorkerJobSpec,
        onEvent: @escaping (WorkerEvent) async throws -> Void
    ) async throws -> WorkerRunResult {
        let ready = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .workerReady,
            jobID: job.jobID,
            sequence: 0,
            payload: WorkerEventPayload(workerVersion: "test", processGroupID: 1)
        )
        try await onEvent(ready)
        await waitForRelease()
        let completed = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .jobCompleted,
            jobID: job.jobID,
            sequence: 1,
            payload: WorkerEventPayload(
                conversionResult: ConversionResult(outputPath: "/Volumes/Available/Movie_AVP.mov")
            )
        )
        try await onEvent(completed)
        return WorkerRunResult(terminalEvent: completed, exitStatus: 0, diagnostics: "")
    }

    func cancel() {
        release()
    }

    func release() {
        let continuation: CheckedContinuation<Void, Never>?
        lock.lock()
        released = true
        continuation = self.continuation
        self.continuation = nil
        lock.unlock()
        continuation?.resume()
    }

    private func waitForRelease() async {
        await withCheckedContinuation { continuation in
            lock.lock()
            if released {
                lock.unlock()
                continuation.resume()
                return
            }
            self.continuation = continuation
            lock.unlock()
        }
    }
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

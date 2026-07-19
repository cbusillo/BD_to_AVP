import XCTest
@testable import BluRayToVisionPro

final class PreviewViewModelTests: XCTestCase {
    @MainActor
    func testCanonicalObservabilityPersistsDuringPreview() async throws {
        try await withTemporaryPreviewEnvironment { sourceURL, cache in
            let completed = expectation(description: "preview completed")
            let observabilityEvent = try makePreviewObservabilityEvent()
            let worker = PreviewWorkerClient(
                observabilityEvent: observabilityEvent,
                onCompleted: { completed.fulfill() }
            )
            let store = PreviewRecordingObservabilityEventStore()
            let viewModel = PreviewViewModel(
                clientFactory: { worker },
                cache: cache,
                observabilityEventStore: store
            )
            let previewDraft = try XCTUnwrap(makePreviewDraft(sourceURL: sourceURL))

            viewModel.startPreview(previewDraft)
            await fulfillment(of: [completed], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            XCTAssertEqual(store.events, [observabilityEvent])
        }
    }

    @MainActor
    func testCompletedPreviewOwnsArtifactUntilDiscarded() async throws {
        try await withTemporaryPreviewEnvironment { sourceURL, cache in
            let completed = expectation(description: "preview completed")
            let worker = PreviewWorkerClient(onCompleted: { completed.fulfill() })
            let viewModel = PreviewViewModel(clientFactory: { worker }, cache: cache)
            let previewDraft = try XCTUnwrap(makePreviewDraft(sourceURL: sourceURL))

            viewModel.startPreview(previewDraft)

            await fulfillment(of: [completed], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            var playerLease: PreviewArtifactLease? = try XCTUnwrap(viewModel.artifactLease)
            XCTAssertEqual(viewModel.phase, .ready)
            XCTAssertEqual(viewModel.reviewedDraft, previewDraft)
            XCTAssertEqual(viewModel.elapsedSeconds, 65)
            XCTAssertEqual(viewModel.elapsedText, "1:05")
            XCTAssertEqual(worker.receivedJob?.operation, "preview_source")
            XCTAssertEqual(worker.receivedJob?.preview?.parentJobID, previewDraft.parentJobID)
            XCTAssertEqual(
                worker.receivedJob?.encoding,
                WorkerJobSpec(previewDraft: previewDraft, destinationURL: playerLease!.directoryURL).encoding
            )
            XCTAssertTrue(FileManager.default.fileExists(atPath: playerLease!.artifact.outputURL.path))

            let directoryURL = playerLease!.directoryURL
            viewModel.discardPreview()

            XCTAssertEqual(viewModel.phase, .expired)
            XCTAssertTrue(FileManager.default.fileExists(atPath: directoryURL.path))
            playerLease = nil
            XCTAssertFalse(FileManager.default.fileExists(atPath: directoryURL.path))
        }
    }

    @MainActor
    func testCancelledPreviewRemovesPartialWorkspace() async throws {
        try await withTemporaryPreviewEnvironment { sourceURL, cache in
            let started = expectation(description: "preview started")
            let delayedEvents = expectation(description: "delayed cancellation events delivered")
            let cancelled = expectation(description: "preview cancelled")
            let worker = PreviewWorkerClient(
                waitsForCancellation: true,
                onStarted: { started.fulfill() },
                onCancellationEventsDelivered: { delayedEvents.fulfill() },
                onCompleted: { cancelled.fulfill() }
            )
            let viewModel = PreviewViewModel(clientFactory: { worker }, cache: cache)
            let previewDraft = try XCTUnwrap(makePreviewDraft(sourceURL: sourceURL))

            viewModel.startPreview(previewDraft)
            await fulfillment(of: [started], timeout: 2)
            let destinationURL = try XCTUnwrap(worker.receivedJob?.destination.map { URL(fileURLWithPath: $0.path) })
            XCTAssertEqual(viewModel.progress, WorkerProgress(currentStage: 9, totalStages: 13, stageFraction: 0.5))

            viewModel.stopActiveWorker()

            await fulfillment(of: [delayedEvents], timeout: 2)
            XCTAssertEqual(viewModel.phase, .stopping)
            XCTAssertEqual(viewModel.stageMessage, "Stopping Preview")
            XCTAssertNil(viewModel.progress)
            worker.resumeAfterDelayedCancellationEvents()

            await fulfillment(of: [cancelled], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }
            XCTAssertEqual(viewModel.phase, .idle)
            XCTAssertNil(viewModel.progress)
            XCTAssertFalse(FileManager.default.fileExists(atPath: destinationURL.path))
            XCTAssertNil(viewModel.artifactLease)
        }
    }

    @MainActor
    func testPrepareAudioStageUsesEncodingPhaseAndDisplayName() async throws {
        try await withTemporaryPreviewEnvironment { sourceURL, cache in
            let started = expectation(description: "prepare audio stage started")
            let cancelled = expectation(description: "prepare audio preview cancelled")
            let worker = PreviewWorkerClient(
                initialStage: "transcode_audio",
                initialStageMessage: "Prepare Audio",
                waitsForCancellation: true,
                onStarted: { started.fulfill() },
                onCompleted: { cancelled.fulfill() }
            )
            let viewModel = PreviewViewModel(clientFactory: { worker }, cache: cache)
            let previewDraft = try XCTUnwrap(makePreviewDraft(sourceURL: sourceURL))

            viewModel.startPreview(previewDraft)

            await fulfillment(of: [started], timeout: 2)
            XCTAssertEqual(viewModel.phase, .encoding)
            XCTAssertEqual(viewModel.stageMessage, "Prepare Audio")

            viewModel.stopActiveWorker()
            await fulfillment(of: [cancelled], timeout: 2)
        }
    }

    @MainActor
    func testPreviewSnapshotDoesNotChangeWithEditableOptions() throws {
        let sourceURL = URL(fileURLWithPath: "/tmp/movie.mkv")
        var options = ConversionOptions()
        options.encoding.hevcQuality = 82
        let conversion = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: sourceURL),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies", isDirectory: true),
            options: options
        )
        let preview = try XCTUnwrap(
            PreviewDraft(
                conversion: conversion,
                outputLength: .oneMinute,
                samplePosition: .ending
            )
        )

        options.encoding.hevcQuality = 20
        let spec = WorkerJobSpec(
            previewDraft: preview,
            destinationURL: URL(fileURLWithPath: "/tmp/preview", isDirectory: true)
        )

        XCTAssertEqual(spec.encoding?.mvHEVCQuality, 82)
        XCTAssertEqual(spec.preview?.position, "end")
        XCTAssertEqual(spec.preview?.durationSeconds, 60)
        XCTAssertEqual(options.encoding.hevcQuality, 20)
    }

    private func makePreviewDraft(sourceURL: URL) -> PreviewDraft? {
        PreviewDraft(
            parentJobID: UUID(uuidString: "11111111-1111-4111-8111-111111111111")!,
            conversion: ConversionDraft(
                source: ConversionSource(kind: .matroska, url: sourceURL),
                sourceDetails: SourceInspection(
                    name: "movie",
                    resolution: "1920x1080",
                    frameRate: "24/1",
                    interlaced: false,
                    sizeBytes: 10,
                    durationSeconds: 7200
                ),
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/Movies", isDirectory: true),
                options: ConversionOptions()
            ),
            outputLength: .oneMinute,
            samplePosition: .middle
        )
    }

    @MainActor
    private func withTemporaryPreviewEnvironment(
        _ operation: @MainActor (URL, PreviewCache) async throws -> Void
    ) async throws {
        let rootURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: rootURL, withIntermediateDirectories: true)
        let sourceURL = rootURL.appendingPathComponent("movie.mkv")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("source".utf8))
        let cache = PreviewCache(rootURL: rootURL.appendingPathComponent("cache", isDirectory: true))
        defer { try? FileManager.default.removeItem(at: rootURL) }
        try await operation(sourceURL, cache)
    }
}

final class PreviewCacheTests: XCTestCase {
    func testExpiredCacheDirectoriesArePruned() throws {
        let rootURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let cache = PreviewCache(rootURL: rootURL)
        let directoryURL = try cache.prepareDirectory(jobID: UUID())
        let now = Date()
        try FileManager.default.setAttributes(
            [.modificationDate: now.addingTimeInterval(-PreviewCache.expirationInterval - 1)],
            ofItemAtPath: directoryURL.path
        )
        defer { try? FileManager.default.removeItem(at: rootURL) }

        cache.removeExpired(now: now)

        XCTAssertFalse(FileManager.default.fileExists(atPath: directoryURL.path))
    }

    func testSymlinkArtifactCannotEscapeOwnedDirectory() throws {
        let rootURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let outsideURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let cache = PreviewCache(rootURL: rootURL)
        let directoryURL = try cache.prepareDirectory(jobID: UUID())
        try FileManager.default.createDirectory(at: outsideURL, withIntermediateDirectories: true)
        let outsideFileURL = outsideURL.appendingPathComponent("outside.mov")
        _ = FileManager.default.createFile(atPath: outsideFileURL.path, contents: Data("outside".utf8))
        let symlinkURL = directoryURL.appendingPathComponent("preview.mov")
        try FileManager.default.createSymbolicLink(at: symlinkURL, withDestinationURL: outsideFileURL)
        defer {
            try? FileManager.default.removeItem(at: rootURL)
            try? FileManager.default.removeItem(at: outsideURL)
        }

        XCTAssertFalse(cache.contains(symlinkURL, in: directoryURL))
        XCTAssertTrue(FileManager.default.fileExists(atPath: outsideFileURL.path))
    }
}

private final class PreviewWorkerClient: WorkerProcessRunning, @unchecked Sendable {
    private let lock = NSLock()
    private let initialStage: String
    private let initialStageMessage: String
    private let observabilityEvent: ObservabilityEvent?
    private let waitsForCancellation: Bool
    private let onStarted: (() -> Void)?
    private let onCancellationEventsDelivered: (() -> Void)?
    private let onCompleted: (() -> Void)?
    private var cancellationContinuation: CheckedContinuation<Void, Never>?
    private var cancellationRequested = false
    private var delayedEventsContinuation: CheckedContinuation<Void, Never>?
    private var delayedEventsResumeRequested = false

    private(set) var receivedJob: WorkerJobSpec?

    init(
        initialStage: String = "create_left_right_files",
        initialStageMessage: String = "Encoding Preview",
        observabilityEvent: ObservabilityEvent? = nil,
        waitsForCancellation: Bool = false,
        onStarted: (() -> Void)? = nil,
        onCancellationEventsDelivered: (() -> Void)? = nil,
        onCompleted: (() -> Void)? = nil
    ) {
        self.initialStage = initialStage
        self.initialStageMessage = initialStageMessage
        self.observabilityEvent = observabilityEvent
        self.waitsForCancellation = waitsForCancellation
        self.onStarted = onStarted
        self.onCancellationEventsDelivered = onCancellationEventsDelivered
        self.onCompleted = onCompleted
    }

    func run(
        job: WorkerJobSpec,
        onEvent: @escaping (WorkerEvent) async throws -> Void
    ) async throws -> WorkerRunResult {
        receivedJob = job
        let ready = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .workerReady,
            jobID: job.jobID,
            sequence: 0,
            payload: WorkerEventPayload(workerVersion: "test", processGroupID: 1)
        )
        try await onEvent(ready)

        let stage = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .stageStarted,
            jobID: job.jobID,
            sequence: 1,
            payload: WorkerEventPayload(
                stage: initialStage,
                message: initialStageMessage,
                progress: WorkerProgress(currentStage: 9, totalStages: 13, stageFraction: nil)
            )
        )
        try await onEvent(stage)

        let heartbeat = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .heartbeat,
            jobID: job.jobID,
            sequence: 2,
            payload: WorkerEventPayload(
                message: "Encoding both eyes",
                elapsedSeconds: 65,
                progress: WorkerProgress(currentStage: 9, totalStages: 13, stageFraction: 0.5)
            )
        )
        try await onEvent(heartbeat)
        let observabilityOffset: Int
        if let observabilityEvent {
            try await onEvent(
                WorkerEvent(
                    protocolVersion: WorkerJobSpec.protocolVersion,
                    type: .observability,
                    jobID: job.jobID,
                    sequence: 3,
                    payload: WorkerEventPayload(observabilityEvent: observabilityEvent)
                )
            )
            observabilityOffset = 1
        } else {
            observabilityOffset = 0
        }
        onStarted?()

        let destinationURL = URL(fileURLWithPath: job.destination!.path, isDirectory: true)
        let partialURL = destinationURL.appendingPathComponent("partial.mov")
        try FileManager.default.createDirectory(at: destinationURL, withIntermediateDirectories: true)
        _ = FileManager.default.createFile(atPath: partialURL.path, contents: Data("partial".utf8))

        if waitsForCancellation {
            await waitForCancellation()
            let delayedStage = WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .stageStarted,
                jobID: job.jobID,
                sequence: 3 + observabilityOffset,
                payload: WorkerEventPayload(
                    stage: "combine_to_mv_hevc",
                    message: "Combining stereo video into MV-HEVC",
                    progress: WorkerProgress(currentStage: 10, totalStages: 13, stageFraction: nil)
                )
            )
            try await onEvent(delayedStage)
            let delayedHeartbeat = WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .heartbeat,
                jobID: job.jobID,
                sequence: 4 + observabilityOffset,
                payload: WorkerEventPayload(
                    elapsedSeconds: 66,
                    progress: WorkerProgress(currentStage: 10, totalStages: 13, stageFraction: 0.2)
                )
            )
            try await onEvent(delayedHeartbeat)
            if onCancellationEventsDelivered != nil {
                onCancellationEventsDelivered?()
                await waitForDelayedCancellationEventsResume()
            }
            let cancelled = WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .jobCancelled,
                jobID: job.jobID,
                sequence: 5 + observabilityOffset,
                payload: WorkerEventPayload(message: "Preview stopped.")
            )
            try await onEvent(cancelled)
            onCompleted?()
            return WorkerRunResult(terminalEvent: cancelled, exitStatus: SIGTERM, diagnostics: "")
        }

        let outputURL = destinationURL.appendingPathComponent("movie_AVP.mov")
        _ = FileManager.default.createFile(atPath: outputURL.path, contents: Data("preview".utf8))
        let artifact = PreviewArtifact(
            sourcePath: job.source.path,
            destinationPath: destinationURL.path,
            outputPath: outputURL.path,
            sizeBytes: 7,
            parentJobID: job.preview!.parentJobID,
            position: job.preview!.position,
            startSeconds: 3570,
            durationSeconds: 60,
            sourceDurationSeconds: 7200
        )
        let artifactReady = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .artifactReady,
            jobID: job.jobID,
            sequence: 3 + observabilityOffset,
            payload: WorkerEventPayload(artifact: artifact)
        )
        try await onEvent(artifactReady)

        let completed = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .jobCompleted,
            jobID: job.jobID,
            sequence: 4 + observabilityOffset,
            payload: WorkerEventPayload(previewResult: artifact)
        )
        try await onEvent(completed)
        onCompleted?()
        return WorkerRunResult(terminalEvent: completed, exitStatus: 0, diagnostics: "")
    }

    func cancel() {
        let continuation: CheckedContinuation<Void, Never>?
        lock.lock()
        cancellationRequested = true
        continuation = cancellationContinuation
        cancellationContinuation = nil
        lock.unlock()
        continuation?.resume()
    }

    func resumeAfterDelayedCancellationEvents() {
        let continuation: CheckedContinuation<Void, Never>?
        lock.lock()
        continuation = delayedEventsContinuation
        delayedEventsContinuation = nil
        if continuation == nil {
            delayedEventsResumeRequested = true
        }
        lock.unlock()
        continuation?.resume()
    }

    private func waitForCancellation() async {
        await withCheckedContinuation { continuation in
            lock.lock()
            if cancellationRequested {
                lock.unlock()
                continuation.resume()
                return
            }
            cancellationContinuation = continuation
            lock.unlock()
        }
    }

    private func waitForDelayedCancellationEventsResume() async {
        await withCheckedContinuation { continuation in
            lock.lock()
            if delayedEventsResumeRequested {
                delayedEventsResumeRequested = false
                lock.unlock()
                continuation.resume()
            } else {
                delayedEventsContinuation = continuation
                lock.unlock()
            }
        }
    }
}

private final class PreviewRecordingObservabilityEventStore: ObservabilityEventPersisting, @unchecked Sendable {
    private let lock = NSLock()
    private var recordedEvents: [ObservabilityEvent] = []

    var events: [ObservabilityEvent] {
        lock.lock()
        defer { lock.unlock() }
        return recordedEvents
    }

    func append(_ event: ObservabilityEvent) {
        lock.lock()
        recordedEvents.append(event)
        lock.unlock()
    }

    func snapshot() -> ObservabilityEventPersistenceSnapshot {
        .disabled
    }
}

private func makePreviewObservabilityEvent() throws -> ObservabilityEvent {
    let fixtureURL = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .appendingPathComponent("tests/fixtures/observability_event_v1.json")
    let fixtureData = try XCTUnwrap(FileManager.default.contents(atPath: fixtureURL.path))
    return try JSONDecoder().decode(ObservabilityEvent.self, from: fixtureData)
}

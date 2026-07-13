import Darwin
import Foundation
import XCTest
@testable import BluRayToVisionPro

final class ConversionViewModelTests: XCTestCase {
    @MainActor
    func testTerminalCompletionWinsIfStopIsRequestedBeforeProcessExit() async throws {
        let terminalDelivered = expectation(description: "terminal delivered")
        let worker = ControlledWorkerClient(terminalDelivered: terminalDelivered)
        let viewModel = ConversionViewModel { worker }

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)

            await fulfillment(of: [terminalDelivered], timeout: 2)
            XCTAssertTrue(viewModel.hasActiveWorker)
            XCTAssertEqual(viewModel.state.phase, .inspecting)

            await viewModel.stopForQuit()

            XCTAssertFalse(viewModel.hasActiveWorker)
            XCTAssertEqual(viewModel.state.phase, .completed)
            XCTAssertEqual(viewModel.state.result?.name, "movie")
        }
    }

    @MainActor
    func testUnsupportedSourceCannotBeRetried() {
        let viewModel = ConversionViewModel()
        let sourceURL = URL(fileURLWithPath: "/tmp/movie.mp4")

        viewModel.selectSource(sourceURL)

        XCTAssertEqual(viewModel.state.phase, .failed)
        XCTAssertFalse(viewModel.state.failureRetryable)
        XCTAssertFalse(viewModel.canRetry)

        viewModel.restartInspection()
        XCTAssertEqual(viewModel.state.phase, .failed)
    }

    @MainActor
    func testUnsupportedSelectionClearsPreviouslyInspectedSource() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let validSourceURL = directoryURL.appendingPathComponent("Feature.mkv")
        let unsupportedSourceURL = directoryURL.appendingPathComponent("Feature.img")
        _ = FileManager.default.createFile(atPath: validSourceURL.path, contents: Data("video".utf8))
        _ = FileManager.default.createFile(atPath: unsupportedSourceURL.path, contents: Data("disc".utf8))
        defer { try? FileManager.default.removeItem(at: directoryURL) }

        let inspectionDone = expectation(description: "inspection done")
        let viewModel = ConversionViewModel {
            TwoPhaseWorkerClient(onInspectionComplete: { inspectionDone.fulfill() })
        }

        viewModel.selectSource(validSourceURL)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        XCTAssertNotNil(viewModel.state.result)

        viewModel.selectSource(unsupportedSourceURL)

        XCTAssertNil(viewModel.source)
        XCTAssertNil(viewModel.state.result)
        XCTAssertEqual(viewModel.state.phase, .failed)
        XCTAssertEqual(
            viewModel.state.failureMessage,
            "Choose a 3D Blu-ray disc, ISO, Blu-ray folder, MKV, MTS, or M2TS source."
        )
    }

    @MainActor
    func testDiscImageSelectionStartsInspection() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let imageURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: imageURL.path, contents: Data("disc".utf8))
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let inspectionDone = expectation(description: "inspection done")
        let viewModel = ConversionViewModel {
            TwoPhaseWorkerClient(onInspectionComplete: { inspectionDone.fulfill() })
        }

        viewModel.selectSource(ConversionSource(kind: .discImage, url: imageURL))
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }

        XCTAssertEqual(viewModel.source?.kind, .discImage)
        XCTAssertEqual(viewModel.state.phase, .completed)
        XCTAssertNotNil(viewModel.state.result)
        XCTAssertFalse(viewModel.hasActiveWorker)
    }

    @MainActor
    func testStartConversionStartsWorkerForInspectedISO() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let conversionStarted = expectation(description: "conversion started")
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { spec in
                XCTAssertEqual(URL(fileURLWithPath: spec.source.path).pathExtension, "iso")
                XCTAssertEqual(spec.operation, "convert_source")
                conversionStarted.fulfill()
            }
        )
        let viewModel = ConversionViewModel { worker }
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let imageURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: imageURL.path, contents: Data("disc".utf8))
        defer { try? FileManager.default.removeItem(at: directoryURL) }

        viewModel.selectSource(ConversionSource(kind: .discImage, url: imageURL))
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }

        let draft = ConversionDraft(
            source: viewModel.source!,
            sourceDetails: viewModel.state.result,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies"),
            outputLength: .fullMovie,
            samplePosition: .beginning,
            options: ConversionOptions()
        )
        viewModel.startConversion(draft: draft)

        await fulfillment(of: [conversionStarted], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        XCTAssertEqual(viewModel.state.phase, .completed)
    }

    @MainActor
    func testStartConversionForUnsupportedSourceKindDoesNotStartWorker() {
        let viewModel = ConversionViewModel()
        let discSource = ConversionSource(kind: .physicalDisc, url: URL(fileURLWithPath: "/Volumes/Disc"))
        viewModel.selectSource(discSource)

        let draft = ConversionDraft(
            source: discSource,
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies"),
            outputLength: .fullMovie,
            samplePosition: .beginning,
            options: ConversionOptions()
        )
        viewModel.startConversion(draft: draft)

        XCTAssertFalse(viewModel.hasActiveWorker)
    }

    @MainActor
    func testStartConversionStartsWorkerWithConvertSourceJobSpec() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let conversionStarted = expectation(description: "conversion started")
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { spec in
                XCTAssertEqual(spec.operation, "convert_source")
                XCTAssertNotNil(spec.destination)
                XCTAssertNotNil(spec.encoding)
                XCTAssertEqual(spec.job?.outputLength, "full_movie")
                conversionStarted.fulfill()
            }
        )
        let viewModel = ConversionViewModel { worker }

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)
            await fulfillment(of: [inspectionDone], timeout: 2)
            // Drain the inspection Task so finish() runs and hasActiveWorker becomes false
            while viewModel.hasActiveWorker { await Task.yield() }

            let draft = ConversionDraft(
                source: viewModel.source!,
                sourceDetails: viewModel.state.result,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/Movies"),
                outputLength: .fullMovie,
                samplePosition: .beginning,
                options: ConversionOptions()
            )
            viewModel.startConversion(draft: draft)

            await fulfillment(of: [conversionStarted], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }
            XCTAssertEqual(viewModel.state.operationKind, .conversion)
            XCTAssertEqual(viewModel.state.phase, .completed)
            XCTAssertEqual(viewModel.state.conversionResult?.outputPath, "/Movies/movie_AVP.mov")
        }
    }

    @MainActor
    func testStopActiveWorkerCancelsConversionAndTransitionsToStopping() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let conversionStarted = expectation(description: "conversion started")
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { _ in conversionStarted.fulfill() },
            waitsForConversionCancellation: true
        )
        let viewModel = ConversionViewModel { worker }

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)
            await fulfillment(of: [inspectionDone], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            let draft = ConversionDraft(
                source: viewModel.source!,
                sourceDetails: viewModel.state.result,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/Movies"),
                outputLength: .fullMovie,
                samplePosition: .beginning,
                options: ConversionOptions()
            )
            viewModel.startConversion(draft: draft)
            await fulfillment(of: [conversionStarted], timeout: 2)

            viewModel.stopActiveWorker()

            XCTAssertEqual(viewModel.state.phase, .stopping)
        }
    }

    @MainActor
    func testStartConversionRejectsDraftForDifferentSource() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let viewModel = ConversionViewModel {
            TwoPhaseWorkerClient(onInspectionComplete: { inspectionDone.fulfill() })
        }

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)
            await fulfillment(of: [inspectionDone], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            let otherURL = sourceURL.deletingLastPathComponent().appendingPathComponent("other.m2ts")
            _ = FileManager.default.createFile(atPath: otherURL.path, contents: Data("video".utf8))
            let draft = ConversionDraft(
                source: ConversionSource(kind: .transportStream, url: otherURL),
                sourceDetails: viewModel.state.result,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/Movies"),
                outputLength: .fullMovie,
                samplePosition: .beginning,
                options: ConversionOptions()
            )

            viewModel.startConversion(draft: draft)

            XCTAssertFalse(viewModel.hasActiveWorker)
            XCTAssertEqual(viewModel.state.phase, .failed)
            XCTAssertEqual(viewModel.state.failureMessage, "Analyze the selected source before starting conversion.")
        }
    }

    @MainActor
    func testStartConversionRejectsSampleOutput() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let viewModel = ConversionViewModel {
            TwoPhaseWorkerClient(onInspectionComplete: { inspectionDone.fulfill() })
        }

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)
            await fulfillment(of: [inspectionDone], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            let draft = ConversionDraft(
                source: viewModel.source!,
                sourceDetails: viewModel.state.result,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/Movies"),
                outputLength: .oneMinute,
                samplePosition: .beginning,
                options: ConversionOptions()
            )

            viewModel.startConversion(draft: draft)

            XCTAssertFalse(viewModel.hasActiveWorker)
            XCTAssertEqual(viewModel.state.phase, .failed)
            XCTAssertEqual(
                viewModel.state.failureMessage,
                "Short sample conversion is not available yet. Choose Full Movie."
            )
        }
    }

    private func withTemporarySource(_ operation: @MainActor (URL) async throws -> Void) async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let sourceURL = directoryURL.appendingPathComponent("movie.m2ts")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8))
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try await operation(sourceURL)
    }
}

private final class TwoPhaseWorkerClient: WorkerProcessRunning, @unchecked Sendable {
    private let lock = NSLock()
    private var callCount = 0
    private var conversionCancellationContinuation: CheckedContinuation<Void, Never>?
    private var conversionCancellationRequested = false

    var onInspectionComplete: (() -> Void)?
    var onConversionJobReceived: ((WorkerJobSpec) -> Void)?
    private let waitsForConversionCancellation: Bool

    init(
        onInspectionComplete: (() -> Void)? = nil,
        onConversionJobReceived: ((WorkerJobSpec) -> Void)? = nil,
        waitsForConversionCancellation: Bool = false
    ) {
        self.onInspectionComplete = onInspectionComplete
        self.onConversionJobReceived = onConversionJobReceived
        self.waitsForConversionCancellation = waitsForConversionCancellation
    }

    func run(job: WorkerJobSpec, onEvent: @escaping (WorkerEvent) async throws -> Void) async throws -> WorkerRunResult {
        let isConversion: Bool
        lock.lock()
        callCount += 1
        isConversion = callCount > 1
        lock.unlock()

        let ready = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .workerReady,
            jobID: job.jobID,
            sequence: 0,
            payload: WorkerEventPayload(workerVersion: "test", processGroupID: 1)
        )

        if isConversion {
            onConversionJobReceived?(job)
            try await onEvent(ready)
            if waitsForConversionCancellation {
                await waitForConversionCancellation()
                let cancelled = WorkerEvent(
                    protocolVersion: WorkerJobSpec.protocolVersion,
                    type: .jobCancelled,
                    jobID: job.jobID,
                    sequence: 1,
                    payload: WorkerEventPayload(message: "Conversion stopped.")
                )
                try await onEvent(cancelled)
                return WorkerRunResult(terminalEvent: cancelled, exitStatus: SIGTERM, diagnostics: "")
            }

            let completed = WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .jobCompleted,
                jobID: job.jobID,
                sequence: 1,
                payload: WorkerEventPayload(
                    conversionResult: ConversionResult(outputPath: "/Movies/movie_AVP.mov")
                )
            )
            try await onEvent(completed)
            return WorkerRunResult(terminalEvent: completed, exitStatus: 0, diagnostics: "")
        } else {
            let result = SourceInspection(name: "movie", resolution: "1920x1080", frameRate: "24/1", interlaced: false, sizeBytes: 10)
            let completed = WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .jobCompleted,
                jobID: job.jobID,
                sequence: 1,
                payload: WorkerEventPayload(result: result)
            )
            try await onEvent(ready)
            try await onEvent(completed)
            onInspectionComplete?()
            return WorkerRunResult(terminalEvent: completed, exitStatus: 0, diagnostics: "")
        }
    }

    func cancel() {
        let continuation: CheckedContinuation<Void, Never>?
        lock.lock()
        conversionCancellationRequested = true
        continuation = conversionCancellationContinuation
        conversionCancellationContinuation = nil
        lock.unlock()
        continuation?.resume()
    }

    private func waitForConversionCancellation() async {
        await withCheckedContinuation { continuation in
            lock.lock()
            if conversionCancellationRequested {
                lock.unlock()
                continuation.resume()
                return
            }
            conversionCancellationContinuation = continuation
            lock.unlock()
        }
    }
}

private final class ControlledWorkerClient: WorkerProcessRunning, @unchecked Sendable {
    private let terminalDelivered: XCTestExpectation
    private let lock = NSLock()
    private var cancellationContinuation: CheckedContinuation<Void, Never>?
    private var cancellationRequested = false

    init(terminalDelivered: XCTestExpectation) {
        self.terminalDelivered = terminalDelivered
    }

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
        let completed = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .jobCompleted,
            jobID: job.jobID,
            sequence: 1,
            payload: WorkerEventPayload(
                result: SourceInspection(
                    name: "movie",
                    resolution: "1920x1080",
                    frameRate: "24/1",
                    interlaced: false,
                    sizeBytes: 10
                )
            )
        )

        try await onEvent(ready)
        try await onEvent(completed)
        terminalDelivered.fulfill()
        await waitForCancellation()
        return WorkerRunResult(terminalEvent: completed, exitStatus: SIGTERM, diagnostics: "")
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
}

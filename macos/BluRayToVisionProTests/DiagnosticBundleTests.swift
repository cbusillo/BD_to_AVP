import Foundation
import XCTest
@testable import BluRayToVisionPro

final class DiagnosticBundleTests: XCTestCase {
    private let fixedDate = Date(timeIntervalSince1970: 1_784_323_200)
    private let fixedBundleID = UUID(uuidString: "01234567-89AB-4CDE-8F01-23456789ABCD")!

    func testBoundedTextBufferRetainsTailAndReportsDroppedBytes() {
        let buffer = BoundedDiagnosticTextBuffer(maximumBytes: 16)

        buffer.append(Data("0123456789abcdefghijklmnop".utf8))

        let snapshot = buffer.snapshot()
        XCTAssertEqual(snapshot.text, "abcdefghijklmnop")
        XCTAssertEqual(snapshot.retainedBytes, 16)
        XCTAssertEqual(snapshot.totalBytes, 26)
        XCTAssertEqual(snapshot.droppedBytes, 10)
        XCTAssertTrue(snapshot.truncated)
    }

    func testRedactorCorrelatesPathsAndRemovesCommandsSecretsAndIdentifiers() {
        let redactor = DiagnosticRedactor(bundleID: fixedBundleID)
        let privatePath = "/Users/alice/Movies/Secret Feature.mkv"
        let token = redactor.pathToken(for: privatePath)
        redactor.registerSensitiveName("Secret Feature")
        let text = """
        File "\(privatePath)", line 42, in convert
        command: /usr/bin/ffmpeg -i "\(privatePath)" -metadata title="Secret Feature"
        Authorization: Bearer abc.def.ghi
        serial_number=ABC123456789
        job 97456C4A-F3C5-44E4-A548-0BD833EAD4BB failed for Secret Feature.mkv
        """

        let output = redactor.redact(text)

        XCTAssertFalse(output.contains("/Users/alice"))
        XCTAssertFalse(output.contains("Secret Feature"))
        XCTAssertFalse(output.contains("abc.def.ghi"))
        XCTAssertFalse(output.contains("ABC123456789"))
        XCTAssertFalse(output.contains("97456C4A-F3C5-44E4-A548-0BD833EAD4BB"))
        XCTAssertTrue(output.contains(token))
        XCTAssertTrue(output.contains("ffmpeg <arguments:redacted>"))
        XCTAssertTrue(output.lowercased().contains("authorization=<redacted>"))
        XCTAssertTrue(output.contains("serial_number=<redacted>"))
        XCTAssertTrue(output.contains("<identifier:01234567:001>"))
    }

    func testStorageProbeClassifiesInaccessibleMetadataWithoutLeakingPath() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let fileURL = directory.appendingPathComponent("Private Movie.mkv")
        XCTAssertTrue(FileManager.default.createFile(atPath: fileURL.path, contents: Data()))
        defer { try? FileManager.default.removeItem(at: directory) }
        let probe = FileSystemDiagnosticStorageProbe { _, _ in
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(EACCES))
        }

        let result = probe.probe(role: .source, url: fileURL, capturedAt: fixedDate)

        XCTAssertEqual(result.status, .inaccessible)
        XCTAssertEqual(result.errorKind, .permissionDenied)
        XCTAssertNil(result.fileSizeBytes)
    }

    func testNoJobBundleIsVersionedAndOmitsJobEvidence() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let recorder = DiagnosticSessionRecorder()
        let snapshot = recorder.snapshot(
            capturedAt: fixedDate,
            lifecycle: WorkerLifecycleState(),
            activeMode: nil,
            batchSummary: nil,
            process: .empty
        )
        let builder = DiagnosticBundleBuilder(
            bundleIDProvider: { self.fixedBundleID },
            runtimeMetadataProvider: {
                DiagnosticRuntimeMetadata(
                    appVersion: "0.3.0",
                    appBuild: "200",
                    distributionChannel: "direct",
                    operatingSystemVersion: "27.0.0",
                    architecture: "arm64"
                )
            }
        )

        let artifact = try builder.createBundle(from: snapshot, outputDirectory: directory)
        let manifestData = try unzipEntry("manifest.json", from: artifact.archiveURL)
        let eventsData = try unzipEntry("events.jsonl", from: artifact.archiveURL)
        let manifest = try XCTUnwrap(JSONSerialization.jsonObject(with: manifestData) as? [String: Any])

        XCTAssertEqual(manifest["schema_version"] as? Int, 1)
        XCTAssertNil(manifest["job"])
        XCTAssertTrue(eventsData.isEmpty)
        XCTAssertLessThanOrEqual(artifact.preview.archiveBytes, artifact.preview.maximumArchiveBytes)
    }

    func testRecorderBoundsHistoryAndRetainsTerminalRetryAndCancellationEvidence() throws {
        let sourceURL = URL(fileURLWithPath: "/tmp/Private Movie.mkv")
        let source = ConversionSource(kind: .matroska, url: sourceURL)
        let recorder = DiagnosticSessionRecorder(
            maximumEvents: 4,
            maximumEventBytes: 64 * 1_024,
            maximumStorageSamples: 2,
            storageSampleInterval: 0
        )
        var lifecycle = WorkerLifecycleState()
        lifecycle.selectSource(sourceURL)
        let jobID = UUID()
        try lifecycle.begin(jobID: jobID, operationKind: .conversion)
        recorder.beginJob(
            context: DiagnosticJobContext(jobID: jobID, source: source),
            lifecycle: lifecycle,
            activeMode: "single_conversion",
            recordedAt: fixedDate
        )
        recorder.recordWorkflow(
            name: "batch.started",
            lifecycle: lifecycle,
            activeMode: "batch_conversion",
            recordedAt: fixedDate.addingTimeInterval(1)
        )
        recorder.record(
            event: WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .jobFailed,
                jobID: jobID,
                sequence: 2,
                payload: WorkerEventPayload(
                    error: WorkerFailure(
                        code: "tool_failed",
                        message: "Tool failed",
                        details: "/Users/alice/private.mkv",
                        retryable: true
                    )
                )
            ),
            lifecycle: lifecycle,
            activeMode: "batch_conversion",
            recordedAt: fixedDate.addingTimeInterval(2)
        )
        lifecycle.failTransport(message: "Retry requested")
        recorder.recordWorkflow(
            name: "retry.requested",
            lifecycle: lifecycle,
            activeMode: nil,
            recordedAt: fixedDate.addingTimeInterval(3)
        )
        recorder.recordWorkflow(
            name: "cancel.requested",
            lifecycle: lifecycle,
            activeMode: nil,
            recordedAt: fixedDate.addingTimeInterval(4)
        )
        recorder.record(
            event: WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .jobCancelled,
                jobID: jobID,
                sequence: 3,
                payload: WorkerEventPayload(message: "Cancelled")
            ),
            lifecycle: lifecycle,
            activeMode: nil,
            recordedAt: fixedDate.addingTimeInterval(5)
        )

        let snapshot = recorder.snapshot(
            capturedAt: fixedDate.addingTimeInterval(6),
            lifecycle: lifecycle,
            activeMode: nil,
            batchSummary: DiagnosticBatchSummary(
                kind: "source_folder",
                totalItems: 3,
                activeItems: 0,
                statusCounts: ["completed": 1, "failed": 1, "stopped": 1]
            ),
            process: .empty
        )

        XCTAssertEqual(snapshot.events.entries.count, 4)
        XCTAssertEqual(snapshot.events.droppedEntries, 2)
        XCTAssertEqual(
            snapshot.events.entries.map(\.name),
            ["job.failed", "retry.requested", "cancel.requested", "job.cancelled"]
        )
        XCTAssertEqual(snapshot.batchSummary?.statusCounts["failed"], 1)
    }

    func testRecorderCapturesCompletedTerminalLifecycle() throws {
        let sourceURL = URL(fileURLWithPath: "/tmp/Completed Movie.mkv")
        let source = ConversionSource(kind: .matroska, url: sourceURL)
        let jobID = UUID()
        var lifecycle = WorkerLifecycleState()
        lifecycle.selectSource(sourceURL)
        try lifecycle.begin(jobID: jobID)
        let recorder = DiagnosticSessionRecorder()
        recorder.beginJob(
            context: DiagnosticJobContext(jobID: jobID, source: source),
            lifecycle: lifecycle,
            activeMode: "single_inspection",
            recordedAt: fixedDate
        )
        let ready = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .workerReady,
            jobID: jobID,
            sequence: 0,
            payload: WorkerEventPayload(workerVersion: "0.3.0", processGroupID: 4242)
        )
        try lifecycle.receive(ready)
        recorder.record(
            event: ready,
            lifecycle: lifecycle,
            activeMode: "single_inspection",
            recordedAt: fixedDate.addingTimeInterval(1)
        )
        let completed = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .jobCompleted,
            jobID: jobID,
            sequence: 1,
            payload: WorkerEventPayload(
                result: SourceInspection(
                    name: "Completed Movie.mkv",
                    resolution: "1920x1080",
                    frameRate: "24/1",
                    interlaced: false,
                    sizeBytes: 10
                )
            )
        )
        try lifecycle.receive(completed)
        recorder.record(
            event: completed,
            lifecycle: lifecycle,
            activeMode: "single_inspection",
            recordedAt: fixedDate.addingTimeInterval(2)
        )

        let snapshot = recorder.snapshot(
            capturedAt: fixedDate.addingTimeInterval(3),
            lifecycle: lifecycle,
            activeMode: nil,
            batchSummary: nil,
            process: .empty
        )

        XCTAssertEqual(snapshot.lifecycle.phase, .completed)
        XCTAssertEqual(snapshot.workerVersion, "0.3.0")
        XCTAssertEqual(snapshot.events.entries.last?.name, "job.completed")
        XCTAssertEqual(snapshot.events.entries.last?.resultSizeBytes, 10)
    }

    func testBundleIsRedactedBoundedTruncatedAndSaveShareReady() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let sourceURL = directory.appendingPathComponent("Secret Feature.mkv")
        let destinationURL = directory.appendingPathComponent("Private Output", isDirectory: true)
        try FileManager.default.createDirectory(at: destinationURL, withIntermediateDirectories: true)
        XCTAssertTrue(FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8)))
        defer { try? FileManager.default.removeItem(at: directory) }

        let source = ConversionSource(kind: .matroska, url: sourceURL)
        let inspection = SourceInspection(
            name: "Secret Feature.mkv",
            resolution: "1920x1080",
            frameRate: "24/1",
            interlaced: false,
            sizeBytes: 5
        )
        let draft = ConversionDraft(
            source: source,
            sourceDetails: inspection,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: destinationURL,
            options: ConversionOptions()
        )
        let jobID = UUID(uuidString: "97456C4A-F3C5-44E4-A548-0BD833EAD4BB")!
        var lifecycle = WorkerLifecycleState()
        lifecycle.selectSource(sourceURL)
        try lifecycle.begin(jobID: jobID, operationKind: .conversion)
        let recorder = DiagnosticSessionRecorder(
            maximumEvents: 3,
            maximumEventBytes: 64 * 1_024,
            maximumStorageSamples: 2,
            storageSampleInterval: 0
        )
        recorder.beginJob(
            context: DiagnosticJobContext(jobID: jobID, draft: draft),
            lifecycle: lifecycle,
            activeMode: "batch_conversion",
            recordedAt: fixedDate
        )
        for sequence in 0..<6 {
            recorder.record(
                event: WorkerEvent(
                    protocolVersion: WorkerJobSpec.protocolVersion,
                    type: .heartbeat,
                    jobID: jobID,
                    sequence: sequence,
                    payload: WorkerEventPayload(
                        stage: "combine_to_mv_hevc",
                        message: "Working on \(sourceURL.path)",
                        elapsedSeconds: sequence,
                        progress: WorkerProgress(currentStage: 5, totalStages: 9, stageFraction: 0.5)
                    )
                ),
                lifecycle: lifecycle,
                activeMode: "batch_conversion",
                recordedAt: fixedDate.addingTimeInterval(TimeInterval(sequence))
            )
        }
        let storageProbe = FileSystemDiagnosticStorageProbe()
        recorder.sampleCurrentStorage(using: storageProbe, recordedAt: fixedDate, force: true)
        recorder.sampleCurrentStorage(
            using: storageProbe,
            recordedAt: fixedDate.addingTimeInterval(10),
            force: true
        )
        recorder.sampleCurrentStorage(
            using: storageProbe,
            recordedAt: fixedDate.addingTimeInterval(20),
            force: true
        )

        let tailBuffer = BoundedDiagnosticTextBuffer(maximumBytes: 1_024)
        tailBuffer.append(Data(String(repeating: "old diagnostic line\n", count: 100).utf8))
        tailBuffer.append(
            Data(
                """
                File "\(sourceURL.path)", line 12
                command: /usr/bin/ffmpeg -i "\(sourceURL.path)" -metadata title="Secret Feature"
                token=ghp_abcdefghijklmnopqrstuvwxyz123456
                """.utf8
            )
        )
        let process = WorkerProcessDiagnosticSnapshot(
            isRunning: true,
            processIdentifier: 4242,
            processGroupIdentifier: 4242,
            cancellationRequested: false,
            toolOutput: tailBuffer.snapshot()
        )
        let snapshot = recorder.snapshot(
            capturedAt: fixedDate.addingTimeInterval(30),
            lifecycle: lifecycle,
            activeMode: "batch_conversion",
            batchSummary: DiagnosticBatchSummary(
                kind: "source_folder",
                totalItems: 4,
                activeItems: 1,
                statusCounts: ["completed": 1, "converting": 1, "pending": 2]
            ),
            process: process
        )
        let builder = DiagnosticBundleBuilder(
            storageProbe: storageProbe,
            bundleIDProvider: { self.fixedBundleID },
            runtimeMetadataProvider: {
                DiagnosticRuntimeMetadata(
                    appVersion: "0.3.0",
                    appBuild: "200",
                    distributionChannel: "direct",
                    operatingSystemVersion: "27.0.0",
                    architecture: "arm64"
                )
            }
        )

        let artifact = try builder.createBundle(from: snapshot, outputDirectory: directory)
        let manifestData = try unzipEntry("manifest.json", from: artifact.archiveURL)
        let eventsData = try unzipEntry("events.jsonl", from: artifact.archiveURL)
        let storageData = try unzipEntry("storage.json", from: artifact.archiveURL)
        let toolTailData = try unzipEntry("tool-tail.txt", from: artifact.archiveURL)
        let manifest = try XCTUnwrap(
            JSONSerialization.jsonObject(with: manifestData) as? [String: Any]
        )
        let storage = try XCTUnwrap(
            JSONSerialization.jsonObject(with: storageData) as? [String: Any]
        )
        let manifestText = String(decoding: manifestData, as: UTF8.self)
        let combinedText = [manifestData, eventsData, storageData, toolTailData]
            .map { String(decoding: $0, as: UTF8.self) }
            .joined(separator: "\n")

        XCTAssertEqual(manifest["schema_version"] as? Int, 1)
        XCTAssertEqual((manifest["archive"] as? [String: Any])?["maximum_compressed_bytes"] as? Int, 2 * 1_024 * 1_024)
        XCTAssertLessThanOrEqual(artifact.preview.archiveBytes, artifact.preview.maximumArchiveBytes)
        XCTAssertEqual(artifact.sharingItems, [artifact.archiveURL])
        XCTAssertTrue(artifact.preview.truncationNotices.contains("Older diagnostic events were omitted."))
        XCTAssertTrue(artifact.preview.truncationNotices.contains("Older storage samples were omitted."))
        XCTAssertTrue(artifact.preview.truncationNotices.contains("Older tool output was omitted."))
        XCTAssertFalse(combinedText.contains(sourceURL.path))
        XCTAssertFalse(combinedText.contains("Secret Feature"))
        XCTAssertFalse(combinedText.contains("ghp_abcdefghijklmnopqrstuvwxyz123456"))
        XCTAssertTrue(combinedText.contains("<path:01234567:"))
        XCTAssertTrue(combinedText.contains("ffmpeg <arguments:redacted>"))
        XCTAssertTrue(manifestText.contains("\"active\" : true"))
        XCTAssertTrue(String(decoding: toolTailData, as: UTF8.self).contains("# truncated=true"))
        let sourcePathToken = try XCTUnwrap(
            (manifest["job"] as? [String: Any])?["source_path_token"] as? String
        )
        let probes = try XCTUnwrap(storage["probes"] as? [[String: Any]])
        let sourceProbe = try XCTUnwrap(probes.first { $0["role"] as? String == "source" })
        XCTAssertEqual(sourceProbe["path_token"] as? String, sourcePathToken)

        let savedDirectory = directory.appendingPathComponent("Saved", isDirectory: true)
        try FileManager.default.createDirectory(at: savedDirectory, withIntermediateDirectories: true)
        let savedURL = try artifact.saveCopy(to: savedDirectory)
        XCTAssertEqual(savedURL.lastPathComponent, artifact.suggestedFilename)
        XCTAssertTrue(FileManager.default.fileExists(atPath: savedURL.path))
    }

    @MainActor
    func testActiveViewModelCaptureDoesNotCancelOrMutateWorker() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let sourceURL = directory.appendingPathComponent("Private Active Movie.mkv")
        XCTAssertTrue(FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8)))
        defer { try? FileManager.default.removeItem(at: directory) }
        let active = expectation(description: "worker active")
        let worker = HoldingDiagnosticWorkerClient(active: active, sensitivePath: sourceURL.path)
        let storageProbe = FileSystemDiagnosticStorageProbe()
        let builder = DiagnosticBundleBuilder(
            storageProbe: storageProbe,
            bundleIDProvider: { self.fixedBundleID },
            runtimeMetadataProvider: {
                DiagnosticRuntimeMetadata(
                    appVersion: "0.3.0",
                    appBuild: "200",
                    distributionChannel: "direct",
                    operatingSystemVersion: "27.0.0",
                    architecture: "arm64"
                )
            }
        )
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            diagnosticClock: { self.fixedDate },
            diagnosticStorageProbe: storageProbe,
            diagnosticBundleBuilder: builder
        )

        viewModel.selectSource(sourceURL)
        await fulfillment(of: [active], timeout: 2)
        let phaseBeforeCapture = viewModel.state.phase

        let artifact = try viewModel.captureDiagnosticBundle(in: directory)

        XCTAssertEqual(viewModel.state.phase, phaseBeforeCapture)
        XCTAssertTrue(viewModel.hasActiveWorker)
        XCTAssertEqual(worker.cancelCallCount, 0)
        let manifestData = try unzipEntry("manifest.json", from: artifact.archiveURL)
        let manifest = try XCTUnwrap(JSONSerialization.jsonObject(with: manifestData) as? [String: Any])
        XCTAssertEqual((manifest["worker"] as? [String: Any])?["active"] as? Bool, true)

        await viewModel.stopForQuit()
        XCTAssertEqual(worker.cancelCallCount, 1)
    }

    private func unzipEntry(_ name: String, from archiveURL: URL) throws -> Data {
        let process = Process()
        let output = Pipe()
        let error = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/unzip")
        process.arguments = ["-p", archiveURL.path, name]
        process.standardOutput = output
        process.standardError = error
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        let errorData = error.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        if process.terminationStatus != 0 {
            throw NSError(
                domain: "DiagnosticBundleTests",
                code: Int(process.terminationStatus),
                userInfo: [NSLocalizedDescriptionKey: String(decoding: errorData, as: UTF8.self)]
            )
        }
        return data
    }
}

private final class HoldingDiagnosticWorkerClient: WorkerProcessRunning, @unchecked Sendable {
    private let activeExpectation: XCTestExpectation
    private let sensitivePath: String
    private let lock = NSLock()
    private var cancellationContinuation: CheckedContinuation<Void, Never>?
    private var cancellationRequested = false
    private var cancellationCount = 0

    init(active: XCTestExpectation, sensitivePath: String) {
        activeExpectation = active
        self.sensitivePath = sensitivePath
    }

    var cancelCallCount: Int {
        lock.lock()
        defer { lock.unlock() }
        return cancellationCount
    }

    func run(
        job: WorkerJobSpec,
        onEvent: @escaping (WorkerEvent) async throws -> Void
    ) async throws -> WorkerRunResult {
        try await onEvent(
            WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .workerReady,
                jobID: job.jobID,
                sequence: 0,
                payload: WorkerEventPayload(workerVersion: "test", processGroupID: 4242)
            )
        )
        try await onEvent(
            WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .jobStarted,
                jobID: job.jobID,
                sequence: 1,
                payload: WorkerEventPayload(operation: job.operation)
            )
        )
        try await onEvent(
            WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .heartbeat,
                jobID: job.jobID,
                sequence: 2,
                payload: WorkerEventPayload(
                    stage: "inspect_source",
                    message: "Reading \(sensitivePath)",
                    elapsedSeconds: 1
                )
            )
        )
        activeExpectation.fulfill()
        await waitForCancellation()
        let cancelled = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .jobCancelled,
            jobID: job.jobID,
            sequence: 3,
            payload: WorkerEventPayload(message: "Cancelled")
        )
        try await onEvent(cancelled)
        return WorkerRunResult(
            terminalEvent: cancelled,
            exitStatus: SIGTERM,
            diagnostics: "",
            diagnosticSnapshot: WorkerProcessDiagnosticSnapshot(
                isRunning: false,
                processIdentifier: 4242,
                processGroupIdentifier: 4242,
                cancellationRequested: true,
                toolOutput: diagnosticSnapshot().toolOutput
            )
        )
    }

    func cancel() {
        let continuation: CheckedContinuation<Void, Never>?
        lock.lock()
        cancellationCount += 1
        cancellationRequested = true
        continuation = cancellationContinuation
        cancellationContinuation = nil
        lock.unlock()
        continuation?.resume()
    }

    func diagnosticSnapshot() -> WorkerProcessDiagnosticSnapshot {
        lock.lock()
        let cancelled = cancellationRequested
        lock.unlock()
        let text = DiagnosticTextSnapshot(
            text: "File \"\(sensitivePath)\" remains active",
            retainedBytes: sensitivePath.utf8.count + 22,
            totalBytes: sensitivePath.utf8.count + 22,
            droppedBytes: 0
        )
        return WorkerProcessDiagnosticSnapshot(
            isRunning: !cancelled,
            processIdentifier: 4242,
            processGroupIdentifier: 4242,
            cancellationRequested: cancelled,
            toolOutput: text
        )
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

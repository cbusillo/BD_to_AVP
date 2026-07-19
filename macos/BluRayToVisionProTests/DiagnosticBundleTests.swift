import Foundation
import XCTest
@testable import BluRayToVisionPro

final class DiagnosticBundleTests: XCTestCase {
    private let fixedDate = Date(timeIntervalSince1970: 1_784_323_200)
    private let fixedBundleID = UUID(uuidString: "01234567-89AB-4CDE-8F01-23456789ABCD")!

    func testDiagnosticZipArchiveMatchesSharedNativeFixture() throws {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/support_diagnostics_native_v1.b64")
        let expectedBase64 = try String(contentsOf: fixtureURL, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let archive = try DiagnosticZipArchive.data(
            entries: [
                .init(name: "manifest.json", data: Data(#"{"schema_version":1}"#.utf8)),
                .init(
                    name: "events.jsonl",
                    data: Data("{\"schema_version\":1,\"source\":\"client\"}\n".utf8)
                ),
                .init(
                    name: "storage.json",
                    data: Data(#"{"schema_version":1,"probes":[]}"#.utf8)
                ),
                .init(
                    name: "tool-tail.txt",
                    data: Data("# bd_to_avp_support_tool_tail schema_version=1\n".utf8)
                ),
            ],
            modificationDate: fixedDate
        )

        XCTAssertEqual(archive.base64EncodedString(), expectedBase64)
    }

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

    func testBoundedTextBufferCapsLargeChunksWithoutSplittingLeadingUTF8() {
        let buffer = BoundedDiagnosticTextBuffer(maximumBytes: 17)
        let input = Data((String(repeating: "old", count: 100_000) + "😀tail-marker").utf8)

        buffer.append(input)

        let snapshot = buffer.snapshot()
        XCTAssertLessThanOrEqual(snapshot.retainedBytes, 17)
        XCTAssertEqual(snapshot.totalBytes, input.count)
        XCTAssertEqual(snapshot.droppedBytes, input.count - snapshot.retainedBytes)
        XCTAssertTrue(snapshot.text.hasSuffix("tail-marker"))
        XCTAssertFalse(snapshot.text.contains("�"))
    }

    func testBoundedUTF8PrefixIncludesMarkerWithinEveryByteCap() {
        let value = String(repeating: "😀", count: 32)

        for maximumBytes in [0, 1, 5, 11, 12, 16, 32] {
            let result = DiagnosticBundleBuilder.boundedUTF8Prefix(
                value,
                maximumBytes: maximumBytes
            )

            XCTAssertTrue(result.truncated)
            XCTAssertLessThanOrEqual(result.value.utf8.count, maximumBytes)
            XCTAssertFalse(result.value.contains("�"))
        }

        let result = DiagnosticBundleBuilder.boundedUTF8Prefix(value, maximumBytes: 16)
        XCTAssertTrue(result.value.hasSuffix("<truncated>"))
    }

    func testEventHistoryAccountsForJSONEscapingBytes() {
        let plain = diagnosticEvent(message: String(repeating: "a", count: 256))
        let escaped = diagnosticEvent(message: String(repeating: "\"", count: 256))
        XCTAssertEqual(plain.message?.utf8.count, escaped.message?.utf8.count)
        XCTAssertGreaterThan(escaped.serializedByteCount, plain.serializedByteCount)
        var history = DiagnosticEventHistory(
            maximumEntries: 8,
            maximumBytes: escaped.serializedByteCount * 2 - 1
        )

        history.append(escaped)
        history.append(escaped)

        let snapshot = history.snapshot()
        XCTAssertEqual(snapshot.entries.count, 1)
        XCTAssertEqual(snapshot.droppedEntries, 1)
        XCTAssertEqual(snapshot.droppedBytes, escaped.serializedByteCount)
    }

    func testSerializedByteCountMatchesJSONEncoderForLineSeparators() {
        let separatorMsg = "\u{2028}\u{2029}"
        let asciiMsg = "ab"
        let separator = diagnosticEvent(message: separatorMsg)
        let ascii = diagnosticEvent(message: asciiMsg)

        let utf8Diff = separatorMsg.utf8.count - asciiMsg.utf8.count
        XCTAssertEqual(separator.serializedByteCount - ascii.serializedByteCount, utf8Diff)

        var history = DiagnosticEventHistory(
            maximumEntries: 8,
            maximumBytes: separator.serializedByteCount * 2 - 1
        )
        history.append(separator)
        history.append(separator)

        let snapshot = history.snapshot()
        XCTAssertEqual(snapshot.entries.count, 1)
        XCTAssertEqual(snapshot.droppedEntries, 1)
        XCTAssertEqual(snapshot.droppedBytes, separator.serializedByteCount)
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

    func testRedactorHandlesEscapedAndSchemePathsTitleArgumentsAndSecretEnvironmentKeys() {
        let redactor = DiagnosticRedactor(bundleID: fixedBundleID)
        let text = """
        shell=/Users/alice/Movies/Secret\\ Feature.mkv
        source=file:/Users/alice/Movies/Another\\ Secret.mkv
        device=dev:/dev/rdisk7
        image=iso:/Volumes/Private\\ Disc/Feature.iso
        arguments: converter --title Secret\\ Feature --mode safe
        command: /Applications/Tool\\ Suite/ffmpeg -i /Users/alice/Movies/Input\\ Feature.mkv --title Input\\ Feature
        OPENAI_API_KEY   =   "sk-live secret value"
        "AWS_SECRET_ACCESS_KEY"  :  "aws secret value"
        process_group_id = 4242
        pid 9876
        NORMAL_VALUE = visible
        """

        let output = redactor.redact(text)

        XCTAssertFalse(output.contains(#"/Users/alice/Movies/Secret\ Feature.mkv"#))
        XCTAssertFalse(output.contains(#"file:/Users/alice/Movies/Another\ Secret.mkv"#))
        XCTAssertFalse(output.contains("dev:/dev/rdisk7"))
        XCTAssertFalse(output.contains(#"iso:/Volumes/Private\ Disc/Feature.iso"#))
        XCTAssertFalse(output.contains(#"/Applications/Tool\ Suite/ffmpeg"#))
        XCTAssertFalse(output.contains("sk-live secret value"))
        XCTAssertFalse(output.contains("aws secret value"))
        XCTAssertFalse(output.contains("4242"))
        XCTAssertFalse(output.contains("9876"))
        XCTAssertTrue(output.contains("--title <title:redacted>"))
        XCTAssertTrue(output.contains("ffmpeg <arguments:redacted>"))
        XCTAssertTrue(output.contains("OPENAI_API_KEY=<redacted>"))
        XCTAssertTrue(output.contains("AWS_SECRET_ACCESS_KEY=<redacted>"))
        XCTAssertTrue(output.contains("process_group_id=<redacted>"))
        XCTAssertTrue(output.contains("pid=<redacted>"))
        XCTAssertTrue(output.contains("NORMAL_VALUE = visible"))
        XCTAssertGreaterThanOrEqual(output.components(separatedBy: "<path:01234567:").count - 1, 4)
    }

    func testRedactorRemovesArbitraryMediaMetadataValues() {
        let redactor = DiagnosticRedactor(bundleID: fixedBundleID)
        let text = """
        Input #0, mov, from '<path:redacted>':
          Metadata:
            artist          : Private Artist
            custom_tag      : personally identifying value
          Duration: 00:10:00.00
        frame=1
        """

        let output = redactor.redact(text)

        XCTAssertFalse(output.contains("Private Artist"))
        XCTAssertFalse(output.contains("personally identifying value"))
        XCTAssertTrue(output.contains("artist: <metadata:redacted>"))
        XCTAssertTrue(output.contains("custom_tag: <metadata:redacted>"))
        XCTAssertTrue(output.contains("Duration: 00:10:00.00"))
        XCTAssertTrue(output.contains("frame=1"))
    }

    func testRedactorRemovesCRLFAndMultilineMetadataValues() {
        let redactor = DiagnosticRedactor(bundleID: fixedBundleID)
        let text = "Input #0:\r\n  Metadata:\r\n    comment: first private line\r\n    : second private line\r\n  Duration: 00:10:00.00\r\n"

        let output = redactor.redact(text)

        XCTAssertFalse(output.contains("first private line"))
        XCTAssertFalse(output.contains("second private line"))
        XCTAssertTrue(output.contains("comment: <metadata:redacted>"))
        XCTAssertTrue(output.contains(": <metadata:redacted>"))
        XCTAssertTrue(output.contains("Duration: 00:10:00.00"))
    }

    func testStorageProbeClassifiesInaccessibleMetadataWithoutLeakingPath() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let fileURL = directory.appendingPathComponent("Private Movie.mkv")
        defer { try? FileManager.default.removeItem(at: directory) }
        let probe = FileSystemDiagnosticStorageProbe { _, _ in
            throw NSError(
                domain: NSCocoaErrorDomain,
                code: NSFileReadNoSuchFileError,
                userInfo: [
                    NSUnderlyingErrorKey: NSError(
                        domain: NSPOSIXErrorDomain,
                        code: Int(EACCES)
                    ),
                ]
            )
        }

        let result = probe.probe(role: .source, url: fileURL, capturedAt: fixedDate)

        XCTAssertEqual(result.status, .inaccessible)
        XCTAssertEqual(result.errorKind, .permissionDenied)
        XCTAssertNil(result.fileSizeBytes)
    }

    func testStorageProbeClassifiesThrowingMissingMetadataSeparately() {
        let missingURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: false)
        let probe = FileSystemDiagnosticStorageProbe()

        let result = probe.probe(role: .output, url: missingURL, capturedAt: fixedDate)

        XCTAssertEqual(result.status, .missing)
        XCTAssertNil(result.errorKind)
        XCTAssertNil(result.fileSizeBytes)
    }

    func testCreateBundleRemovesNewlyCreatedDirectoryOnWriteFailure() throws {
        let base = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: base) }
        try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)

        let outputDirectory = base.appendingPathComponent("bundles", isDirectory: true)
        XCTAssertFalse(FileManager.default.fileExists(atPath: outputDirectory.path))

        let writeError = CocoaError(.fileWriteUnknown)
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
            archiveWriter: { _, _ in throw writeError }
        )

        XCTAssertThrowsError(try builder.createBundle(from: snapshot, outputDirectory: outputDirectory))
        XCTAssertFalse(FileManager.default.fileExists(atPath: outputDirectory.path),
                       "Newly created output directory should be removed after write failure")
    }

    func testCreateBundlePreservesPreexistingDirectoryOnWriteFailure() throws {
        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: outputDirectory) }

        let writeError = CocoaError(.fileWriteUnknown)
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
            archiveWriter: { _, _ in throw writeError }
        )

        XCTAssertThrowsError(try builder.createBundle(from: snapshot, outputDirectory: outputDirectory))
        XCTAssertTrue(FileManager.default.fileExists(atPath: outputDirectory.path),
                      "Pre-existing output directory must not be removed on write failure")
    }

    func testRedactionContextEvictsOldestWhenCapacityExceeded() {
        let recorder = DiagnosticSessionRecorder(
            maximumEvents: 3,
            maximumEventBytes: 64 * 1_024,
            maximumStorageSamples: 2,
            storageSampleInterval: 0
        )
        var lifecycle = WorkerLifecycleState()
        var firstJobID: UUID?
        for i in 0..<4 {
            let jobID = UUID()
            if i == 0 { firstJobID = jobID }
            let source = ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/tmp/movie\(i).mkv"))
            lifecycle.selectSource(source.url)
            try? lifecycle.begin(jobID: jobID)
            recorder.beginJob(
                context: DiagnosticJobContext(jobID: jobID, source: source),
                lifecycle: lifecycle,
                activeMode: "batch_conversion",
                recordedAt: fixedDate.addingTimeInterval(TimeInterval(i))
            )
        }

        let snapshot = recorder.snapshot(
            capturedAt: fixedDate.addingTimeInterval(5),
            lifecycle: lifecycle,
            activeMode: nil,
            batchSummary: nil,
            process: .empty
        )

        let retainedJobIDs = snapshot.redactionContexts.map(\.jobID)
        XCTAssertEqual(retainedJobIDs.count, 3)
        XCTAssertFalse(retainedJobIDs.contains(firstJobID!),
                       "Oldest redaction context should be evicted when capacity is exceeded")
    }

    func testCreateBundleThrowsPayloadTooLargeWhenStorageExceedsLimit() throws {
        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: outputDirectory) }

        let tinyConfig = DiagnosticBundleBuilder.Configuration(
            maximumArchiveBytes: 2 * 1_024 * 1_024,
            maximumUncompressedBytes: 1_500_000,
            maximumManifestBytes: 64 * 1_024,
            maximumEventsBytes: 320 * 1_024,
            maximumStorageBytes: 1,
            maximumToolTailBytes: 640 * 1_024
        )
        let recorder = DiagnosticSessionRecorder()
        let snapshot = recorder.snapshot(
            capturedAt: fixedDate,
            lifecycle: WorkerLifecycleState(),
            activeMode: nil,
            batchSummary: nil,
            process: .empty
        )
        let builder = DiagnosticBundleBuilder(
            configuration: tinyConfig,
            bundleIDProvider: { self.fixedBundleID }
        )

        XCTAssertThrowsError(try builder.createBundle(from: snapshot, outputDirectory: outputDirectory)) { error in
            guard case DiagnosticBundleError.payloadTooLarge = error else {
                return XCTFail("Expected payloadTooLarge, got \(error)")
            }
        }
        let contents = (try? FileManager.default.contentsOfDirectory(atPath: outputDirectory.path)) ?? []
        XCTAssertTrue(contents.isEmpty, "No archive file should remain after a payloadTooLarge failure")
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

    func testRecorderProjectsCanonicalObservabilityFields() throws {
        let observability = try JSONDecoder().decode(
            ObservabilityEvent.self,
            from: Data(#"{"schema":"bd_to_avp.observability","schema_version":1,"emitter":"worker","stream_id":"11111111-1111-4111-8111-111111111111","sequence":0,"occurred_at":"2026-07-18T00:00:00Z","elapsed_ms":10,"kind":"tool.failed","severity":"error","privacy":"private","redaction":"raw","context":{"correlation":{},"stage":{"id":"create_mkv"},"tool":{"id":"makemkvcon"},"process":{"pid":42,"exit_code":1}},"data":{"message":{"value":"MakeMKV failed","privacy":"private","truncated":false},"detail":{"value":"bounded detail","privacy":"private","truncated":false},"artifact":{"role":"intermediate","state":"growing","location":{"value":"/private/output/movie.mkv","privacy":"private","truncated":false},"size_bytes":536870912,"modification_age_seconds":2,"growth_bytes_per_second":1048576},"failure":{"code":"nonzero_exit","retryable":false},"activity":{"last_output_age_seconds":31}}}"#.utf8)
        )
        let jobID = UUID()
        var lifecycle = WorkerLifecycleState()
        lifecycle.selectSource(URL(fileURLWithPath: "/tmp/movie.mkv"))
        try lifecycle.begin(jobID: jobID, operationKind: .conversion)
        let event = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .observability,
            jobID: jobID,
            sequence: 0,
            payload: WorkerEventPayload(observabilityEvent: observability)
        )
        try lifecycle.receive(event)
        let recorder = DiagnosticSessionRecorder()

        recorder.record(
            event: event,
            lifecycle: lifecycle,
            activeMode: "single_conversion",
            recordedAt: fixedDate
        )
        let snapshot = recorder.snapshot(
            capturedAt: fixedDate,
            lifecycle: lifecycle,
            activeMode: nil,
            batchSummary: nil,
            process: .empty
        )

        let record = try XCTUnwrap(snapshot.events.entries.last)
        XCTAssertEqual(record.source, "worker")
        XCTAssertEqual(record.name, "tool.failed")
        XCTAssertEqual(record.stage, "create_mkv")
        XCTAssertEqual(record.tool, "makemkvcon")
        XCTAssertEqual(record.processState, "failed")
        XCTAssertEqual(record.lastOutputAgeSeconds, 31)
        XCTAssertEqual(record.artifactRole, "intermediate")
        XCTAssertEqual(record.artifactState, "growing")
        XCTAssertEqual(record.artifactSizeBytes, 536_870_912)
        XCTAssertEqual(record.artifactModificationAgeSeconds, 2)
        XCTAssertEqual(record.artifactGrowthBytesPerSecond, 1_048_576)
        XCTAssertEqual(record.privacy, "private")
        XCTAssertEqual(record.redaction, "raw")
        XCTAssertEqual(record.messagePrivacy, "private")
        XCTAssertEqual(record.detailsPrivacy, "private")
        XCTAssertEqual(record.message, "MakeMKV failed")
        XCTAssertEqual(record.details, "bounded detail")
        XCTAssertEqual(record.level, "error")
        XCTAssertEqual(record.failureCode, "nonzero_exit")
        XCTAssertEqual(record.retryable, false)

        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let artifact = try DiagnosticBundleBuilder(
            bundleIDProvider: { self.fixedBundleID }
        ).createBundle(from: snapshot, outputDirectory: directory)
        let eventsData = try unzipEntry("events.jsonl", from: artifact.archiveURL)
        let exported = try XCTUnwrap(
            JSONSerialization.jsonObject(with: eventsData.split(separator: 0x0A)[0])
                as? [String: Any]
        )
        XCTAssertEqual(exported["tool"] as? String, "makemkvcon")
        XCTAssertEqual(exported["process_state"] as? String, "failed")
        XCTAssertEqual(exported["last_output_age_seconds"] as? Int, 31)
        XCTAssertEqual(exported["artifact_role"] as? String, "intermediate")
        XCTAssertEqual(exported["privacy"] as? String, "private")
        XCTAssertEqual(exported["redaction"] as? String, "raw")
        XCTAssertEqual(exported["message_privacy"] as? String, "private")
        XCTAssertEqual(exported["details_privacy"] as? String, "private")
        XCTAssertEqual(
            (exported["artifact_size_rounded_bytes"] as? NSNumber)?.int64Value,
            536_870_912
        )
        XCTAssertEqual(
            (exported["artifact_growth_rounded_bytes_per_second"] as? NSNumber)?.int64Value,
            1_048_576
        )
        XCTAssertFalse(String(decoding: eventsData, as: UTF8.self).contains("/private/output"))
    }

    func testRecorderOmitsCanonicalTextMarkedOmitted() throws {
        let observability = try JSONDecoder().decode(
            ObservabilityEvent.self,
            from: Data(#"{"schema":"bd_to_avp.observability","schema_version":1,"emitter":"worker","stream_id":"11111111-1111-4111-8111-111111111111","sequence":0,"occurred_at":"2026-07-19T00:00:00Z","kind":"tool.failed","severity":"error","privacy":"private","redaction":"omitted","context":{"correlation":{}},"data":{"message":{"value":"private message","privacy":"private","truncated":false},"detail":{"value":"private detail","privacy":"private","truncated":false}}}"#.utf8)
        )
        let jobID = UUID()
        var lifecycle = WorkerLifecycleState()
        lifecycle.selectSource(URL(fileURLWithPath: "/tmp/movie.mkv"))
        try lifecycle.begin(jobID: jobID, operationKind: .conversion)
        let event = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .observability,
            jobID: jobID,
            sequence: 0,
            payload: WorkerEventPayload(observabilityEvent: observability)
        )
        try lifecycle.receive(event)
        let recorder = DiagnosticSessionRecorder()

        recorder.record(
            event: event,
            lifecycle: lifecycle,
            activeMode: "single_conversion",
            recordedAt: fixedDate
        )

        let record = try XCTUnwrap(
            recorder.snapshot(
                capturedAt: fixedDate,
                lifecycle: lifecycle,
                activeMode: nil,
                batchSummary: nil,
                process: .empty
            ).events.entries.last
        )
        XCTAssertEqual(record.redaction, "omitted")
        XCTAssertNil(record.message)
        XCTAssertNil(record.details)
    }

    func testWorkflowAttributionUsesExplicitJobInsteadOfLatestContext() throws {
        let firstSource = ConversionSource(
            kind: .matroska,
            url: URL(fileURLWithPath: "/tmp/first.mkv")
        )
        let secondSource = ConversionSource(
            kind: .transportStream,
            url: URL(fileURLWithPath: "/tmp/second.m2ts")
        )
        let secondDraft = ConversionDraft(
            source: secondSource,
            sourceDetails: SourceInspection(
                name: "second",
                resolution: "1920x1080",
                frameRate: "24/1",
                interlaced: false
            ),
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/output", isDirectory: true),
            options: ConversionOptions()
        )
        let firstJobID = UUID()
        let secondJobID = UUID()
        var lifecycle = WorkerLifecycleState()
        lifecycle.selectSource(firstSource.url)
        try lifecycle.begin(jobID: firstJobID)
        let recorder = DiagnosticSessionRecorder()
        recorder.beginJob(
            context: DiagnosticJobContext(jobID: firstJobID, source: firstSource),
            lifecycle: lifecycle,
            activeMode: "batch_inspection",
            recordedAt: fixedDate
        )
        lifecycle.selectSource(secondSource.url)
        try lifecycle.begin(jobID: secondJobID, operationKind: .conversion)
        recorder.beginJob(
            context: DiagnosticJobContext(jobID: secondJobID, draft: secondDraft),
            lifecycle: lifecycle,
            activeMode: "batch_conversion",
            recordedAt: fixedDate.addingTimeInterval(1)
        )

        recorder.recordWorkflow(
            name: "batch.retry_requested",
            lifecycle: lifecycle,
            activeMode: nil,
            recordedAt: fixedDate.addingTimeInterval(2),
            jobID: firstJobID
        )
        recorder.recordWorkflow(
            name: "batch.finished",
            lifecycle: lifecycle,
            activeMode: nil,
            recordedAt: fixedDate.addingTimeInterval(3)
        )

        let entries = recorder.snapshot(
            capturedAt: fixedDate.addingTimeInterval(4),
            lifecycle: lifecycle,
            activeMode: nil,
            batchSummary: nil,
            process: .empty
        ).events.entries
        XCTAssertEqual(entries.suffix(2).map(\.jobID), [firstJobID, nil])
        XCTAssertEqual(entries.suffix(2).map(\.operation), ["inspect_source", nil])
    }

    func testProcessExitStatusIsStructuredInManifestAndEvents() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let source = ConversionSource(
            kind: .matroska,
            url: directory.appendingPathComponent("failed.mkv")
        )
        let jobID = UUID()
        var lifecycle = WorkerLifecycleState()
        lifecycle.selectSource(source.url)
        try lifecycle.begin(jobID: jobID)
        lifecycle.failTransport(message: "Worker failed")
        let recorder = DiagnosticSessionRecorder()
        recorder.beginJob(
            context: DiagnosticJobContext(jobID: jobID, source: source),
            lifecycle: lifecycle,
            activeMode: "single_inspection",
            recordedAt: fixedDate
        )
        recorder.recordWorkflow(
            name: "process.failed",
            lifecycle: lifecycle,
            activeMode: "single_inspection",
            recordedAt: fixedDate.addingTimeInterval(1),
            jobID: jobID,
            exitStatus: 23
        )
        let snapshot = recorder.snapshot(
            capturedAt: fixedDate.addingTimeInterval(2),
            lifecycle: lifecycle,
            activeMode: nil,
            batchSummary: nil,
            process: .empty
        )
        let builder = DiagnosticBundleBuilder(bundleIDProvider: { self.fixedBundleID })

        let artifact = try builder.createBundle(from: snapshot, outputDirectory: directory)
        let manifestData = try unzipEntry("manifest.json", from: artifact.archiveURL)
        let eventsData = try unzipEntry("events.jsonl", from: artifact.archiveURL)
        let manifest = try XCTUnwrap(JSONSerialization.jsonObject(with: manifestData) as? [String: Any])
        let eventLine = try XCTUnwrap(String(decoding: eventsData, as: UTF8.self).split(separator: "\n").last)
        let event = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(eventLine.utf8)) as? [String: Any]
        )

        XCTAssertEqual((manifest["worker"] as? [String: Any])?["exit_status"] as? Int, 23)
        XCTAssertEqual(event["exit_status"] as? Int, 23)
    }

    func testSerializedDiagnosticsOmitProcessIdentifiersAndRoundMediaAndStorageSizes() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let fileQuantum: Int64 = 256 * 1_024 * 1_024
        let volumeQuantum: Int64 = 16 * 1_024 * 1_024 * 1_024
        let rawFileSize = fileQuantum * 7 + 12_345
        let rawAvailableSize = volumeQuantum * 31 + 54_321
        let rawTotalSize = volumeQuantum * 63 + 98_765
        let sourceURL = directory.appendingPathComponent("Private Source.mkv")
        let destinationURL = directory.appendingPathComponent("Private Destination", isDirectory: true)
        let source = ConversionSource(kind: .matroska, url: sourceURL)
        let draft = ConversionDraft(
            source: source,
            sourceDetails: SourceInspection(
                name: "Private Source.mkv",
                resolution: "1920x1080",
                frameRate: "24/1",
                interlaced: false,
                sizeBytes: rawFileSize
            ),
            profile: BuiltInProfile.balanced.profile,
            destinationURL: destinationURL,
            options: ConversionOptions()
        )
        let probe = FixedDiagnosticStorageProbe(
            fileSizeBytes: rawFileSize,
            volumeAvailableBytes: rawAvailableSize,
            volumeTotalBytes: rawTotalSize
        )
        let jobID = UUID()
        var lifecycle = WorkerLifecycleState()
        lifecycle.selectSource(sourceURL)
        try lifecycle.begin(jobID: jobID, operationKind: .conversion)
        let recorder = DiagnosticSessionRecorder(storageSampleInterval: 0)
        recorder.beginJob(
            context: DiagnosticJobContext(jobID: jobID, draft: draft),
            lifecycle: lifecycle,
            activeMode: "single_conversion",
            recordedAt: fixedDate
        )
        recorder.record(
            event: WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .workerReady,
                jobID: jobID,
                sequence: 0,
                payload: WorkerEventPayload(workerVersion: "0.3.0", processGroupID: 4242)
            ),
            lifecycle: lifecycle,
            activeMode: "single_conversion",
            recordedAt: fixedDate.addingTimeInterval(1)
        )
        recorder.record(
            event: WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .jobCompleted,
                jobID: jobID,
                sequence: 1,
                payload: WorkerEventPayload(
                    conversionResult: ConversionResult(
                        outputPath: draft.proposedOutputURL.path,
                        sizeBytes: rawFileSize
                    )
                )
            ),
            lifecycle: lifecycle,
            activeMode: "single_conversion",
            recordedAt: fixedDate.addingTimeInterval(2)
        )
        recordStorageSamples(
            recorder: recorder,
            using: probe,
            recordedAt: fixedDate.addingTimeInterval(3),
            force: true
        )
        let snapshot = recorder.snapshot(
            capturedAt: fixedDate.addingTimeInterval(4),
            lifecycle: lifecycle,
            activeMode: "single_conversion",
            batchSummary: nil,
            process: WorkerProcessDiagnosticSnapshot(
                isRunning: true,
                processIdentifier: 4242,
                processGroupIdentifier: 4242,
                cancellationRequested: false,
                toolOutput: .empty
            )
        )
        let builder = DiagnosticBundleBuilder(
            storageProbe: probe,
            bundleIDProvider: { self.fixedBundleID }
        )

        let artifact = try builder.createBundle(from: snapshot, outputDirectory: directory)
        let manifestData = try unzipEntry("manifest.json", from: artifact.archiveURL)
        let eventsData = try unzipEntry("events.jsonl", from: artifact.archiveURL)
        let storageData = try unzipEntry("storage.json", from: artifact.archiveURL)
        let manifest = try XCTUnwrap(JSONSerialization.jsonObject(with: manifestData) as? [String: Any])
        let worker = try XCTUnwrap(manifest["worker"] as? [String: Any])
        let privacy = try XCTUnwrap(manifest["privacy"] as? [String: Any])
        let events = try eventsData.split(separator: 0x0A).map { line in
            try XCTUnwrap(JSONSerialization.jsonObject(with: Data(line)) as? [String: Any])
        }
        let completedEvent = try XCTUnwrap(events.first { $0["name"] as? String == "job.completed" })
        let storage = try XCTUnwrap(JSONSerialization.jsonObject(with: storageData) as? [String: Any])
        let probes = try XCTUnwrap(storage["probes"] as? [[String: Any]])
        let sourceProbe = try XCTUnwrap(probes.first { $0["role"] as? String == "source" })
        let samples = try XCTUnwrap(storage["samples"] as? [[String: Any]])
        let serializedText = [manifestData, eventsData, storageData]
            .map { String(decoding: $0, as: UTF8.self) }
            .joined(separator: "\n")

        XCTAssertNil(worker["process_identifier"])
        XCTAssertNil(worker["process_group_identifier"])
        XCTAssertTrue(events.allSatisfy { $0["process_group_identifier"] == nil })
        XCTAssertEqual((completedEvent["result_size_rounded_bytes"] as? NSNumber)?.int64Value, fileQuantum * 7)
        XCTAssertNil(completedEvent["result_size_bytes"])
        XCTAssertEqual((sourceProbe["file_size_rounded_bytes"] as? NSNumber)?.int64Value, fileQuantum * 7)
        XCTAssertEqual(
            (sourceProbe["volume_available_rounded_bytes"] as? NSNumber)?.int64Value,
            volumeQuantum * 31
        )
        XCTAssertEqual((sourceProbe["volume_total_rounded_bytes"] as? NSNumber)?.int64Value, volumeQuantum * 63)
        XCTAssertTrue(samples.allSatisfy { $0["file_size_bytes"] == nil && $0["volume_available_bytes"] == nil })
        XCTAssertEqual(privacy["rules_version"] as? Int, 3)
        XCTAssertEqual(privacy["size_rounding_mode"] as? String, "down")
        XCTAssertEqual((privacy["file_size_quantum_bytes"] as? NSNumber)?.int64Value, fileQuantum)
        XCTAssertEqual((privacy["volume_capacity_quantum_bytes"] as? NSNumber)?.int64Value, volumeQuantum)
        XCTAssertEqual(
            (privacy["byte_rate_quantum_bytes_per_second"] as? NSNumber)?.int64Value,
            1_024 * 1_024
        )
        XCTAssertFalse(serializedText.contains(String(rawFileSize)))
        XCTAssertFalse(serializedText.contains(String(rawAvailableSize)))
        XCTAssertFalse(serializedText.contains(String(rawTotalSize)))
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
        recordStorageSamples(
            recorder: recorder,
            using: storageProbe,
            recordedAt: fixedDate,
            force: true
        )
        recordStorageSamples(
            recorder: recorder,
            using: storageProbe,
            recordedAt: fixedDate.addingTimeInterval(10),
            force: true
        )
        recordStorageSamples(
            recorder: recorder,
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
            process: process,
            observabilityPersistence: ObservabilityEventPersistenceSnapshot(
                enabled: true,
                maximumFileBytes: 4 * 1_024 * 1_024,
                maximumTotalBytes: 12 * 1_024 * 1_024,
                maximumPendingBytes: 4 * 1_024 * 1_024,
                pendingBytes: 1_024,
                writtenEvents: 30,
                writtenBytes: 65_536,
                droppedEvents: 2,
                droppedBytes: 4_096,
                failureCount: 1
            )
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
        let localStore = try XCTUnwrap(manifest["local_observability_store"] as? [String: Any])
        XCTAssertEqual(localStore["enabled"] as? Bool, true)
        XCTAssertEqual(localStore["maximum_total_bytes"] as? Int, 12 * 1_024 * 1_024)
        XCTAssertEqual(localStore["written_events"] as? Int, 30)
        XCTAssertEqual(localStore["dropped_events"] as? Int, 2)
        XCTAssertNil(localStore["location"])
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
    func testActiveViewModelCaptureRunsOffMainAndDoesNotMutateWorker() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let sourceURL = directory.appendingPathComponent("Private Active Movie.mkv")
        XCTAssertTrue(FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8)))
        defer { try? FileManager.default.removeItem(at: directory) }
        let active = expectation(description: "worker active")
        let worker = HoldingDiagnosticWorkerClient(active: active, sensitivePath: sourceURL.path)
        let storageThread = ThreadObservation()
        let builderThread = ThreadObservation()
        let storageProbe = ThreadRecordingDiagnosticStorageProbe(observation: storageThread)
        let builder = DiagnosticBundleBuilder(
            storageProbe: storageProbe,
            bundleIDProvider: {
                builderThread.recordCurrentThread()
                return self.fixedBundleID
            },
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

        let artifact = try await viewModel.captureDiagnosticBundle(in: directory)

        XCTAssertEqual(viewModel.state.phase, phaseBeforeCapture)
        XCTAssertTrue(viewModel.hasActiveWorker)
        XCTAssertEqual(worker.cancelCallCount, 0)
        XCTAssertEqual(storageThread.mainThreadObservation, false)
        XCTAssertEqual(builderThread.mainThreadObservation, false)
        let manifestData = try unzipEntry("manifest.json", from: artifact.archiveURL)
        let manifest = try XCTUnwrap(JSONSerialization.jsonObject(with: manifestData) as? [String: Any])
        XCTAssertEqual((manifest["worker"] as? [String: Any])?["active"] as? Bool, true)

        await viewModel.stopForQuit()
        XCTAssertEqual(worker.cancelCallCount, 1)
    }

    @MainActor
    func testSlowCaptureDoesNotBlockWorkerEventsAndUsesFrozenActiveSnapshot() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let sourceURL = directory.appendingPathComponent("Private Active Movie.mkv")
        XCTAssertTrue(FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8)))
        defer { try? FileManager.default.removeItem(at: directory) }
        let active = expectation(description: "worker active")
        let probeStarted = expectation(description: "capture probe started")
        let worker = HoldingDiagnosticWorkerClient(active: active, sensitivePath: sourceURL.path)
        let storageProbe = BlockingDiagnosticStorageProbe(started: probeStarted)
        defer { storageProbe.release() }
        let builder = DiagnosticBundleBuilder(
            storageProbe: storageProbe,
            bundleIDProvider: { self.fixedBundleID }
        )
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            diagnosticClock: { self.fixedDate },
            diagnosticStorageProbe: storageProbe,
            diagnosticBundleBuilder: builder
        )

        viewModel.selectSource(sourceURL)
        await fulfillment(of: [active], timeout: 2)
        let captureTask = Task {
            try await viewModel.captureDiagnosticBundle(in: directory)
        }
        await fulfillment(of: [probeStarted], timeout: 2)

        await viewModel.stopForQuit()

        XCTAssertEqual(viewModel.state.phase, .cancelled)
        XCTAssertFalse(viewModel.hasActiveWorker)
        storageProbe.release()
        let artifact = try await captureTask.value
        let manifestData = try unzipEntry("manifest.json", from: artifact.archiveURL)
        let manifest = try XCTUnwrap(JSONSerialization.jsonObject(with: manifestData) as? [String: Any])
        XCTAssertEqual((manifest["worker"] as? [String: Any])?["active"] as? Bool, true)
    }

    private func diagnosticEvent(message: String) -> DiagnosticEventRecord {
        DiagnosticEventRecord(
            recordedAt: fixedDate,
            source: "worker",
            name: "log",
            jobID: nil,
            sequence: 1,
            phase: "processing",
            operation: "convert_source",
            activeMode: "single_conversion",
            stage: nil,
            tool: nil,
            processState: nil,
            lastOutputAgeSeconds: nil,
            artifactRole: nil,
            artifactState: nil,
            artifactSizeBytes: nil,
            artifactModificationAgeSeconds: nil,
            artifactGrowthBytesPerSecond: nil,
            privacy: nil,
            redaction: nil,
            messagePrivacy: nil,
            detailsPrivacy: nil,
            message: message,
            details: nil,
            level: "info",
            elapsedSeconds: 1,
            progress: nil,
            warningCode: nil,
            failureCode: nil,
            retryable: nil,
            choices: nil,
            resultSizeBytes: nil,
            workerVersion: "test",
            exitStatus: nil
        )
    }

    private func recordStorageSamples(
        recorder: DiagnosticSessionRecorder,
        using probe: any DiagnosticStorageProbing,
        recordedAt: Date,
        force: Bool
    ) {
        guard let request = recorder.makeStorageSampleRequest(
            recordedAt: recordedAt,
            force: force
        ) else {
            return XCTFail("Expected diagnostic storage targets")
        }
        let samples = request.targets.map { target in
            RawDiagnosticStorageSample(
                probe: probe.probe(
                    role: target.role,
                    url: target.url,
                    capturedAt: request.capturedAt
                )
            )
        }
        recorder.recordStorageSamples(samples, for: request.jobID)
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

private final class ThreadObservation: @unchecked Sendable {
    private let lock = NSLock()
    private var observations: [Bool] = []

    var mainThreadObservation: Bool? {
        lock.lock()
        defer { lock.unlock() }
        guard !observations.isEmpty else {
            return nil
        }
        return observations.contains(true)
    }

    func recordCurrentThread() {
        lock.lock()
        observations.append(Thread.isMainThread)
        lock.unlock()
    }
}

private struct ThreadRecordingDiagnosticStorageProbe: DiagnosticStorageProbing {
    let observation: ThreadObservation
    private let probe = FileSystemDiagnosticStorageProbe()

    func probe(role: DiagnosticStorageRole, url: URL, capturedAt: Date) -> RawDiagnosticStorageProbe {
        observation.recordCurrentThread()
        return probe.probe(role: role, url: url, capturedAt: capturedAt)
    }
}

private final class BlockingDiagnosticStorageProbe: DiagnosticStorageProbing, @unchecked Sendable {
    private let started: XCTestExpectation
    private let releaseSemaphore = DispatchSemaphore(value: 0)
    private let lock = NSLock()
    private var hasBlocked = false
    private let probe = FileSystemDiagnosticStorageProbe()

    init(started: XCTestExpectation) {
        self.started = started
    }

    func probe(role: DiagnosticStorageRole, url: URL, capturedAt: Date) -> RawDiagnosticStorageProbe {
        let shouldBlock = lock.withLock { () -> Bool in
            guard !hasBlocked else {
                return false
            }
            hasBlocked = true
            return true
        }
        if shouldBlock {
            started.fulfill()
            releaseSemaphore.wait()
        }
        return probe.probe(role: role, url: url, capturedAt: capturedAt)
    }

    func release() {
        releaseSemaphore.signal()
    }
}

private struct FixedDiagnosticStorageProbe: DiagnosticStorageProbing {
    let fileSizeBytes: Int64
    let volumeAvailableBytes: Int64
    let volumeTotalBytes: Int64

    func probe(role: DiagnosticStorageRole, url: URL, capturedAt: Date) -> RawDiagnosticStorageProbe {
        RawDiagnosticStorageProbe(
            capturedAt: capturedAt,
            role: role,
            url: url,
            status: .available,
            isDirectory: role == .destination,
            isReadable: true,
            isWritable: role != .source,
            fileSizeBytes: fileSizeBytes,
            modificationAgeSeconds: 120,
            volumeAvailableBytes: volumeAvailableBytes,
            volumeTotalBytes: volumeTotalBytes,
            volumeReadOnly: false,
            errorKind: nil
        )
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

import Foundation
import XCTest
@testable import BluRayToVisionPro

final class WorkerProcessClientTests: XCTestCase {
    private let jobID = UUID(uuidString: "97456C4A-F3C5-44E4-A548-0BD833EAD4BB")!

    func testDecodesEventsFromWorkerProcess() async throws {
        let client = fixtureClient(body: """
        print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "worker.ready", "job_id": job_id, "sequence": 0, "payload": {"worker_version": "test", "process_group_id": os.getpid()}}), flush=True)
        print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "job.completed", "job_id": job_id, "sequence": 1, "payload": {"result": {"name": "movie", "resolution": "1920x1080", "frame_rate": "24/1", "interlaced": False, "size_bytes": 10, "titles": []}}}), flush=True)
        """)
        let job = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/tmp/movie.m2ts"), jobID: jobID)
        var events: [WorkerEvent] = []

        let result = try await client.run(job: job) { event in
            events.append(event)
        }

        XCTAssertEqual(events.map(\.type), [.workerReady, .jobCompleted])
        XCTAssertEqual(result.terminalEvent.payload.result?.resolution, "1920x1080")
        XCTAssertEqual(result.exitStatus, 0)
        XCTAssertFalse(result.diagnosticSnapshot.isRunning)
    }

    func testDeliversObservabilityBetweenReadyAndTerminalEvents() async throws {
        let client = fixtureClient(body: """
        \(readyEvent())
        observability = {"schema": "bd_to_avp.observability", "schema_version": 1, "emitter": "worker", "stream_id": job_id, "sequence": 0, "occurred_at": "2026-07-18T00:00:00Z", "elapsed_ms": 1, "kind": "tool.started", "severity": "info", "privacy": "private", "redaction": "raw", "context": {"correlation": {}}, "data": {}}
        print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "observability", "job_id": job_id, "sequence": 1, "payload": {"event": observability}}), flush=True)
        print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "job.completed", "job_id": job_id, "sequence": 2, "payload": {"result": {"name": "movie", "resolution": "1920x1080", "frame_rate": "24/1", "interlaced": False, "size_bytes": 10, "titles": []}}}), flush=True)
        """)
        let job = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/tmp/movie.m2ts"), jobID: jobID)
        var events: [WorkerEvent] = []

        let result = try await client.run(job: job) { event in
            events.append(event)
        }

        XCTAssertEqual(events.map(\.type), [.workerReady, .observability, .jobCompleted])
        XCTAssertEqual(events[1].payload.observabilityEvent?.kind, "tool.started")
        XCTAssertEqual(result.terminalEvent.type, .jobCompleted)
    }

    func testBackpressuresHighVolumeStdoutUntilSlowHandlerCatchesUp() async throws {
        let eventCount = 260
        let payloadBytes = 48 * 1_024
        let client = fixtureClient(body: """
        \(readyEvent())
        sys.stderr.write("WRITER-STARTED\\n")
        sys.stderr.flush()
        message = "x" * \(payloadBytes)
        for sequence in range(1, \(eventCount + 1)):
            print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "log", "job_id": job_id, "sequence": sequence, "payload": {"level": "info", "message": message}}), flush=True)
        sys.stderr.write("WRITER-COMPLETED\\n")
        sys.stderr.flush()
        print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "job.completed", "job_id": job_id, "sequence": \(eventCount + 1), "payload": {"result": {"name": "movie", "resolution": "1920x1080", "frame_rate": "24/1", "interlaced": False, "size_bytes": 10, "titles": []}}}), flush=True)
        """)
        let job = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/tmp/movie.m2ts"), jobID: jobID)
        let slowHandlerStarted = expectation(description: "slow handler started")
        let gate = AsyncGate()
        var sequences: [Int] = []
        let task = Task {
            try await client.run(job: job) { event in
                sequences.append(event.sequence)
                if event.sequence == 1 {
                    slowHandlerStarted.fulfill()
                    await gate.wait()
                }
            }
        }

        await fulfillment(of: [slowHandlerStarted], timeout: 5)
        let writerStarted = await waitForDiagnosticMarker("WRITER-STARTED", client: client)
        XCTAssertTrue(writerStarted)
        guard writerStarted else {
            await gate.open()
            _ = await task.result
            return
        }
        try await Task.sleep(nanoseconds: 250_000_000)
        XCTAssertFalse(client.diagnosticSnapshot().toolOutput.text.contains("WRITER-COMPLETED"))
        await gate.open()
        let result = try await task.value

        XCTAssertEqual(sequences, Array(0 ... (eventCount + 1)))
        XCTAssertEqual(result.terminalEvent.type, .jobCompleted)
        XCTAssertEqual(result.exitStatus, 0)
        XCTAssertTrue(result.diagnostics.contains("WRITER-COMPLETED"))
    }

    func testHandlesProductionShapedProgressBurst() async throws {
        let progressEventCount = 400
        let client = fixtureClient(body: """
        \(readyEvent())
        for sequence in range(1, \(progressEventCount + 1)):
            observability = {"schema": "bd_to_avp.observability", "schema_version": 1, "emitter": "worker", "stream_id": job_id, "sequence": sequence - 1, "occurred_at": "2026-07-22T00:00:00Z", "elapsed_ms": sequence, "kind": "tool.progress", "severity": "info", "privacy": "private", "redaction": "raw", "context": {"correlation": {}}, "data": {"progress": {"completed_units": sequence, "total_units": \(progressEventCount), "unit": "ticks"}}}
            print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "observability", "job_id": job_id, "sequence": sequence, "payload": {"event": observability}}), flush=True)
        print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "job.completed", "job_id": job_id, "sequence": \(progressEventCount + 1), "payload": {"result": {"name": "movie", "resolution": "1920x1080", "frame_rate": "24/1", "interlaced": False, "size_bytes": 10, "titles": []}}}), flush=True)
        """)
        let job = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/tmp/movie.m2ts"), jobID: jobID)
        var sequences: [Int] = []
        var progressEvents = 0

        let result = try await client.run(job: job) { event in
            sequences.append(event.sequence)
            if event.type == .observability {
                progressEvents += 1
                try await Task.sleep(nanoseconds: 5_000_000)
            }
        }

        XCTAssertEqual(sequences, Array(0 ... (progressEventCount + 1)))
        XCTAssertEqual(progressEvents, progressEventCount)
        XCTAssertEqual(result.terminalEvent.type, .jobCompleted)
        XCTAssertEqual(result.exitStatus, 0)
    }

    func testCancellationWhileStdoutIsBackpressuredReapsWorker() async throws {
        let payloadBytes = 48 * 1_024
        let client = fixtureClient(body: """
        \(readyEvent())
        sys.stderr.write("WRITER-STARTED\\n")
        sys.stderr.flush()
        message = "x" * \(payloadBytes)
        for sequence in range(1, 10_000):
            print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "log", "job_id": job_id, "sequence": sequence, "payload": {"level": "info", "message": message}}), flush=True)
        sys.stderr.write("WRITER-COMPLETED\\n")
        sys.stderr.flush()
        """)
        let job = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/tmp/movie.m2ts"), jobID: jobID)
        let slowHandlerStarted = expectation(description: "slow handler started")
        let runFinished = expectation(description: "run finished")
        let gate = AsyncGate()
        let task = Task {
            defer {
                runFinished.fulfill()
            }
            return try await client.run(job: job) { event in
                if event.sequence == 1 {
                    slowHandlerStarted.fulfill()
                    await gate.wait()
                }
            }
        }

        await fulfillment(of: [slowHandlerStarted], timeout: 5)
        let writerStarted = await waitForDiagnosticMarker("WRITER-STARTED", client: client)
        XCTAssertTrue(writerStarted)
        guard writerStarted else {
            task.cancel()
            await gate.open()
            _ = await task.result
            return
        }
        try await Task.sleep(nanoseconds: 250_000_000)
        XCTAssertFalse(client.diagnosticSnapshot().toolOutput.text.contains("WRITER-COMPLETED"))
        task.cancel()
        await gate.open()
        await fulfillment(of: [runFinished], timeout: 5)

        switch await task.result {
        case .failure(let error):
            XCTAssertTrue(error is CancellationError, "Unexpected cancellation error: \(error)")
        case .success:
            XCTFail("Expected cancellation before a terminal event to fail the run")
        }
        XCTAssertFalse(client.diagnosticSnapshot().isRunning)
    }

    func testStreamsAndBoundsDiagnosticsWhileWorkerIsActive() async throws {
        let diagnosticPayloadBytes = 4 * 1_024 * 1_024
        let client = fixtureClient(body: """
        \(readyEvent())
        sys.stderr.buffer.write(b"FIRST-MARKER\\n")
        for _ in range(\(diagnosticPayloadBytes / (64 * 1_024))):
            sys.stderr.buffer.write(b"x" * (64 * 1_024))
        sys.stderr.buffer.write(b"TAIL-MARKER\\n")
        sys.stderr.flush()
        time.sleep(30)
        """)
        let job = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/tmp/movie.m2ts"), jobID: jobID)
        let ready = expectation(description: "worker ready")
        let task = Task {
            try await client.run(job: job) { event in
                if event.type == .workerReady {
                    ready.fulfill()
                }
            }
        }

        await fulfillment(of: [ready], timeout: 2)
        var snapshot = client.diagnosticSnapshot()
        let deadline = Date().addingTimeInterval(3)
        while !snapshot.toolOutput.text.contains("TAIL-MARKER"), Date() < deadline {
            try await Task.sleep(nanoseconds: 20_000_000)
            snapshot = client.diagnosticSnapshot()
        }
        guard snapshot.toolOutput.text.contains("TAIL-MARKER") else {
            client.cancel()
            _ = await task.result
            return XCTFail("Timed out after 3 s waiting for TAIL-MARKER in stderr tail")
        }

        XCTAssertTrue(snapshot.isRunning)
        XCTAssertTrue(snapshot.toolOutput.truncated)
        XCTAssertLessThanOrEqual(snapshot.toolOutput.retainedBytes, 512 * 1_024)
        XCTAssertEqual(
            snapshot.toolOutput.totalBytes,
            diagnosticPayloadBytes + Data("FIRST-MARKER\nTAIL-MARKER\n".utf8).count
        )
        XCTAssertEqual(
            snapshot.toolOutput.droppedBytes,
            snapshot.toolOutput.totalBytes - snapshot.toolOutput.retainedBytes
        )
        XCTAssertFalse(snapshot.toolOutput.text.contains("FIRST-MARKER"))
        XCTAssertTrue(snapshot.toolOutput.text.contains("TAIL-MARKER"))

        client.cancel()
        _ = await task.result
        XCTAssertTrue(client.diagnosticSnapshot().cancellationRequested)
    }

    func testReportsMissingTerminalEvent() async throws {
        let client = fixtureClient(body: readyEvent())
        let job = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/tmp/movie.m2ts"), jobID: jobID)

        do {
            _ = try await client.run(job: job) { _ in }
            XCTFail("Expected the missing terminal event to fail")
        } catch let error as WorkerClientError {
            guard case .missingTerminalEvent = error else {
                return XCTFail("Unexpected worker error: \(error)")
            }
        }
    }

    func testReportsMalformedEventStream() async throws {
        let client = fixtureClient(body: "print('not-json', flush=True); sys.exit(7)")
        let job = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/tmp/movie.m2ts"), jobID: jobID)

        do {
            _ = try await client.run(job: job) { _ in }
            XCTFail("Expected malformed JSON to fail")
        } catch let error as WorkerClientError {
            guard case let .protocolFailure(_, _, exitStatus) = error else {
                return XCTFail("Unexpected worker error: \(error)")
            }
            XCTAssertEqual(error.processExitStatus, exitStatus)
        }
    }

    func testRejectsTerminalEventBeforeWorkerReady() async throws {
        let client = fixtureClient(body: """
        print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "job.completed", "job_id": job_id, "sequence": 0, "payload": {"result": {"name": "movie", "resolution": "1920x1080", "frame_rate": "24/1", "interlaced": False, "size_bytes": 10, "titles": []}}}), flush=True)
        """)
        let job = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/tmp/movie.m2ts"), jobID: jobID)

        do {
            _ = try await client.run(job: job) { _ in }
            XCTFail("Expected the missing worker.ready event to fail")
        } catch let error as WorkerClientError {
            guard case .protocolFailure = error else {
                return XCTFail("Unexpected worker error: \(error)")
            }
        }
    }

    func testRejectsWorkerReadyWithoutOwnedProcessGroup() async throws {
        let client = fixtureClient(body: """
        print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "worker.ready", "job_id": job_id, "sequence": 0, "payload": {}}), flush=True)
        """)
        let job = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/tmp/movie.m2ts"), jobID: jobID)

        do {
            _ = try await client.run(job: job) { _ in }
            XCTFail("Expected the missing process group to fail")
        } catch let error as WorkerClientError {
            guard case .protocolFailure = error else {
                return XCTFail("Unexpected worker error: \(error)")
            }
        }
    }

    func testRejectsEventAfterTerminal() async throws {
        let client = fixtureClient(body: """
        \(readyEvent())
        print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "job.completed", "job_id": job_id, "sequence": 1, "payload": {"result": {"name": "movie", "resolution": "1920x1080", "frame_rate": "24/1", "interlaced": False, "size_bytes": 10, "titles": []}}}), flush=True)
        print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "log", "job_id": job_id, "sequence": 2, "payload": {"level": "info", "message": "late event"}}), flush=True)
        """)
        let job = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/tmp/movie.m2ts"), jobID: jobID)

        do {
            _ = try await client.run(job: job) { _ in }
            XCTFail("Expected an event after terminal to fail")
        } catch let error as WorkerClientError {
            guard case .protocolFailure = error else {
                return XCTFail("Unexpected worker error: \(error)")
            }
        }
    }

    func testCancellationReapsWorkerAfterTerminalEvent() async throws {
        let client = fixtureClient(body: """
        \(readyEvent())
        print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "job.completed", "job_id": job_id, "sequence": 1, "payload": {"result": {"name": "movie", "resolution": "1920x1080", "frame_rate": "24/1", "interlaced": False, "size_bytes": 10, "titles": []}}}), flush=True)
        time.sleep(30)
        """)
        let job = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/tmp/movie.m2ts"), jobID: jobID)
        let terminalReceived = expectation(description: "terminal event received")
        let terminalState = TerminalEventState()
        let task = Task {
            try await client.run(job: job) { event in
                if event.type.isTerminal {
                    await terminalState.record()
                    terminalReceived.fulfill()
                }
            }
        }

        await fulfillment(of: [terminalReceived], timeout: 10)
        guard await terminalState.wasReceived else {
            client.cancel()
            _ = await task.result
            return
        }
        client.cancel()
        let result = try await task.value

        XCTAssertEqual(result.terminalEvent.type, .jobCompleted)
        XCTAssertNotEqual(result.exitStatus, 0)
    }

    func testTaskCancellationAfterTerminalDeliveryReturnsCancellationError() async throws {
        let client = fixtureClient(body: """
        \(readyEvent())
        print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "job.completed", "job_id": job_id, "sequence": 1, "payload": {"result": {"name": "movie", "resolution": "1920x1080", "frame_rate": "24/1", "interlaced": False, "size_bytes": 10, "titles": []}}}), flush=True)
        """)
        let job = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/tmp/movie.m2ts"), jobID: jobID)
        let terminalHandlerStarted = expectation(description: "terminal handler started")
        let runFinished = expectation(description: "run finished")
        let gate = AsyncGate()
        let task = Task {
            defer {
                runFinished.fulfill()
            }
            return try await client.run(job: job) { event in
                if event.type.isTerminal {
                    terminalHandlerStarted.fulfill()
                    await gate.wait()
                }
            }
        }

        await fulfillment(of: [terminalHandlerStarted], timeout: 5)
        task.cancel()
        await gate.open()
        await fulfillment(of: [runFinished], timeout: 5)

        switch await task.result {
        case .failure(let error):
            XCTAssertTrue(error is CancellationError, "Unexpected cancellation error: \(error)")
        case .success:
            XCTFail("Expected task cancellation after terminal delivery to fail the run")
        }
        XCTAssertFalse(client.diagnosticSnapshot().isRunning)
    }

    func testCancellationRequestedBeforeLaunchIsNotLost() async throws {
        let client = fixtureClient(body: "time.sleep(30)")
        let job = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/tmp/movie.m2ts"), jobID: jobID)
        client.cancel()
        let startedAt = Date()

        do {
            _ = try await client.run(job: job) { _ in }
            XCTFail("Expected the pre-launch cancellation to stop the worker")
        } catch {
            XCTAssertLessThan(Date().timeIntervalSince(startedAt), 5)
        }
    }

    private func readyEvent() -> String {
        """
        print(json.dumps({"protocol_version": \(WorkerJobSpec.protocolVersion), "type": "worker.ready", "job_id": job_id, "sequence": 0, "payload": {"worker_version": "test", "process_group_id": os.getpid()}}), flush=True)
        """
    }

    private func waitForDiagnosticMarker(
        _ marker: String,
        client: WorkerProcessClient,
        timeout: TimeInterval = 2
    ) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if client.diagnosticSnapshot().toolOutput.text.contains(marker) {
                return true
            }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        return false
    }

    private func fixtureClient(body: String) -> WorkerProcessClient {
        let script = """
        import json
        import os
        import sys
        import time

        if os.getpgrp() != os.getpid():
            os.setsid()
        request = json.loads(sys.stdin.read())
        job_id = request["job_id"]
        \(body)
        """
        return WorkerProcessClient(
            configuration: WorkerLaunchConfiguration(
                executableURL: URL(
                    fileURLWithPath: ProcessInfo.processInfo.environment["BD_TO_AVP_TEST_PYTHON"]
                        ?? "/usr/bin/python3"
                ),
                arguments: ["-c", script],
                currentDirectoryURL: nil,
                environment: ProcessInfo.processInfo.environment
            )
        )
    }
}

private actor TerminalEventState {
    private(set) var wasReceived = false

    func record() {
        wasReceived = true
    }
}

private actor AsyncGate {
    private var isOpen = false
    private var continuations: [CheckedContinuation<Void, Never>] = []

    func wait() async {
        guard !isOpen else {
            return
        }
        await withCheckedContinuation { continuation in
            if isOpen {
                continuation.resume()
            } else {
                continuations.append(continuation)
            }
        }
    }

    func open() {
        guard !isOpen else {
            return
        }
        isOpen = true
        let waitingContinuations = continuations
        continuations.removeAll()
        for continuation in waitingContinuations {
            continuation.resume()
        }
    }
}

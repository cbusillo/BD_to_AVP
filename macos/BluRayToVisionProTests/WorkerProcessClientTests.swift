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
        let client = fixtureClient(body: "print('not-json', flush=True)")
        let job = WorkerJobSpec(sourceURL: URL(fileURLWithPath: "/tmp/movie.m2ts"), jobID: jobID)

        do {
            _ = try await client.run(job: job) { _ in }
            XCTFail("Expected malformed JSON to fail")
        } catch let error as WorkerClientError {
            guard case .protocolFailure = error else {
                return XCTFail("Unexpected worker error: \(error)")
            }
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

import Foundation
import XCTest
@testable import BluRayToVisionPro

final class StallRecoveryViewModelTests: XCTestCase {
    @MainActor
    func testVisibleStallWaitsForAcknowledgementAndStopClearsIt() async throws {
        let ready = expectation(description: "worker stalled")
        let submitted = expectation(description: "control submitted")
        let worker = StallWorkerFixture(ready: ready, submitted: submitted)
        let model = ConversionViewModel { worker }
        let source = try makeSource()
        defer { try? FileManager.default.removeItem(at: source.deletingLastPathComponent()) }
        model.selectSource(source)
        await fulfillment(of: [ready], timeout: 3)
        let notice = try XCTUnwrap(model.stallRecovery.notices.first)
        model.keepWaiting(toolRunID: notice.id, episodeID: notice.stall.stallEpisodeID)
        await fulfillment(of: [submitted], timeout: 3)
        XCTAssertNotNil(model.stallRecovery.notices.first?.pendingCommand)
        XCTAssertNil(model.stallRecovery.notices.first?.message)
        try await worker.acknowledge()
        XCTAssertNil(model.stallRecovery.notices.first?.pendingCommand)
        XCTAssertEqual(
            model.stallRecovery.notices.first?.message,
            "Extra time added. Waiting for video output to resume."
        )
        model.stopActiveWorker()
        XCTAssertTrue(model.stallRecovery.notices.isEmpty)
        await model.stopForQuit()
    }

    @MainActor
    func testControlWriteFailureLeavesAnHonestVisibleMessage() async throws {
        let ready = expectation(description: "worker stalled")
        let submitted = expectation(description: "failed control submitted")
        let worker = StallWorkerFixture(ready: ready, submitted: submitted, failWrites: true)
        let model = ConversionViewModel { worker }
        let source = try makeSource()
        defer { try? FileManager.default.removeItem(at: source.deletingLastPathComponent()) }
        model.selectSource(source)
        await fulfillment(of: [ready], timeout: 3)
        let notice = try XCTUnwrap(model.stallRecovery.notices.first)
        model.keepWaiting(toolRunID: notice.id, episodeID: notice.stall.stallEpisodeID)
        await fulfillment(of: [submitted], timeout: 3)
        for _ in 0..<100 {
            if model.stallRecovery.notices.first?.pendingCommand == nil { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertEqual(
            model.stallRecovery.notices.first?.message,
            "The request could not be sent. Extra time has not been confirmed."
        )
        await model.stopForQuit()
        XCTAssertTrue(model.stallRecovery.notices.isEmpty)
    }

    private func makeSource() throws -> URL {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let source = directory.appendingPathComponent("stall.mkv")
        try Data([0]).write(to: source)
        return source
    }
}

private final class StallWorkerFixture: WorkerProcessRunning, @unchecked Sendable {
    private let ready: XCTestExpectation
    private let submitted: XCTestExpectation
    private let failWrites: Bool
    private let lock = NSLock()
    private var handler: ((WorkerEvent) async throws -> Void)?
    private var command: WorkerWaitCommand?
    private var continuation: CheckedContinuation<Void, Never>?
    private var cancelled = false
    private var sequence = 2

    init(ready: XCTestExpectation, submitted: XCTestExpectation, failWrites: Bool = false) {
        self.ready = ready
        self.submitted = submitted
        self.failWrites = failWrites
    }

    func run(job: WorkerJobSpec, onEvent: @escaping (WorkerEvent) async throws -> Void) async throws -> WorkerRunResult {
        lock.withLock { handler = onEvent }
        try await onEvent(WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion, type: .workerReady, jobID: job.jobID, sequence: 0,
            payload: WorkerEventPayload(controlCapabilities: ["keep_waiting_v1"])
        ))
        try await onEvent(WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion, type: .toolStall, jobID: job.jobID, sequence: 1,
            payload: WorkerEventPayload(stall: WorkerStallEvent(
                toolRunID: UUID(), stallEpisodeID: UUID(), tool: "mv_hevc_encoder", state: .stalled,
                canExtend: true, grantsUsed: 0, maxGrants: 2, grantSeconds: 120,
                artifacts: [], artifactsOmitted: 0, toolProgress: nil
            ))
        ))
        ready.fulfill()
        await withCheckedContinuation { pending in
            let alreadyCancelled = lock.withLock {
                if cancelled { return true }
                continuation = pending
                return false
            }
            if alreadyCancelled { pending.resume() }
        }
        let terminal = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion, type: .jobCancelled,
            jobID: job.jobID, sequence: lock.withLock { sequence }, payload: WorkerEventPayload()
        )
        try await onEvent(terminal)
        return WorkerRunResult(terminalEvent: terminal, exitStatus: 0, diagnostics: "")
    }

    func sendControl(_ command: WorkerWaitCommand) async throws {
        lock.withLock { self.command = command }
        submitted.fulfill()
        if failWrites { throw WorkerControlError.unavailable }
    }

    func acknowledge() async throws {
        let captured = lock.withLock {
            let current = sequence
            sequence += 1
            return (command, handler, current)
        }
        let command = try XCTUnwrap(captured.0)
        let handler = try XCTUnwrap(captured.1)
        try await handler(WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion, type: .controlResult,
            jobID: command.jobID, sequence: captured.2,
            payload: WorkerEventPayload(controlResult: WorkerControlResult(
                commandID: command.commandID, toolRunID: command.toolRunID, stallEpisodeID: command.stallEpisodeID,
                accepted: true, code: "extended", duplicate: false, grantsUsed: 1, grantSeconds: 120
            ))
        ))
    }

    func cancel() {
        let pending = lock.withLock {
            cancelled = true
            let pending = continuation
            continuation = nil
            return pending
        }
        pending?.resume()
    }
}

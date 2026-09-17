import Foundation
import XCTest
@testable import BluRayToVisionPro

final class StallRecoveryStateTests: XCTestCase {
    private let jobID = UUID()
    private let toolRunID = UUID()
    private let episodeID = UUID()

    func testWaitRemainsPendingUntilMatchingWorkerAcknowledgement() throws {
        var state = readyState()
        state.receive(event(.toolStall, stall: stall()))
        let command = try XCTUnwrap(state.requestWait(toolRunID: toolRunID, episodeID: episodeID))
        XCTAssertNil(state.notices[0].message)
        XCTAssertNotNil(state.notices[0].pendingCommand)
        XCTAssertNil(state.requestWait(toolRunID: toolRunID, episodeID: episodeID))
        state.receive(event(.toolStall, stall: stall(state: .extended, grantsUsed: 1)))
        XCTAssertNotNil(state.notices[0].pendingCommand)
        state.receive(event(.controlResult, result: result(command)))
        XCTAssertNil(state.notices[0].pendingCommand)
        XCTAssertEqual(state.notices[0].message, "Extra time added. Waiting for video output to resume.")
    }

    func testNoControlWithoutAdvertisedCapability() {
        var state = StallRecoveryState()
        state.begin(jobID: jobID)
        state.receive(event(.workerReady))
        state.receive(event(.toolStall, stall: stall()))
        XCTAssertEqual(state.notices.count, 1)
        XCTAssertNil(state.requestWait(toolRunID: toolRunID, episodeID: episodeID))
    }

    func testOldEpisodeAcknowledgementDoesNotClearNewRequest() throws {
        var state = readyState()
        state.receive(event(.toolStall, stall: stall()))
        let old = try XCTUnwrap(state.requestWait(toolRunID: toolRunID, episodeID: episodeID))
        state.receive(event(.toolStall, stall: stall(state: .recovered)))
        let newEpisode = UUID()
        state.receive(event(.toolStall, stall: stall(episodeID: newEpisode)))
        let current = try XCTUnwrap(state.requestWait(toolRunID: toolRunID, episodeID: newEpisode))
        state.receive(event(.controlResult, result: result(old)))
        XCTAssertEqual(state.notices[0].pendingCommand, current)
        XCTAssertNil(state.notices[0].message)
        state.writeFailed(old)
        XCTAssertEqual(state.notices[0].pendingCommand, current)
    }

    func testWrongJobAndWrongRunCannotAcknowledgeWaiting() throws {
        var state = readyState()
        state.receive(event(.toolStall, stall: stall()))
        let command = try XCTUnwrap(state.requestWait(toolRunID: toolRunID, episodeID: episodeID))
        state.receive(event(.controlResult, jobID: UUID(), result: result(command)))
        let wrongRun = WorkerWaitCommand(
            commandID: command.commandID, jobID: jobID, toolRunID: UUID(), stallEpisodeID: episodeID
        )
        state.receive(event(.controlResult, result: result(wrongRun)))
        XCTAssertEqual(state.notices[0].pendingCommand, command)
    }

    func testRecoveryTerminalAndStageTransitionClearChoices() throws {
        for clearingEvent in [
            event(.toolStall, stall: stall(state: .recovered)),
            event(.toolStall, stall: stall(state: .ended)),
            event(.toolStall, stall: stall(state: .timedOut)),
            event(.stageStarted),
            event(.jobCancelled),
        ] {
            var state = readyState()
            state.receive(event(.toolStall, stall: stall()))
            let command = try XCTUnwrap(state.requestWait(toolRunID: toolRunID, episodeID: episodeID))
            state.receive(clearingEvent)
            state.receive(event(.controlResult, result: result(command)))
            XCTAssertTrue(state.notices.isEmpty)
        }
    }

    func testWriteFailureAndWorkerRejectionDoNotClaimExtraTime() throws {
        var state = readyState()
        state.receive(event(.toolStall, stall: stall()))
        let command = try XCTUnwrap(state.requestWait(toolRunID: toolRunID, episodeID: episodeID))
        state.writeFailed(command)
        XCTAssertNil(state.notices[0].pendingCommand)
        XCTAssertEqual(state.notices[0].message, "The request could not be sent. Extra time has not been confirmed.")
        let second = try XCTUnwrap(state.requestWait(toolRunID: toolRunID, episodeID: episodeID))
        state.receive(event(.controlResult, result: result(second, accepted: false)))
        XCTAssertTrue(try XCTUnwrap(state.notices[0].message).contains("was not added"))
    }

    func testEndedSiblingDoesNotClearAnotherStallOrItsPendingChoice() throws {
        var state = readyState()
        let otherTool = UUID()
        state.receive(event(.toolStall, stall: stall()))
        let command = try XCTUnwrap(state.requestWait(toolRunID: toolRunID, episodeID: episodeID))
        state.receive(event(.toolStall, stall: stall(toolRunID: otherTool)))
        state.receive(event(.toolStall, stall: stall(toolRunID: otherTool, state: .recovered)))
        XCTAssertEqual(state.notices.count, 1)
        XCTAssertEqual(state.notices[0].pendingCommand, command)
    }

    func testMaximumGrantNoticeCannotOfferAnotherExtension() {
        var state = readyState()
        state.receive(event(.toolStall, stall: stall(state: .extended, grantsUsed: 2, canExtend: false)))
        XCTAssertNil(state.requestWait(toolRunID: toolRunID, episodeID: episodeID))
        XCTAssertFalse(state.notices[0].canRequestWait)
    }

    func testSharedStallFixtureRetainsUsefulProgressAndArtifactEvidence() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("tests/fixtures/native_worker_stall_v13.json"))
        let decoded = try JSONDecoder().decode(WorkerEvent.self, from: data)
        XCTAssertEqual(decoded.protocolVersion, WorkerJobSpec.protocolVersion)
        XCTAssertEqual(decoded.type, .toolStall)
        XCTAssertNil(decoded.payload.progress)
        let report = try XCTUnwrap(decoded.payload.stall)
        XCTAssertEqual(report.artifacts[0].sizeBytes, 4_831_838_208)
        XCTAssertEqual(report.toolProgress?["repeated_updates"], 3)
        XCTAssertEqual(report.toolProgress?["completed_units"], 129_479)
        let details = try XCTUnwrap(report.diagnosticDetails)
        XCTAssertTrue(details.contains("no_progress_age_seconds"))
        XCTAssertTrue(details.contains("stall_episode_id"))
        XCTAssertFalse(details.contains("/Users/"))
    }

    func testDiagnosticRecorderPreservesStallAndControlDecisions() throws {
        let sourceURL = URL(fileURLWithPath: "/tmp/private-movie.mkv")
        var lifecycle = WorkerLifecycleState()
        lifecycle.selectSource(sourceURL)
        try lifecycle.begin(jobID: jobID, operationKind: .conversion)
        let recorder = DiagnosticSessionRecorder()
        recorder.beginJob(
            context: DiagnosticJobContext(
                jobID: jobID, source: ConversionSource(kind: .matroska, url: sourceURL)
            ),
            lifecycle: lifecycle, activeMode: "single_conversion", recordedAt: Date()
        )
        let command = WorkerWaitCommand(
            commandID: UUID(), jobID: jobID, toolRunID: toolRunID, stallEpisodeID: episodeID
        )
        for incoming in [event(.toolStall, stall: stall()), event(.controlResult, result: result(command))] {
            recorder.record(event: incoming, lifecycle: lifecycle, activeMode: "single_conversion", recordedAt: Date())
        }
        let snapshot = recorder.snapshot(
            capturedAt: Date(), lifecycle: lifecycle, activeMode: nil, batchSummary: nil, process: .empty
        )
        let entries = snapshot.events.entries
        let stallEntry = try XCTUnwrap(entries.first { $0.name == "tool.stall" })
        let controlEntry = try XCTUnwrap(entries.first { $0.name == "control.result" })
        XCTAssertEqual(stallEntry.tool, "mv_hevc_encoder")
        XCTAssertTrue(try XCTUnwrap(stallEntry.details).contains("stall_episode_id"))
        XCTAssertTrue(try XCTUnwrap(controlEntry.details).contains(command.commandID.uuidString))
        XCTAssertFalse(try XCTUnwrap(stallEntry.details).contains("private-movie"))
        XCTAssertFalse(try XCTUnwrap(controlEntry.details).contains("private-movie"))
    }

    private func readyState() -> StallRecoveryState {
        var state = StallRecoveryState()
        state.begin(jobID: jobID)
        state.receive(WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion, type: .workerReady, jobID: jobID, sequence: 0,
            payload: WorkerEventPayload(controlCapabilities: ["keep_waiting_v1"])
        ))
        return state
    }

    private func stall(
        toolRunID: UUID? = nil, episodeID: UUID? = nil, state: WorkerStallEvent.State = .stalled,
        grantsUsed: Int = 0, canExtend: Bool = true
    ) -> WorkerStallEvent {
        WorkerStallEvent(
            toolRunID: toolRunID ?? self.toolRunID, stallEpisodeID: episodeID ?? self.episodeID,
            tool: "mv_hevc_encoder", state: state, canExtend: canExtend, grantsUsed: grantsUsed,
            maxGrants: 2, grantSeconds: 120, artifacts: [], artifactsOmitted: 0, toolProgress: nil
        )
    }

    private func result(_ command: WorkerWaitCommand, accepted: Bool = true) -> WorkerControlResult {
        WorkerControlResult(
            commandID: command.commandID, toolRunID: command.toolRunID, stallEpisodeID: command.stallEpisodeID,
            accepted: accepted, code: accepted ? "extended" : "deadline_elapsed", duplicate: false,
            grantsUsed: accepted ? 1 : nil, grantSeconds: accepted ? 120 : nil
        )
    }

    private func event(
        _ type: WorkerEventType, jobID: UUID? = nil,
        stall: WorkerStallEvent? = nil, result: WorkerControlResult? = nil
    ) -> WorkerEvent {
        WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion, type: type,
            jobID: jobID ?? self.jobID, sequence: 1,
            payload: WorkerEventPayload(stall: stall, controlResult: result)
        )
    }
}

import Foundation
import XCTest
@testable import BluRayToVisionPro

final class WorkerLifecycleTests: XCTestCase {
    private let sourceURL = URL(fileURLWithPath: "/tmp/movie.m2ts")
    private let jobID = UUID(uuidString: "4B92D6F7-6E78-4FA0-AE1A-0D4E0841F38A")!

    func testCompleteLifecycleTransitionsThroughStructuredEvents() throws {
        var state = WorkerLifecycleState()
        state.selectSource(sourceURL)
        XCTAssertEqual(state.phase, .ready)

        try state.begin(jobID: jobID)
        try state.receive(event(.workerReady, sequence: 0, payload: .init(workerVersion: "0.2.143")))
        try state.receive(event(.jobStarted, sequence: 1, payload: .init(operation: "inspect_source")))
        try state.receive(
            event(
                .stageStarted,
                sequence: 2,
                payload: .init(stage: "inspect_source", message: "Reading video metadata")
            )
        )
        try state.receive(event(.heartbeat, sequence: 3, payload: .init(elapsedSeconds: 2)))

        XCTAssertEqual(state.phase, .processing)
        XCTAssertEqual(state.stageMessage, "Reading video metadata")
        XCTAssertEqual(state.elapsedSeconds, 2)

        let result = SourceInspection(
            name: "movie",
            resolution: "1920x1080",
            frameRate: "24000/1001",
            interlaced: false,
            sizeBytes: 42
        )
        try state.receive(event(.jobCompleted, sequence: 4, payload: .init(result: result)))

        XCTAssertEqual(state.phase, .completed)
        XCTAssertEqual(state.result, result)
    }

    func testCancellationTransitionsThroughStoppingAndCancelled() throws {
        var state = WorkerLifecycleState()
        state.selectSource(sourceURL)
        try state.begin(jobID: jobID)
        try state.receive(event(.workerReady, sequence: 0))

        state.requestStop()
        XCTAssertEqual(state.phase, .stopping)

        try state.receive(event(.jobCancelled, sequence: 1, payload: .init(message: "Source inspection cancelled.")))
        XCTAssertEqual(state.phase, .cancelled)
    }

    func testFailureEventPreservesRecoveryDetails() throws {
        var state = WorkerLifecycleState()
        state.selectSource(sourceURL)
        try state.begin(jobID: jobID)

        let failure = WorkerFailure(code: "probe_failed", message: "Could not inspect source.", details: "bad stream", retryable: false)
        try state.receive(event(.jobFailed, sequence: 0, payload: .init(error: failure)))

        XCTAssertEqual(state.phase, .failed)
        XCTAssertEqual(state.failureMessage, "Could not inspect source.")
        XCTAssertEqual(state.failureDetails, "bad stream")
        XCTAssertFalse(state.failureRetryable)
    }

    func testRetryableFailureIsExposedToTheInterface() throws {
        var state = WorkerLifecycleState()
        state.selectSource(sourceURL)
        try state.begin(jobID: jobID)

        let failure = WorkerFailure(code: "temporary", message: "Try again.", details: nil, retryable: true)
        try state.receive(event(.jobFailed, sequence: 0, payload: .init(error: failure)))

        XCTAssertTrue(state.failureRetryable)
    }

    func testConversionBeginSetsConversionStageMessage() throws {
        var state = WorkerLifecycleState()
        state.selectSource(sourceURL)

        try state.begin(jobID: jobID, operationKind: .conversion)

        XCTAssertEqual(state.phase, .inspecting)
        XCTAssertEqual(state.stageMessage, "Preparing conversion")
        XCTAssertEqual(state.operationKind, .conversion)
    }

    func testConversionJobCompletedStoresConversionResult() throws {
        var state = WorkerLifecycleState()
        state.selectSource(sourceURL)
        try state.begin(jobID: jobID, operationKind: .conversion)
        try state.receive(event(.workerReady, sequence: 0, payload: .init(workerVersion: "1.0")))

        let convResult = ConversionResult(outputPath: "/Movies/movie_AVP.mov", durationSeconds: 7200)
        try state.receive(event(.jobCompleted, sequence: 1, payload: .init(conversionResult: convResult)))

        XCTAssertEqual(state.phase, .completed)
        XCTAssertEqual(state.conversionResult, convResult)
        XCTAssertNil(state.result)
        XCTAssertEqual(state.stageMessage, "Conversion complete")
    }

    func testConversionCompleteStopUsesConversionMessage() throws {
        var state = WorkerLifecycleState()
        state.selectSource(sourceURL)
        try state.begin(jobID: jobID, operationKind: .conversion)
        try state.receive(event(.workerReady, sequence: 0))

        state.requestStop()
        state.completeStop()

        XCTAssertEqual(state.phase, .cancelled)
        XCTAssertEqual(state.activityMessage, "Conversion stopped.")
    }

    func testInspectionCompleteStopUsesInspectionMessage() throws {
        var state = WorkerLifecycleState()
        state.selectSource(sourceURL)
        try state.begin(jobID: jobID)
        try state.receive(event(.workerReady, sequence: 0))

        state.requestStop()
        state.completeStop()

        XCTAssertEqual(state.phase, .cancelled)
        XCTAssertEqual(state.activityMessage, "Inspection stopped.")
    }

    func testInspectionJobCompletedRejectsConversionResultPayload() throws {
        var state = WorkerLifecycleState()
        state.selectSource(sourceURL)
        try state.begin(jobID: jobID)
        try state.receive(event(.workerReady, sequence: 0))

        let convResult = ConversionResult(outputPath: "/out.mov", durationSeconds: nil)
        XCTAssertThrowsError(
            try state.receive(event(.jobCompleted, sequence: 1, payload: .init(conversionResult: convResult)))
        ) { error in
            XCTAssertEqual(error as? WorkerLifecycleError, .missingPayload(event: .jobCompleted))
        }
    }

    func testConversionJobCompletedRejectsInspectionResultPayload() throws {
        var state = WorkerLifecycleState()
        state.selectSource(sourceURL)
        try state.begin(jobID: jobID, operationKind: .conversion)
        try state.receive(event(.workerReady, sequence: 0))

        let inspResult = SourceInspection(name: "m", resolution: "1920x1080", frameRate: "24/1", interlaced: false, sizeBytes: 10)
        XCTAssertThrowsError(
            try state.receive(event(.jobCompleted, sequence: 1, payload: .init(result: inspResult)))
        ) { error in
            XCTAssertEqual(error as? WorkerLifecycleError, .missingPayload(event: .jobCompleted))
        }
    }

    func testRejectsSequenceGap() throws {
        var state = WorkerLifecycleState()
        state.selectSource(sourceURL)
        try state.begin(jobID: jobID)

        XCTAssertThrowsError(try state.receive(event(.jobStarted, sequence: 2))) { error in
            XCTAssertEqual(
                error as? WorkerLifecycleError,
                .unexpectedSequence(expected: 0, received: 2)
            )
        }
    }

    private func event(
        _ type: WorkerEventType,
        sequence: Int,
        payload: WorkerEventPayload = WorkerEventPayload()
    ) -> WorkerEvent {
        WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: type,
            jobID: jobID,
            sequence: sequence,
            payload: payload
        )
    }
}

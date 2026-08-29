import XCTest
@testable import BluRayToVisionPro

final class PersistentQueueCommandStateTests: XCTestCase {
    func testEmptyQueueDisablesQueueOperationsAndUsesStartTitle() {
        let state = makeState()

        XCTAssertEqual(state.startTitle, "Start Queue")
        XCTAssertFalse(state.canStart)
        XCTAssertFalse(state.canPauseAfterCurrent)
        XCTAssertFalse(state.canStopCurrent)
        XCTAssertFalse(state.canMoveUp)
        XCTAssertFalse(state.canMoveDown)
        XCTAssertFalse(state.canConvertNext)
        XCTAssertFalse(state.canRemoveSelectedItem)
        XCTAssertFalse(state.canUndo)
    }

    func testWaitingItemsDriveMoveAndConvertNextAvailability() {
        let first = makeItem(ordinal: 0, status: .waiting)
        let second = makeItem(ordinal: 1, status: .waiting)

        let firstState = makeState(items: [first, second], selectedItemID: first.id)
        XCTAssertTrue(firstState.canStart)
        XCTAssertFalse(firstState.canMoveUp)
        XCTAssertTrue(firstState.canMoveDown)
        XCTAssertFalse(firstState.canConvertNext)

        let secondState = makeState(items: [first, second], selectedItemID: second.id)
        XCTAssertTrue(secondState.canMoveUp)
        XCTAssertFalse(secondState.canMoveDown)
        XCTAssertTrue(secondState.canConvertNext)
    }

    func testRunStatesAndWorkersDriveTitlesAndAvailability() {
        let item = makeItem(ordinal: 0, status: .waiting)

        let paused = makeState(items: [item], runState: .paused)
        XCTAssertEqual(paused.startTitle, "Resume Queue")
        XCTAssertTrue(paused.canStart)
        XCTAssertFalse(paused.canPauseAfterCurrent)

        let running = makeState(items: [item], runState: .running, hasActiveWorker: true)
        XCTAssertFalse(running.canStart)
        XCTAssertTrue(running.canPauseAfterCurrent)
        XCTAssertTrue(running.canStopCurrent)

        let pauseAfterCurrent = makeState(
            items: [item],
            runState: .pauseAfterCurrent,
            hasActiveWorker: true
        )
        XCTAssertFalse(pauseAfterCurrent.canStart)
        XCTAssertFalse(pauseAfterCurrent.canPauseAfterCurrent)
        XCTAssertTrue(pauseAfterCurrent.canStopCurrent)

        let stopping = makeState(items: [makeItem(ordinal: 0, status: .stopping)], runState: .running, hasActiveWorker: true)
        XCTAssertFalse(stopping.canStart)
        XCTAssertTrue(stopping.canStopCurrent)

        let previewActive = makeState(items: [item], hasPreviewWorker: true)
        XCTAssertFalse(previewActive.canStart)
    }

    func testTerminalAndUnresolvedItemsHaveExpectedAvailability() throws {
        let completed = makeItem(
            ordinal: 0,
            status: .completed(DurableQueueResult(outputPath: "/tmp/output.mp4"))
        )
        let stopped = makeItem(ordinal: 1, status: .stopped)
        let unresolved = makeItem(ordinal: 2, status: .needsChoice(try makeConflict()))

        let completedState = makeState(items: [completed], selectedItemID: completed.id)
        XCTAssertFalse(completedState.canStart)
        XCTAssertFalse(completedState.canRemoveSelectedItem)
        XCTAssertNotNil(completedState.selectedItemLockReason)

        let stoppedState = makeState(items: [stopped], selectedItemID: stopped.id)
        XCTAssertTrue(stoppedState.canStart)
        XCTAssertTrue(stoppedState.canRemoveSelectedItem)

        let unresolvedState = makeState(items: [unresolved], selectedItemID: unresolved.id)
        XCTAssertFalse(unresolvedState.canStart)
        XCTAssertTrue(unresolvedState.canRemoveSelectedItem)
        XCTAssertNotNil(unresolvedState.selectedItemLockReason)
    }

    func testOffPeakScheduleAndInsertedDiscAreReflectedInState() {
        let disc = ConversionSource(
            kind: .physicalDisc,
            url: URL(fileURLWithPath: "/Volumes/Disc"),
            displayName: "Inserted Disc",
            workerSourcePath: "/Volumes/Disc"
        )
        let schedule = OffPeakQueueSchedule(
            startAt: Date(timeIntervalSince1970: 1_000),
            endAt: Date(timeIntervalSince1970: 2_000)
        )
        let state = makeState(
            items: [makeItem(ordinal: 0, status: .waiting)],
            offPeakSchedule: schedule,
            insertedDiscs: [disc]
        )

        XCTAssertEqual(state.startTitle, "Start Now")
        XCTAssertEqual(state.offPeakSchedule, schedule)
        XCTAssertEqual(state.insertedDiscs, [disc])
    }

    func testUndoOnlyEnablesForValidRemovalTokenRevision() {
        XCTAssertTrue(makeState(removalTokenIsValid: true).canUndo)
        XCTAssertFalse(makeState(removalTokenIsValid: false).canUndo)
    }

    private func makeState(
        items: [PersistentQueueItem] = [],
        selectedItemID: UUID? = nil,
        runState: PersistentQueueRunState = .idle,
        hasActiveWorker: Bool = false,
        hasPreviewWorker: Bool = false,
        offPeakSchedule: OffPeakQueueSchedule? = nil,
        insertedDiscs: [ConversionSource] = [],
        removalTokenIsValid: Bool = false
    ) -> PersistentQueueCommandState {
        PersistentQueueCommandState(
            items: items,
            selectedItemID: selectedItemID,
            runState: runState,
            hasActiveWorker: hasActiveWorker,
            hasPreviewWorker: hasPreviewWorker,
            offPeakSchedule: offPeakSchedule,
            insertedDiscs: insertedDiscs,
            removalTokenIsValid: removalTokenIsValid
        )
    }

    private func makeItem(
        ordinal: Int,
        status: PersistentQueueItemStatus
    ) -> PersistentQueueItem {
        let sourceURL = URL(fileURLWithPath: "/tmp/queue-\(ordinal).mkv")
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: sourceURL),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/Output"),
            options: ConversionOptions()
        )
        return PersistentQueueItem(ordinal: ordinal, draft: draft, status: status)
    }

    private func makeConflict() throws -> RouteQualityConflict {
        var options = ConversionOptions()
        try options.encoding.selectQualityStep(.maximumDetail)
        guard case let .conflict(conflict) = RouteQualityEngine.propose(
            options: options,
            edit: .reusableIntermediates(true)
        ) else {
            throw TestError.missingConflict
        }
        return conflict
    }

    private enum TestError: Error {
        case missingConflict
    }
}

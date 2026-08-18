import XCTest
@testable import BluRayToVisionPro

@MainActor
final class SetupQueueAdmissionTests: XCTestCase {
    func testResolvedSnapshotsCanStartAndRemainFrozenAfterLaterEdits() throws {
        let draft = makeDraft()
        let admission = SetupQueueAdmission()
        admission.add(drafts: [draft])

        XCTAssertTrue(admission.canStart)
        let snapshot = try XCTUnwrap(admission.startableDrafts.first)
        var changed = draft.options
        changed.encoding.audioBitrate = 512
        XCTAssertNotEqual(snapshot.options, changed)
    }

    func testHeldItemGatesStartUntilExactResolutionIsApplied() throws {
        let draft = makeDraft()
        var proposed = draft.options
        proposed.job.intermediatePolicy = .reusable
        let conflict = try conflict(for: draft.options)
        let admission = SetupQueueAdmission()
        admission.add(drafts: [draft], conflicts: [conflict])

        XCTAssertFalse(admission.canStart)
        let group = try XCTUnwrap(admission.groups.first)
        let resolution = try XCTUnwrap(group.conflict.resolutions.first(where: \.isAvailable))
        var selection = QueueResolutionSelection()
        selection.resolutionID = resolution.id
        XCTAssertNoThrow(try admission.apply(group: group, selection: selection).get())
        XCTAssertTrue(admission.canStart)
        XCTAssertEqual(
            admission.items.first?.resolutionTrace?.qualityOutcome,
            "\(try XCTUnwrap(admission.items.first?.currentDraft).options.videoRoutePlan.qualityTitle) quality"
        )
    }

    func testOneScopeNeverMutatesAnotherWaitingItemOrRunningSnapshot() throws {
        let draft = makeDraft()
        let conflict = try conflict(for: draft.options)
        let admission = SetupQueueAdmission()
        admission.add(drafts: [draft, draft], conflicts: [conflict, conflict])
        let group = try XCTUnwrap(admission.groups.first)
        var selection = QueueResolutionSelection()
        selection.resolutionID = try XCTUnwrap(group.conflict.resolutions.first(where: \.isAvailable)?.id)
        selection.scope = .item(group.candidates[0].id)
        XCTAssertNoThrow(try admission.apply(group: group, selection: selection).get())
        admission.markRunning(group.candidates[0].id)
        selection.scope = .item(group.candidates[1].id)

        XCTAssertNoThrow(try admission.apply(group: group, selection: selection).get())
        XCTAssertEqual(admission.items[0].state, .running)
        XCTAssertNotNil(admission.items[0].currentDraft)
        XCTAssertEqual(admission.items[1].state, .waiting)
    }

    func testChangeRestoresHeldConflictAndClearsTrace() throws {
        let draft = makeDraft()
        let conflict = try conflict(for: draft.options)
        let admission = SetupQueueAdmission()
        admission.add(drafts: [draft], conflicts: [conflict])
        let group = try XCTUnwrap(admission.groups.first)
        var selection = QueueResolutionSelection()
        selection.resolutionID = try XCTUnwrap(group.conflict.resolutions.first(where: \.isAvailable)?.id)
        XCTAssertNoThrow(try admission.apply(group: group, selection: selection).get())
        let itemID = try XCTUnwrap(admission.items.first?.id)

        admission.changeResolution(itemID)

        XCTAssertFalse(admission.canStart)
        XCTAssertNil(admission.items.first?.currentDraft)
        XCTAssertNil(admission.items.first?.resolutionTrace)
        XCTAssertEqual(admission.groups.first?.candidates.first?.id, itemID)
    }

    func testDurableAndRuntimeProjectionPreserveIdentityTraceAndState() throws {
        let draft = makeDraft()
        let conflict = try conflict(for: draft.options)
        let admission = SetupQueueAdmission()
        admission.add(drafts: [draft, draft], conflicts: [conflict, conflict])
        let group = try XCTUnwrap(admission.groups.first)
        var selection = QueueResolutionSelection()
        selection.resolutionID = try XCTUnwrap(group.conflict.resolutions.first(where: \.isAvailable)?.id)
        XCTAssertNoThrow(try admission.apply(group: group, selection: selection).get())

        var durable = try XCTUnwrap(admission.durableItems().first)
        XCTAssertEqual(durable.id, admission.items[0].id)
        XCTAssertEqual(durable.resolutionTrace, admission.items[0].resolutionTrace)
        durable.state = .processing
        let projected = try PersistentQueueItem(item: durable)
        admission.synchronize(with: [projected])

        XCTAssertEqual(admission.items[0].state, .running)
        XCTAssertEqual(admission.items[0].resolutionTrace, durable.resolutionTrace)
    }

    private func makeDraft() -> ConversionDraft {
        let source = ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/tmp/Movie.mkv"))
        return ConversionDraft(
            source: source,
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp"),
            options: ConversionOptions()
        )
    }

    private func conflict(for options: ConversionOptions) throws -> RouteQualityConflict {
        var maximumDetail = options
        try maximumDetail.encoding.selectQualityStep(.maximumDetail)
        guard case let .conflict(conflict) = RouteQualityEngine.propose(
            options: maximumDetail,
            edit: .reusableIntermediates(true)
        ) else {
            throw TestError.missingConflict
        }
        return conflict
    }

    private enum TestError: Error { case missingConflict }
}

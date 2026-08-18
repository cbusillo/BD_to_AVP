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

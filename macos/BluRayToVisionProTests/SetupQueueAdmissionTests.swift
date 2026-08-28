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
        let secondDraft = makeDraft(path: "/tmp/Second Movie.mkv")
        let conflict = try conflict(for: draft.options)
        let admission = SetupQueueAdmission()
        admission.add(drafts: [draft, secondDraft], conflicts: [conflict, conflict])
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
        let secondDraft = makeDraft(path: "/tmp/Second Movie.mkv")
        let conflict = try conflict(for: draft.options)
        let admission = SetupQueueAdmission()
        admission.add(drafts: [draft, secondDraft], conflicts: [conflict, conflict])
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

    func testClearQueueIsAvailableForAttentionButNotRunningItems() throws {
        let admission = SetupQueueAdmission()
        let draft = makeDraft()
        admission.add(drafts: [draft])
        let itemID = admission.items[0].id
        admission.markRunning(itemID)

        XCTAssertFalse(admission.canClear)

        let failedItem = DurableConversionQueueItem(
            id: itemID,
            ordinal: 0,
            origin: .singleSource,
            intent: DurableQueueItemIntent(draft: draft),
            inspection: draft.sourceDetails,
            state: .failed,
            failure: DurableQueueFailure(
                code: "conversion_failed",
                message: "Conversion failed",
                details: nil,
                retryable: true
            )
        )
        admission.synchronize(with: [try PersistentQueueItem(item: failedItem)])
        XCTAssertTrue(admission.canClear)
        admission.removeAll()
        XCTAssertTrue(admission.items.isEmpty)
    }

    func testDuplicateDraftIsRejectedAcrossAddCalls() {
        let draft = makeDraft()
        let admission = SetupQueueAdmission()

        let firstResult = admission.add(drafts: [draft])
        let duplicateResult = admission.add(drafts: [draft])

        XCTAssertEqual(firstResult, SetupQueueAddResult(addedCount: 1, duplicateDisplayNames: []))
        XCTAssertEqual(
            duplicateResult,
            SetupQueueAddResult(addedCount: 0, duplicateDisplayNames: ["Movie.mkv"])
        )
        XCTAssertEqual(admission.items.count, 1)
        XCTAssertFalse(admission.canAdd(drafts: [draft]))
    }

    func testDuplicateDraftIsRejectedWithinIncomingBatch() {
        let draft = makeDraft()
        let admission = SetupQueueAdmission()

        let result = admission.add(drafts: [draft, draft])

        XCTAssertEqual(result.addedCount, 1)
        XCTAssertEqual(result.duplicateDisplayNames, ["Movie.mkv"])
        XCTAssertEqual(admission.items.count, 1)
    }

    func testDifferentTitlesFromSameSourceCanBothBeAdded() {
        let firstDraft = makeDraft(selectedTitle: makeTitle(id: "title:1", name: "First Feature"))
        let secondDraft = makeDraft(selectedTitle: makeTitle(id: "title:2", name: "Second Feature"))
        let admission = SetupQueueAdmission()

        let result = admission.add(drafts: [firstDraft, secondDraft])

        XCTAssertEqual(result.addedCount, 2)
        XCTAssertTrue(result.duplicateDisplayNames.isEmpty)
        XCTAssertEqual(admission.items.count, 2)
    }

    func testChangedSettingsDoNotPermitDuplicateSourceTitle() {
        let draft = makeDraft()
        var changedOptions = draft.options
        changedOptions.encoding.audioBitrate = 512
        let changedDraft = makeDraft(options: changedOptions)
        let admission = SetupQueueAdmission()
        admission.add(drafts: [draft])

        let result = admission.add(drafts: [changedDraft])

        XCTAssertEqual(result.addedCount, 0)
        XCTAssertEqual(result.duplicateCount, 1)
        XCTAssertEqual(admission.items.count, 1)
    }

    private func makeDraft(
        path: String = "/tmp/Movie.mkv",
        options: ConversionOptions = ConversionOptions(),
        selectedTitle: SourceTitle? = nil
    ) -> ConversionDraft {
        let source = ConversionSource(kind: .matroska, url: URL(fileURLWithPath: path))
        return ConversionDraft(
            source: source,
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp"),
            options: options,
            selectedTitle: selectedTitle
        )
    }

    private func makeTitle(id: String, name: String) -> SourceTitle {
        SourceTitle(
            id: id,
            name: name,
            outputName: name,
            durationSeconds: 7_200,
            resolution: "1920x1080",
            frameRate: "24000/1001",
            mainFeature: false
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

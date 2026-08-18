import XCTest
@testable import BluRayToVisionPro

final class QueueResolutionTests: XCTestCase {
    func testGroupingRequiresSameCauseAndValidExitsAndDisclosesTitles() throws {
        let first = try candidate(title: "One")
        let second = try candidate(title: "Two")
        var changedOptions = ConversionOptions()
        try changedOptions.encoding.selectQualityStep(.maximumDetail)
        guard case let .conflict(differentConflict) = RouteQualityEngine.propose(
            options: changedOptions,
            edit: .softwareEncoder(true)
        ) else { return XCTFail("Expected conflict") }
        let third = QueueResolutionCandidate(id: UUID(), draft: first.draft, conflict: differentConflict)

        let groups = QueueResolutionGroup.group([first, second, third])

        XCTAssertEqual(groups.count, 2)
        XCTAssertEqual(groups.first(where: { $0.candidates.count == 2 })?.titleDisclosure, "One, Two")
    }

    func testApplicationStartsUnselectedAndChangesOnlyChosenScope() throws {
        let first = try candidate(title: "One")
        let second = try candidate(title: "Two")
        let group = try XCTUnwrap(QueueResolutionGroup.group([first, second]).first)
        var selection = QueueResolutionSelection()
        XCTAssertFalse(selection.canApply)
        selection.resolutionID = try XCTUnwrap(group.conflict.resolutions.first(where: { $0.isAvailable })?.id)
        selection.scope = .item(first.id)

        let application = try QueueResolutionApplication.apply(group: group, selection: selection).get()

        XCTAssertEqual(Set(application.resolvedDrafts.keys), [first.id])
    }

    private func candidate(title: String) throws -> QueueResolutionCandidate {
        var options = ConversionOptions()
        try options.encoding.selectQualityStep(.maximumDetail)
        guard case let .conflict(conflict) = RouteQualityEngine.propose(
            options: options,
            edit: .reusableIntermediates(true)
        ) else { throw TestError.missingConflict }
        let source = ConversionSource(
            kind: .matroska,
            url: URL(fileURLWithPath: "/tmp/\(title).mkv"),
            displayName: title,
            workerSourcePath: "/tmp/\(title).mkv"
        )
        let draft = ConversionDraft(
            source: source,
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp"),
            options: options
        )
        return QueueResolutionCandidate(id: UUID(), draft: draft, conflict: conflict)
    }

    private enum TestError: Error { case missingConflict }
}

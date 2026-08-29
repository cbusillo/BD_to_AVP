import Foundation
import XCTest
@testable import BluRayToVisionPro

final class PersistentQueueOutcomeSummaryTests: XCTestCase {
    func testCategorizesCompletedFailedAndNeedsActionSeparately() throws {
        let completed = try makeItem(ordinal: 5, state: .completed)
        let failed = try makeItem(ordinal: 1, state: .failed)
        let needsChoice = try makeItem(ordinal: 4, state: .needsChoice)
        let interrupted = try makeItem(ordinal: 2, state: .interrupted)
        let attention = try makeItem(ordinal: 3, state: .attention)
        let stopped = try makeItem(ordinal: 0, state: .stopped)
        let notStarted = try makeItem(ordinal: 6, state: .notStarted)

        let summary = PersistentQueueOutcomeSummary(items: [completed, failed, needsChoice, interrupted, attention, stopped, notStarted])

        XCTAssertEqual(summary.completedItemIDs, [completed.id])
        XCTAssertEqual(summary.failedItemIDs, [failed.id])
        XCTAssertEqual(summary.needsActionItemIDs, [interrupted.id, attention.id, needsChoice.id])
        XCTAssertTrue(summary.hasAnyResults)
    }

    func testNotificationDescriptionOmitsZeroCategories() throws {
        let completed = try makeItem(ordinal: 0, state: .completed)
        let failed = try makeItem(ordinal: 1, state: .failed)

        XCTAssertEqual(
            PersistentQueueOutcomeSummary(items: [completed, failed]).notificationDescription,
            "1 completed. 1 failed."
        )
    }

    private func makeItem(ordinal: Int, state: DurableQueueItemState) throws -> PersistentQueueItem {
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/Sources/Feature-\(ordinal).mkv")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies"),
            options: ConversionOptions()
        )
        return try PersistentQueueItem(item: DurableConversionQueueItem(
            ordinal: ordinal,
            origin: .singleSource,
            intent: DurableQueueItemIntent(draft: draft),
            state: state,
            decision: state == .attention ? DurableQueueDecision(
                identifier: "source_missing",
                prompt: "Reconnect the source.",
                choices: [WorkerRecoveryChoice.retryContinueOnError.rawValue]
            ) : nil,
            failure: state == .failed ? DurableQueueFailure(
                code: "temporary",
                message: "Temporary failure",
                details: nil,
                retryable: true
            ) : nil,
            result: state == .completed ? DurableQueueResult(outputPath: "/Movies/Feature-\(ordinal).mov") : nil,
            routeQualityConflict: state == .needsChoice ? DurableRouteQualityConflict(conflict: makeRouteQualityConflict()) : nil
        ))
    }

    private func makeRouteQualityConflict() throws -> RouteQualityConflict {
        var options = ConversionOptions()
        try options.encoding.selectQualityStep(.maximumDetail)
        guard case let .conflict(conflict) = RouteQualityEngine.propose(
            options: options,
            edit: .reusableIntermediates(true)
        ) else {
            throw NSError(domain: "PersistentQueueOutcomeSummaryTests", code: 1)
        }
        return conflict
    }
}

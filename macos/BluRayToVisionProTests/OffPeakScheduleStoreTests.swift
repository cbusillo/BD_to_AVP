import Foundation
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class OffPeakScheduleStoreTests: XCTestCase {
    func testSchedulePersistsEditsAndCancellation() async throws {
        let fileURL = temporaryScheduleURL()
        defer { try? FileManager.default.removeItem(at: fileURL.deletingLastPathComponent()) }
        let first = schedule(start: 100, end: 200)
        let edited = schedule(start: 300, end: 500)
        let store = OffPeakScheduleStore(fileURL: fileURL)

        try await store.save(first)
        XCTAssertEqual(OffPeakScheduleStore(fileURL: fileURL).schedule, first)

        try await store.save(edited)
        XCTAssertEqual(OffPeakScheduleStore(fileURL: fileURL).schedule, edited)

        try await store.cancel()
        XCTAssertNil(OffPeakScheduleStore(fileURL: fileURL).schedule)
    }

    func testUnsupportedDocumentFailsClosedWithoutOverwriting() async throws {
        let fileURL = temporaryScheduleURL()
        defer { try? FileManager.default.removeItem(at: fileURL.deletingLastPathComponent()) }
        try FileManager.default.createDirectory(
            at: fileURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let original = Data(#"{"version":2,"schedule":null,"lastOutcome":null}"#.utf8)
        try original.write(to: fileURL)
        let store = OffPeakScheduleStore(fileURL: fileURL)

        XCTAssertTrue(store.writesBlocked)
        await XCTAssertThrowsErrorAsync {
            try await store.save(self.schedule(start: 100, end: 200))
        }
        XCTAssertEqual(try Data(contentsOf: fileURL), original)
    }

    func testExactStartConsumesScheduleOnce() async throws {
        let store = OffPeakScheduleStore.inMemory()
        let scheduled = schedule(start: 100, end: 200)
        try await store.save(scheduled)

        let firstEvaluation = try await store.evaluate(at: date(100), appLaunched: false)
        let secondEvaluation = try await store.evaluate(at: date(100), appLaunched: false)
        XCTAssertEqual(firstEvaluation, .start(scheduled))
        XCTAssertEqual(secondEvaluation, .none)
        XCTAssertEqual(store.lastOutcome?.kind, .started)
    }

    func testInWindowWakeStartsLateWhileAppStayedOpen() async throws {
        let store = OffPeakScheduleStore.inMemory()
        let scheduled = schedule(start: 100, end: 200)
        try await store.save(scheduled)

        let evaluation = try await store.evaluate(at: date(150), appLaunched: false)
        XCTAssertEqual(evaluation, .start(scheduled))
    }

    func testRelaunchAfterStartMarksScheduleMissed() async throws {
        let store = OffPeakScheduleStore.inMemory()
        try await store.save(schedule(start: 100, end: 200))

        let evaluation = try await store.evaluate(at: date(150), appLaunched: true)
        XCTAssertEqual(evaluation, .missed(.appRelaunchedAfterStart))
        XCTAssertEqual(store.lastOutcome?.missReason, .appRelaunchedAfterStart)
    }

    func testEvaluationAfterEndMarksWindowMissed() async throws {
        let store = OffPeakScheduleStore.inMemory()
        try await store.save(schedule(start: 100, end: 200))

        let evaluation = try await store.evaluate(at: date(200), appLaunched: true)
        XCTAssertEqual(evaluation, .missed(.windowEndedBeforeEvaluation))
    }

    func testConcurrentEvaluationConsumesOnlyOnce() async throws {
        let store = OffPeakScheduleStore.inMemory()
        let scheduled = schedule(start: 100, end: 200)
        try await store.save(scheduled)

        async let first = store.evaluate(at: date(150), appLaunched: false)
        async let second = store.evaluate(at: date(150), appLaunched: false)
        let results = try await [first, second]

        XCTAssertEqual(results.filter { $0 == .start(scheduled) }.count, 1)
        XCTAssertEqual(results.filter { $0 == .none }.count, 1)
    }

    func testConsumedStartCanRecordNoRunnableItems() async throws {
        let store = OffPeakScheduleStore.inMemory()
        let scheduled = schedule(start: 100, end: 200)
        try await store.save(scheduled)
        _ = try await store.evaluate(at: date(150), appLaunched: false)

        try await store.markStartedScheduleWithoutRunnableItems(scheduleID: scheduled.id, at: date(151))

        XCTAssertEqual(store.lastOutcome?.kind, .missed)
        XCTAssertEqual(store.lastOutcome?.missReason, .noRunnableItems)
    }

    private func schedule(start: TimeInterval, end: TimeInterval) -> OffPeakQueueSchedule {
        OffPeakQueueSchedule(startAt: date(start), endAt: date(end), createdAt: date(0))
    }

    private func date(_ interval: TimeInterval) -> Date {
        Date(timeIntervalSince1970: interval)
    }

    private func temporaryScheduleURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
            .appendingPathComponent("queue.schedule.json")
    }
}

private func XCTAssertThrowsErrorAsync(
    _ expression: () async throws -> Void,
    file: StaticString = #filePath,
    line: UInt = #line
) async {
    do {
        try await expression()
        XCTFail("Expected expression to throw", file: file, line: line)
    } catch {}
}

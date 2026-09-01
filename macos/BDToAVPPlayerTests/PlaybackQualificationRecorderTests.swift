#if BD_TO_AVP_QUALIFICATION
import AVFoundation
import Foundation
import XCTest
@testable import BDToAVPPlayer

final class PlaybackQualificationRecorderTests: XCTestCase {
    func testSafeIdentifierSanitizesUnsafeCharactersAndRejectsEmptyValues() {
        XCTAssertEqual(
            PlaybackQualificationRecorder.safeIdentifier("run id/with spaces"),
            "run_id_with_spaces"
        )
        XCTAssertNil(PlaybackQualificationRecorder.safeIdentifier("///"))
        XCTAssertNil(PlaybackQualificationRecorder.safeIdentifier(String(repeating: "a", count: 97)))
    }

    func testCategoryMappingsMatchHostContract() {
        XCTAssertEqual(PlaybackQualificationRecorder.timeControlStatusCategory(.paused), "paused")
        XCTAssertEqual(
            PlaybackQualificationRecorder.timeControlStatusCategory(.waitingToPlayAtSpecifiedRate),
            "waiting"
        )
        XCTAssertEqual(PlaybackQualificationRecorder.timeControlStatusCategory(.playing), "playing")
        XCTAssertEqual(PlaybackQualificationRecorder.waitingReasonCategory(nil), "none")
        XCTAssertEqual(
            PlaybackQualificationRecorder.waitingReasonCategory(.evaluatingBufferingRate),
            "evaluating_buffering_rate"
        )
        XCTAssertEqual(
            PlaybackQualificationRecorder.waitingReasonCategory(.toMinimizeStalls),
            "to_minimize_stalls"
        )
        XCTAssertEqual(PlaybackQualificationRecorder.waitingReasonCategory(.noItemToPlay), "no_item")
        XCTAssertEqual(PlaybackQualificationRecorder.itemStatusCategory(nil), "unknown")
        XCTAssertEqual(PlaybackQualificationRecorder.itemStatusCategory(.readyToPlay), "ready")
        XCTAssertEqual(PlaybackQualificationRecorder.itemErrorCategory(nil), "none")
        XCTAssertEqual(
            PlaybackQualificationRecorder.itemErrorCategory(
                NSError(domain: NSURLErrorDomain, code: -1)
            ),
            "network"
        )
        XCTAssertEqual(
            PlaybackQualificationRecorder.itemErrorCategory(
                NSError(domain: AVFoundationErrorDomain, code: -1)
            ),
            "decoder"
        )
    }

    func testJSONLShapeIsCanonicalAndPathFree() throws {
        let rootURL = try makeTemporaryDirectory()
        var currentDate = Date(timeIntervalSince1970: 1_700_000_000)
        var currentUptime: TimeInterval = 100
        let recorder = try XCTUnwrap(
            PlaybackQualificationRecorder(
                runID: "run id/with spaces",
                mediaID: "media-123",
                outputDirectoryURL: rootURL,
                dateProvider: { currentDate },
                uptimeProvider: { currentUptime },
                thermalStateProvider: { .fair },
                footprintProvider: { 123_456 },
                hardwareModelProvider: { "RealityDevice14,1" }
            )
        )
        let player = AVPlayer()

        recorder.recordPrepare(player: player)
        currentDate.addTimeInterval(15)
        currentUptime += 15
        recorder.recordSample(player: player, durationSeconds: 42)
        recorder.recordPlayRequested(player: player)
        recorder.recordPauseRequested(player: player)
        recorder.recordSeekStarted(player: player, detail: "resume_restore")
        recorder.recordSeekCompleted(player: player, detail: "resume_restore")
        recorder.recordSceneInactive(player: player)
        recorder.recordSceneActive(player: player)
        recorder.recordSessionFinished(player: player, durationSeconds: 42)

        let contents = try String(contentsOf: recorder.fileURL, encoding: .utf8)
        XCTAssertEqual(recorder.fileURL.lastPathComponent, "run_id_with_spaces.jsonl")
        XCTAssertFalse(contents.contains("file://"))
        XCTAssertFalse(contents.contains("/tmp/"))
        XCTAssertFalse(contents.contains("bookmark"))
        XCTAssertFalse(contents.contains("filename"))

        let lines = try jsonLines(from: contents)
        let header = try XCTUnwrap(lines.first(where: { stringValue($0, "kind") == "header" }))
        XCTAssertEqual(stringValue(header, "run_id"), "run_id_with_spaces")
        XCTAssertEqual(stringValue(header, "media_id"), "media-123")
        XCTAssertEqual(numberValue(header, "sample_interval_seconds"), 15)
        XCTAssertEqual(
            stringValue(try objectValue(header, "device"), "hardware_model"),
            "RealityDevice14,1"
        )
        XCTAssertNotNil(stringValue(header, "captured_at"))

        let sample = try XCTUnwrap(lines.first(where: { stringValue($0, "kind") == "sample" }))
        XCTAssertEqual(stringValue(sample, "thermal_state"), "fair")
        XCTAssertEqual(numberValue(sample, "physical_footprint_bytes"), 123_456)
        XCTAssertEqual(numberValue(sample, "duration_seconds"), 42)
        XCTAssertEqual(stringValue(sample, "time_control_status"), "paused")
        XCTAssertEqual(stringValue(sample, "waiting_reason"), "none")
        XCTAssertEqual(stringValue(sample, "item_status"), "unknown")
        XCTAssertTrue(sample["likely_to_keep_up"] is NSNull)
        XCTAssertEqual(stringValue(sample, "item_error_category"), "none")

        let events = lines.filter { stringValue($0, "kind") == "event" }
        XCTAssertTrue(events.contains(where: { stringValue($0, "event") == "prepare" }))
        XCTAssertTrue(events.contains(where: { stringValue($0, "event") == "play_requested" }))
        XCTAssertTrue(events.contains(where: {
            stringValue($0, "event") == "pause_requested"
                && stringValue($0, "detail") == "user_pause"
        }))
        XCTAssertTrue(events.contains(where: {
            stringValue($0, "event") == "seek_started"
                && stringValue($0, "detail") == "resume_restore"
        }))
        XCTAssertTrue(events.contains(where: {
            stringValue($0, "event") == "scene_inactive"
                && stringValue($0, "detail") == "scene_inactive"
        }))
        XCTAssertTrue(events.contains(where: { stringValue($0, "event") == "session_finished" }))

        let footer = try XCTUnwrap(lines.last)
        XCTAssertEqual(stringValue(footer, "kind"), "footer")
        XCTAssertEqual(stringValue(footer, "reason"), "session_finished")
        XCTAssertEqual(numberValue(footer, "duration_seconds"), 42)
    }

    func testSampleCadenceSkipsIntervalsBelowFifteenSeconds() throws {
        let rootURL = try makeTemporaryDirectory()
        var currentUptime: TimeInterval = 10
        let recorder = try XCTUnwrap(
            PlaybackQualificationRecorder(
                runID: "cadence",
                mediaID: "media-456",
                outputDirectoryURL: rootURL,
                uptimeProvider: { currentUptime },
                footprintProvider: { nil }
            )
        )
        let player = AVPlayer()

        recorder.recordSampleIfNeeded(player: player, durationSeconds: 100)
        currentUptime += 14.9
        recorder.recordSampleIfNeeded(player: player, durationSeconds: 100)
        currentUptime += 0.2
        recorder.recordSampleIfNeeded(player: player, durationSeconds: 100)
        recorder.recordSessionFinished(player: player, durationSeconds: 100)

        let lines = try jsonLines(from: String(contentsOf: recorder.fileURL, encoding: .utf8))
        XCTAssertEqual(lines.filter { stringValue($0, "kind") == "sample" }.count, 2)
    }

    func testNilFootprintIsRecordedAsNull() throws {
        let recorder = try XCTUnwrap(
            PlaybackQualificationRecorder(
                runID: "footprint",
                mediaID: "media-789",
                outputDirectoryURL: try makeTemporaryDirectory(),
                footprintProvider: { nil }
            )
        )
        recorder.recordSample(player: AVPlayer(), durationSeconds: 100)
        recorder.recordSessionFinished(player: AVPlayer(), durationSeconds: 100)

        let sample = try XCTUnwrap(
            jsonLines(from: String(contentsOf: recorder.fileURL, encoding: .utf8))
                .first(where: { stringValue($0, "kind") == "sample" })
        )
        XCTAssertTrue(sample["physical_footprint_bytes"] is NSNull)
    }

    func testSampleAfterFooterIsIgnored() throws {
        let recorder = try XCTUnwrap(
            PlaybackQualificationRecorder(
                runID: "post-footer-sample",
                mediaID: "media-post-footer",
                outputDirectoryURL: try makeTemporaryDirectory()
            )
        )
        recorder.recordSessionFinished(player: AVPlayer(), durationSeconds: 100)
        let before = try Data(contentsOf: recorder.fileURL)
        recorder.recordSample(player: AVPlayer(), durationSeconds: 100)
        XCTAssertEqual(try Data(contentsOf: recorder.fileURL), before)
    }

    func testTimeControlEventsUseCapturedStatusAndClocks() throws {
        let rootURL = try makeTemporaryDirectory()
        let startDate = Date(timeIntervalSince1970: 1_700_000_000)
        let recorder = try XCTUnwrap(
            PlaybackQualificationRecorder(
                runID: "time-control",
                mediaID: "media-time-control",
                outputDirectoryURL: rootURL,
                dateProvider: { startDate },
                uptimeProvider: { 100 }
            )
        )

        recorder.recordTimeControlChanged(
            status: .paused,
            playerTimeSeconds: 4,
            capturedAt: startDate.addingTimeInterval(10),
            capturedUptime: 110
        )
        recorder.recordTimeControlChanged(
            status: .waitingToPlayAtSpecifiedRate,
            playerTimeSeconds: 5,
            capturedAt: startDate.addingTimeInterval(12),
            capturedUptime: 112
        )
        recorder.recordTimeControlChanged(
            status: .playing,
            playerTimeSeconds: 6,
            capturedAt: startDate.addingTimeInterval(15),
            capturedUptime: 115
        )
        recorder.recordSessionFinished(player: AVPlayer(), durationSeconds: 100)

        let events = try jsonLines(from: String(contentsOf: recorder.fileURL, encoding: .utf8))
            .filter { stringValue($0, "event") == "time_control_changed" }
        XCTAssertEqual(events.map { stringValue($0, "detail") }, [
            "paused", "paused->waiting", "waiting->playing"
        ])
        XCTAssertEqual(events.map { numberValue($0, "elapsed_seconds") }, [10, 12, 15])
        XCTAssertEqual(events.map { numberValue($0, "player_time_seconds") }, [4, 5, 6])
        XCTAssertEqual(
            stringValue(events[0], "captured_at"),
            "2023-11-14T22:13:30.000Z"
        )
    }

    func testRepeatedRunIDPreservesThePreviousLog() throws {
        let rootURL = try makeTemporaryDirectory()
        let first = try XCTUnwrap(
            PlaybackQualificationRecorder(
                runID: "duplicate-run",
                mediaID: "media-1",
                outputDirectoryURL: rootURL
            )
        )
        first.recordSample(player: AVPlayer(), durationSeconds: 100)
        first.recordSessionFinished(player: AVPlayer(), durationSeconds: 100)
        let original = try Data(contentsOf: first.fileURL)

        let second = PlaybackQualificationRecorder(
            runID: "duplicate-run",
            mediaID: "media-1",
            outputDirectoryURL: rootURL
        )
        XCTAssertNil(second)
        XCTAssertEqual(try Data(contentsOf: first.fileURL), original)
    }

    private func makeTemporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("PlaybackQualificationRecorderTests", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    private func jsonLines(from contents: String) throws -> [[String: Any]] {
        try contents
            .split(separator: "\n", omittingEmptySubsequences: true)
            .map { line in
                let object = try JSONSerialization.jsonObject(with: Data(line.utf8))
                return try XCTUnwrap(object as? [String: Any])
            }
    }

    private func objectValue(_ object: [String: Any], _ key: String) throws -> [String: Any] {
        try XCTUnwrap(object[key] as? [String: Any])
    }

    private func stringValue(_ object: [String: Any], _ key: String) -> String? {
        object[key] as? String
    }

    private func numberValue(_ object: [String: Any], _ key: String) -> Double? {
        (object[key] as? NSNumber)?.doubleValue
    }
}
#endif

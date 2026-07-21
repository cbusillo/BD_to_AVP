import Foundation
import XCTest
@testable import BluRayToVisionPro

final class LiveObservabilityStatusTests: XCTestCase {
    func testReducerProjectsPathFreeLiveToolAndArtifactState() throws {
        let event = try makeEvent(
            kind: "tool.artifact",
            stageID: "create_mkv",
            toolID: "makemkvcon",
            process: ["pid": 42, "process_group_id": 42],
            activityAge: 12,
            artifact: [
                "role": "intermediate",
                "state": "growing",
                "location": [
                    "value": "/private/output/Feature.mkv",
                    "privacy": "private",
                    "truncated": false,
                ],
                "size_bytes": 1_048_576,
                "modification_age_seconds": 2,
                "growth_bytes_per_second": 262_144,
            ]
        )
        var status = LiveObservabilityStatus.empty

        status.receive(event, receivedAt: Date(timeIntervalSince1970: 100))

        XCTAssertEqual(status.stageID, "create_mkv")
        XCTAssertEqual(status.toolID, "makemkvcon")
        XCTAssertEqual(status.processState, .running)
        XCTAssertEqual(status.lastOutputAgeSeconds, 12)
        XCTAssertEqual(status.artifactRole, "intermediate")
        XCTAssertEqual(status.artifactState, "growing")
        XCTAssertEqual(status.artifactSizeBytes, 1_048_576)
        XCTAssertEqual(status.artifactModificationAgeSeconds, 2)
        XCTAssertEqual(status.artifactGrowthBytesPerSecond, 262_144)
        XCTAssertFalse(String(describing: status).contains("/private/output"))
    }

    func testHostClockAdvancesLastOutputAgeAndDetectsStall() throws {
        let event = try makeEvent(
            kind: "tool.activity",
            process: ["pid": 42, "process_group_id": 42],
            activityAge: 12
        )
        let receivedAt = Date(timeIntervalSince1970: 100)
        var status = LiveObservabilityStatus.empty
        status.receive(event, receivedAt: receivedAt)

        XCTAssertEqual(
            status.currentLastOutputAgeSeconds(at: receivedAt.addingTimeInterval(5.9)),
            17
        )
        XCTAssertFalse(status.isStalled(at: receivedAt.addingTimeInterval(47)))
        XCTAssertTrue(status.isStalled(at: receivedAt.addingTimeInterval(48)))

        status.receive(
            try makeEvent(
                kind: "tool.artifact",
                process: ["pid": 42],
                artifact: ["role": "intermediate", "state": "growing"]
            ),
            receivedAt: receivedAt.addingTimeInterval(20)
        )
        XCTAssertEqual(
            status.currentLastOutputAgeSeconds(at: receivedAt.addingTimeInterval(21)),
            33
        )
    }

    func testGrowingArtifactKeepsQuietToolOutOfStalledState() throws {
        let receivedAt = Date(timeIntervalSince1970: 100)
        var status = LiveObservabilityStatus.empty
        status.receive(
            try makeEvent(
                kind: "tool.activity",
                toolID: "ffmpeg",
                toolRunID: "run-a",
                process: ["pid": 42, "process_group_id": 42],
                activityAge: 58
            ),
            receivedAt: receivedAt
        )
        status.receive(
            try makeEvent(
                kind: "tool.artifact",
                stageID: "create_left_right_files",
                toolID: "ffmpeg",
                toolRunID: "run-a",
                process: ["pid": 42, "process_group_id": 42],
                artifact: [
                    "role": "left_eye_video_output",
                    "state": "growing",
                    "size_bytes": 1_572_864_000,
                    "modification_age_seconds": 4,
                    "growth_bytes_per_second": 8_388_608,
                ]
            ),
            receivedAt: receivedAt.addingTimeInterval(1)
        )

        XCTAssertEqual(
            status.activityState(at: receivedAt.addingTimeInterval(3)),
            .toolQuietArtifactsActive
        )
        XCTAssertFalse(status.isStalled(at: receivedAt.addingTimeInterval(3)))
    }

    func testStalePositiveGrowthSampleEventuallyBecomesStalled() throws {
        let receivedAt = Date(timeIntervalSince1970: 100)
        var status = LiveObservabilityStatus.empty
        status.receive(
            try makeEvent(
                kind: "tool.activity",
                toolID: "ffmpeg",
                toolRunID: "run-a",
                process: ["pid": 42, "process_group_id": 42],
                activityAge: 60
            ),
            receivedAt: receivedAt
        )
        status.receive(
            try makeEvent(
                kind: "tool.artifact",
                stageID: "create_left_right_files",
                toolID: "ffmpeg",
                toolRunID: "run-a",
                process: ["pid": 42, "process_group_id": 42],
                artifact: [
                    "role": "left_eye_video_output",
                    "state": "growing",
                    "size_bytes": 1_572_864_000,
                    "modification_age_seconds": 4,
                    "growth_bytes_per_second": 8_388_608,
                ]
            ),
            receivedAt: receivedAt
        )

        XCTAssertEqual(
            status.activityState(at: receivedAt.addingTimeInterval(60)),
            .stalled
        )
    }

    func testUnknownArtifactStateDoesNotMaskAStall() throws {
        let receivedAt = Date(timeIntervalSince1970: 100)
        var status = LiveObservabilityStatus.empty
        status.receive(
            try makeEvent(
                kind: "tool.activity",
                toolRunID: "run-a",
                process: ["pid": 42, "process_group_id": 42],
                activityAge: 60
            ),
            receivedAt: receivedAt
        )
        status.receive(
            try makeEvent(
                kind: "tool.artifact",
                toolRunID: "run-a",
                process: ["pid": 42],
                artifact: [
                    "role": "left_eye_video_output",
                    "state": "future/state",
                    "modification_age_seconds": 1,
                    "growth_bytes_per_second": 8_388_608,
                ]
            ),
            receivedAt: receivedAt
        )

        XCTAssertNil(status.artifactState)
        XCTAssertEqual(status.activityState(at: receivedAt), .stalled)
    }

    func testRetainsBothLeftAndRightArtifactSnapshots() throws {
        let receivedAt = Date(timeIntervalSince1970: 100)
        var status = LiveObservabilityStatus.empty
        status.receive(
            try makeEvent(
                kind: "tool.artifact",
                stageID: "create_left_right_files",
                toolID: "ffmpeg",
                toolRunID: "run-a",
                process: ["pid": 42, "process_group_id": 42],
                artifact: [
                    "role": "left_eye_video_output",
                    "state": "growing",
                    "size_bytes": 1_048_576,
                    "modification_age_seconds": 2,
                    "growth_bytes_per_second": 262_144,
                ]
            ),
            receivedAt: receivedAt
        )
        status.receive(
            try makeEvent(
                kind: "tool.artifact",
                stageID: "create_left_right_files",
                toolID: "ffmpeg",
                toolRunID: "run-a",
                process: ["pid": 42, "process_group_id": 42],
                artifact: [
                    "role": "right_eye_video_output",
                    "state": "growing",
                    "size_bytes": 2_097_152,
                    "modification_age_seconds": 3,
                    "growth_bytes_per_second": 524_288,
                ]
            ),
            receivedAt: receivedAt.addingTimeInterval(1)
        )

        XCTAssertEqual(status.artifacts.map(\.role), ["left_eye_video_output", "right_eye_video_output"])
        XCTAssertEqual(status.artifacts.map(\.growthBytesPerSecond), [262_144, 524_288])
        XCTAssertEqual(status.artifactRole, "right_eye_video_output")
        XCTAssertEqual(status.artifactGrowthBytesPerSecond, 524_288)
    }

    func testToolRunChangeResetsArtifactSnapshotState() throws {
        let receivedAt = Date(timeIntervalSince1970: 100)
        var status = LiveObservabilityStatus.empty
        status.receive(
            try makeEvent(
                kind: "tool.artifact",
                toolID: "ffmpeg",
                toolRunID: "run-a",
                process: ["pid": 42],
                artifact: [
                    "role": "left_eye_video_output",
                    "state": "growing",
                ]
            ),
            receivedAt: receivedAt
        )
        status.receive(
            try makeEvent(
                kind: "tool.started",
                toolID: "ffmpeg",
                toolRunID: "run-b",
                process: ["pid": 43, "process_group_id": 43]
            ),
            receivedAt: receivedAt.addingTimeInterval(1)
        )

        XCTAssertEqual(status.toolRunID, "run-b")
        XCTAssertTrue(status.artifacts.isEmpty)
    }

    func testSameToolRunRetainsArtifactsWhenToolLabelChanges() throws {
        let receivedAt = Date(timeIntervalSince1970: 100)
        var status = LiveObservabilityStatus.empty
        status.receive(
            try makeEvent(
                kind: "tool.artifact",
                toolID: "ffmpeg",
                toolRunID: "run-a",
                process: ["pid": 42],
                artifact: ["role": "left_eye_video_output", "state": "growing"]
            ),
            receivedAt: receivedAt
        )
        status.receive(
            try makeEvent(
                kind: "tool.activity",
                toolID: "ffmpeg-helper",
                toolRunID: "run-a",
                process: ["pid": 42],
                activityAge: 1
            ),
            receivedAt: receivedAt.addingTimeInterval(1)
        )

        XCTAssertEqual(status.toolID, "ffmpeg-helper")
        XCTAssertEqual(status.toolRunID, "run-a")
        XCTAssertEqual(status.artifacts.map(\.role), ["left_eye_video_output"])
    }

    func testSharedLeftRightFixturesKeepQuietEncoderActive() throws {
        let receivedAt = Date(timeIntervalSince1970: 100)
        var status = LiveObservabilityStatus.empty
        for fixtureName in [
            "observability_left_eye_growing_v1.json",
            "observability_right_eye_growing_v1.json",
        ] {
            status.receive(
                try JSONDecoder().decode(
                    ObservabilityEvent.self,
                    from: try sharedFixtureData(named: fixtureName)
                ),
                receivedAt: receivedAt
            )
        }

        XCTAssertEqual(status.activityState(at: receivedAt), .toolQuietArtifactsActive)
        XCTAssertEqual(
            status.artifacts.map(\.role),
            ["left_eye_video_output", "right_eye_video_output"]
        )
        XCTAssertEqual(status.artifacts.map(\.sizeBytes), [884_998_144, 1_589_137_900])
    }

    func testSharedLongRunningFixturesDistinguishHealthyWorkFromStall() throws {
        let receivedAt = Date(timeIntervalSince1970: 100)
        var active = LiveObservabilityStatus.empty
        active.receive(
            try JSONDecoder().decode(
                ObservabilityEvent.self,
                from: try sharedFixtureData(named: "observability_long_running_tool_v1.json")
            ),
            receivedAt: receivedAt
        )
        var stalled = LiveObservabilityStatus.empty
        stalled.receive(
            try JSONDecoder().decode(
                ObservabilityEvent.self,
                from: try sharedFixtureData(named: "observability_stalled_tool_v1.json")
            ),
            receivedAt: receivedAt
        )

        XCTAssertFalse(active.isStalled(at: receivedAt))
        XCTAssertEqual(active.artifactGrowthBytesPerSecond, 8_388_608)
        XCTAssertTrue(stalled.isStalled(at: receivedAt))
        XCTAssertEqual(stalled.artifactGrowthBytesPerSecond, 0)
        XCTAssertEqual(stalled.artifactModificationAgeSeconds, 61)
    }

    func testTerminalToolEventUpdatesProcessState() throws {
        var status = LiveObservabilityStatus.empty
        status.receive(
            try makeEvent(
                kind: "tool.started",
                process: ["pid": 42, "process_group_id": 42]
            ),
            receivedAt: Date(timeIntervalSince1970: 100)
        )
        status.receive(
            try makeEvent(
                kind: "tool.completed",
                process: ["pid": 42, "process_group_id": 42, "exit_code": 0]
            ),
            receivedAt: Date(timeIntervalSince1970: 105)
        )

        XCTAssertEqual(status.processState, .completed)
        XCTAssertEqual(status.currentLastOutputAgeSeconds(at: Date(timeIntervalSince1970: 200)), nil)

        status.receive(
            try makeEvent(
                kind: "tool.artifact",
                process: ["pid": 42, "process_group_id": 42],
                artifact: ["role": "intermediate", "state": "complete"]
            ),
            receivedAt: Date(timeIntervalSince1970: 106)
        )
        XCTAssertEqual(status.processState, .completed)

        status.receive(
            try makeEvent(
                kind: "tool.started",
                process: ["pid": 43, "process_group_id": 43]
            ),
            receivedAt: Date(timeIntervalSince1970: 107)
        )
        XCTAssertEqual(status.processState, .running)
    }

    func testUnsafeIdentifiersAreNotProjected() throws {
        let event = try makeEvent(
            kind: "tool.started",
            stageID: "../../private",
            toolID: "/usr/bin/ffmpeg",
            process: ["pid": 42]
        )
        var status = LiveObservabilityStatus.empty

        status.receive(event, receivedAt: Date())

        XCTAssertNil(status.stageID)
        XCTAssertNil(status.toolID)
        XCTAssertEqual(status.processState, .running)
    }

    private func makeEvent(
        kind: String,
        stageID: String = "encode",
        toolID: String = "ffmpeg",
        toolRunID: String = "tool-run-1",
        process: [String: Any]? = nil,
        activityAge: Int64? = nil,
        artifact: [String: Any]? = nil
    ) throws -> ObservabilityEvent {
        var fixture = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: sharedFixtureData()) as? [String: Any]
        )
        fixture["kind"] = kind
        var context = try XCTUnwrap(fixture["context"] as? [String: Any])
        context["stage"] = ["id": stageID]
        context["tool"] = ["id": toolID, "run_id": toolRunID]
        context["process"] = process
        fixture["context"] = context
        var data = try XCTUnwrap(fixture["data"] as? [String: Any])
        data["activity"] = activityAge.map { ["last_output_age_seconds": $0] }
        data["artifact"] = artifact
        fixture["data"] = data
        return try JSONDecoder().decode(
            ObservabilityEvent.self,
            from: JSONSerialization.data(withJSONObject: fixture, options: [.sortedKeys])
        )
    }

    private func sharedFixtureData() throws -> Data {
        try sharedFixtureData(named: "observability_event_v1.json")
    }

    private func sharedFixtureData(named fixtureName: String) throws -> Data {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/\(fixtureName)")
        return try XCTUnwrap(FileManager.default.contents(atPath: fixtureURL.path))
    }
}

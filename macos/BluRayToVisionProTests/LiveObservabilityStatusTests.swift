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
        context["tool"] = ["id": toolID]
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
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/observability_event_v1.json")
        return try XCTUnwrap(FileManager.default.contents(atPath: fixtureURL.path))
    }
}

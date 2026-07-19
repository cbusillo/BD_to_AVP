import Foundation
import XCTest
@testable import BluRayToVisionPro

final class ObservabilityEventTests: XCTestCase {
    func testSharedPythonFixtureDecodesAndRoundTrips() throws {
        let fixture = try sharedFixtureData()

        let event = try JSONDecoder().decode(ObservabilityEvent.self, from: fixture)
        let encoded = try JSONEncoder().encode(event)

        XCTAssertEqual(event.schema, "bd_to_avp.observability")
        XCTAssertEqual(event.schemaVersion, 1)
        XCTAssertEqual(event.kind, "tool.progress")
        XCTAssertEqual(event.context.stage?.id, "create_mkv")
        XCTAssertEqual(event.context.tool?.id, "makemkvcon")
        XCTAssertEqual(event.data.artifact?.sizeBytes, 1_593_835_520)
        XCTAssertEqual(try canonicalJSON(encoded), try canonicalJSON(fixture))
    }

    func testUnknownKindRemainsDecodable() throws {
        var fixture = try XCTUnwrap(try jsonObject(try sharedFixtureData()) as? [String: Any])
        fixture["kind"] = "future.tool.signal"
        let data = try JSONSerialization.data(withJSONObject: fixture, options: [.sortedKeys])

        let event = try JSONDecoder().decode(ObservabilityEvent.self, from: data)

        XCTAssertEqual(event.kind, "future.tool.signal")
    }

    func testWorkerEnvelopeDecodesCanonicalEvent() throws {
        let nestedEvent = try jsonObject(try sharedFixtureData())
        let jobID = UUID()
        let envelope: [String: Any] = [
            "protocol_version": WorkerJobSpec.protocolVersion,
            "type": "observability",
            "job_id": jobID.uuidString.lowercased(),
            "sequence": 2,
            "payload": ["event": nestedEvent],
        ]
        let data = try JSONSerialization.data(withJSONObject: envelope, options: [.sortedKeys])

        let workerEvent = try JSONDecoder().decode(WorkerEvent.self, from: data)

        XCTAssertEqual(workerEvent.type, .observability)
        XCTAssertEqual(workerEvent.jobID, jobID)
        XCTAssertEqual(workerEvent.payload.observabilityEvent?.kind, "tool.progress")
        XCTAssertEqual(workerEvent.payload.observabilityEvent?.context.tool?.id, "makemkvcon")
    }

    func testUnsupportedSchemaAndVersionAreRejected() throws {
        var fixture = try XCTUnwrap(try jsonObject(try sharedFixtureData()) as? [String: Any])
        fixture["schema"] = "future.observability"
        let unsupportedSchema = try JSONSerialization.data(withJSONObject: fixture)

        XCTAssertThrowsError(try JSONDecoder().decode(ObservabilityEvent.self, from: unsupportedSchema))

        fixture["schema"] = ObservabilityEvent.currentSchema
        fixture["schema_version"] = 2
        let unsupportedVersion = try JSONSerialization.data(withJSONObject: fixture)

        XCTAssertThrowsError(try JSONDecoder().decode(ObservabilityEvent.self, from: unsupportedVersion))
    }

    func testSecretAndOversizedTextAreRejected() throws {
        var fixture = try XCTUnwrap(try jsonObject(try sharedFixtureData()) as? [String: Any])
        fixture["privacy"] = "secret"
        let secretEvent = try JSONSerialization.data(withJSONObject: fixture)
        XCTAssertThrowsError(try JSONDecoder().decode(ObservabilityEvent.self, from: secretEvent))

        fixture["privacy"] = "private"
        var data = try XCTUnwrap(fixture["data"] as? [String: Any])
        var message = try XCTUnwrap(data["message"] as? [String: Any])
        message["privacy"] = "secret"
        data["message"] = message
        fixture["data"] = data
        let secretText = try JSONSerialization.data(withJSONObject: fixture)
        XCTAssertThrowsError(try JSONDecoder().decode(ObservabilityEvent.self, from: secretText))

        message["privacy"] = "private"
        message["value"] = String(repeating: "x", count: ObservabilityEvent.maximumMessageBytes + 1)
        data["message"] = message
        fixture["data"] = data
        let oversizedMessage = try JSONSerialization.data(withJSONObject: fixture)
        XCTAssertThrowsError(try JSONDecoder().decode(ObservabilityEvent.self, from: oversizedMessage))
    }

    func testNegativeOutputAgeIsRejected() throws {
        var fixture = try XCTUnwrap(try jsonObject(try sharedFixtureData()) as? [String: Any])
        var data = try XCTUnwrap(fixture["data"] as? [String: Any])
        data["activity"] = ["last_output_age_seconds": -1]
        fixture["data"] = data
        let negativeAge = try JSONSerialization.data(withJSONObject: fixture)

        XCTAssertThrowsError(try JSONDecoder().decode(ObservabilityEvent.self, from: negativeAge))
    }

    private func sharedFixtureData() throws -> Data {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/observability_event_v1.json")
        return try XCTUnwrap(
            FileManager.default.contents(atPath: fixtureURL.path),
            "Required observability fixture is missing"
        )
    }

    private func jsonObject(_ data: Data) throws -> Any {
        try JSONSerialization.jsonObject(with: data)
    }

    private func canonicalJSON(_ data: Data) throws -> Data {
        try JSONSerialization.data(withJSONObject: jsonObject(data), options: [.sortedKeys])
    }
}

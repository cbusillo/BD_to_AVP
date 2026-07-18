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

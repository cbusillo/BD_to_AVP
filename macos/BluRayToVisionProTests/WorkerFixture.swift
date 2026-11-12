import Foundation
import XCTest
@testable import BluRayToVisionPro

func currentWorkerFixtureData(_ family: String) throws -> Data {
    let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()
    let data = try Data(contentsOf: root.appendingPathComponent("tests/fixtures/\(family)_v13.json"))
    var payload = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    payload["protocol_version"] = WorkerJobSpec.protocolVersion
    return try JSONSerialization.data(withJSONObject: payload)
}

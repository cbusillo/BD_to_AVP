import XCTest
@testable import BluRayToVisionPro

final class RelayNetworkServerTests: XCTestCase {
    func testBonjourAdvertisementRequiresProtocolVersionThree() throws {
        let sessionID = try RelaySessionIdentifier(rawValue: "00000000-0000-4000-8000-000000000001")
        let advertisement = RelayBonjourAdvertisement(sessionID: sessionID)
        let record = NetService.dictionary(fromTXTRecord: advertisement.txtRecord)
        XCTAssertEqual(String(data: try XCTUnwrap(record["v"]), encoding: .utf8), "3")
        XCTAssertEqual(advertisement.serviceType, RelayWireContract.bonjourServiceType)
    }
}

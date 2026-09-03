import Foundation
import XCTest
@testable import BDToAVPPlayer

final class RelayBonjourBrowserTests: XCTestCase {
    func testBonjourBrowserMatchesHostServiceType() {
        XCTAssertEqual(RelayBonjourBrowser.serviceType, "_bdtoavp-relay._tcp")
    }

    func testEndpointKeepsResolvedHostAndPort() {
        let endpoint = makeTestEndpoint(baseURL: URL(string: "http://relay.local:49152")!)
        XCTAssertEqual(endpoint.displayName, "Vision-Pro")
        XCTAssertEqual(endpoint.baseURL.port, 49152)
    }
}

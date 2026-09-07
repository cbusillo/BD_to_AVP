import Darwin
import Foundation
import Network
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

    func testProtocolFilterAcceptsPendingTXTMetadata() {
        XCTAssertTrue(RelayBonjourProtocolFilter.accepts(nil))
    }

    func testProtocolFilterAcceptsMatchingStringVersion() {
        var record = NWTXTRecord()
        record.setEntry(.string("3"), for: "v")
        XCTAssertTrue(RelayBonjourProtocolFilter.accepts(record))
    }

    func testProtocolFilterAcceptsMatchingDataVersion() {
        var record = NWTXTRecord()
        record.setEntry(.data(Data("3".utf8)), for: "v")
        XCTAssertTrue(RelayBonjourProtocolFilter.accepts(record))
    }

    func testProtocolFilterRejectsExplicitVersionMismatch() {
        var record = NWTXTRecord()
        record.setEntry(.string("2"), for: "v")
        XCTAssertFalse(RelayBonjourProtocolFilter.accepts(record))
    }

    func testProtocolFilterRejectsTXTWithoutVersion() {
        var record = NWTXTRecord()
        record.setEntry(.string("session"), for: "sid")
        XCTAssertFalse(RelayBonjourProtocolFilter.accepts(record))
    }

    func testEndpointPrefersResolvedIPv4Address() throws {
        let endpoint = try XCTUnwrap(RelayBonjourEndpointFactory.endpoint(
            name: "Chris's Mac Studio",
            type: "_bdtoavp-relay._tcp.",
            domain: "local.",
            addresses: [ipv4Address("192.168.1.3")],
            hostName: "unresolvable.local.",
            port: 63688
        ))
        XCTAssertEqual(endpoint.baseURL, URL(string: "http://192.168.1.3:63688"))
    }

    func testEndpointFallsBackToResolvedHostName() throws {
        let endpoint = try XCTUnwrap(RelayBonjourEndpointFactory.endpoint(
            name: "Chris's Mac Studio",
            type: "_bdtoavp-relay._tcp.",
            domain: "local.",
            addresses: [],
            hostName: "relay.local.",
            port: 63688
        ))
        XCTAssertEqual(endpoint.baseURL, URL(string: "http://relay.local:63688"))
    }

    func testEndpointRejectsMissingAddressAndHostName() {
        XCTAssertNil(RelayBonjourEndpointFactory.endpoint(
            name: "Chris's Mac Studio",
            type: "_bdtoavp-relay._tcp.",
            domain: "local.",
            addresses: [],
            hostName: nil,
            port: 63688
        ))
    }

    private func ipv4Address(_ value: String) -> Data {
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        XCTAssertEqual(inet_pton(AF_INET, value, &address.sin_addr), 1)
        return withUnsafeBytes(of: &address) { Data($0) }
    }
}

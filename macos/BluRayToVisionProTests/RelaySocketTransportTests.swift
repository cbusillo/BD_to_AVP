import Darwin
import Foundation
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class RelaySocketTransportTests: XCTestCase {
    func testConnectionAbortedAcceptIsRetriedAndNextClientIsServed() async throws {
        let queue = DispatchQueue(label: "relay.socket.tests.aborted-accept")
        let lock = NSLock()
        var calls = 0
        let listener = try RelaySocketListener(queue: queue, acceptSyscall: { descriptor, address, length in
            let call = lock.withLock {
                calls += 1
                return calls
            }
            if call == 1 {
                errno = ECONNABORTED
                return -1
            }
            return Darwin.accept(descriptor, address, length)
        })
        defer { listener.cancel() }
        let served = expectation(description: "aborted accept is recovered")
        let failed = expectation(description: "listener remains healthy")
        failed.isInverted = true
        try listener.start(
            name: "RelaySocketTests-\(UUID().uuidString)", type: RelayWireContract.bonjourServiceType,
            txtRecord: Data([0]),
            accept: { connection, _ in
                connection.receive { request, error in
                    guard request != nil, error == nil else { connection.cancel(); return }
                    connection.send(Data("HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n".utf8)) { error in
                        XCTAssertNil(error)
                        connection.cancel()
                        served.fulfill()
                    }
                }
            }, failure: { failed.fulfill() }
        )
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        let url = try XCTUnwrap(URL(string: "http://127.0.0.1:\(listener.port)/recovered"))
        let (_, response) = try await session.data(from: url)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 204)
        await fulfillment(of: [served, failed], timeout: 2)
    }

    func testResourcePressureAcceptUsesBoundedBackoffAndCancelsCleanly() async throws {
        let queue = DispatchQueue(label: "relay.socket.tests.resource-accept")
        let lock = NSLock()
        var calls = 0
        let listener = try RelaySocketListener(queue: queue, acceptSyscall: { _, _, _ in
            lock.withLock { calls += 1 }
            errno = EMFILE
            return -1
        })
        let failed = expectation(description: "resource pressure does not fail listener")
        failed.isInverted = true
        try listener.start(
            name: "RelaySocketTests-\(UUID().uuidString)", type: RelayWireContract.bonjourServiceType,
            txtRecord: Data([0]), accept: { _, _ in XCTFail("resource pressure must not admit a client") },
            failure: { failed.fulfill() }
        )
        let session = URLSession(configuration: .ephemeral)
        let task = session.dataTask(with: try XCTUnwrap(URL(string: "http://127.0.0.1:\(listener.port)/pressure")))
        task.resume()
        try await Task.sleep(for: .milliseconds(250))
        task.cancel()
        session.invalidateAndCancel()
        listener.cancel()
        let observedCalls = lock.withLock { calls }
        XCTAssertGreaterThanOrEqual(observedCalls, 1)
        XCTAssertLessThanOrEqual(observedCalls, 4)
        await fulfillment(of: [failed], timeout: 1)
    }

    func testAcceptErrorDispositionRecoversExpectedTransientErrors() {
        XCTAssertEqual(RelaySocketAcceptErrorDisposition.classify(ECONNABORTED), .retry)
        XCTAssertEqual(RelaySocketAcceptErrorDisposition.classify(EMFILE), .retryAfterBackoff)
        XCTAssertEqual(RelaySocketAcceptErrorDisposition.classify(ENFILE), .retryAfterBackoff)
        XCTAssertEqual(RelaySocketAcceptErrorDisposition.classify(ENOBUFS), .retryAfterBackoff)
        XCTAssertEqual(RelaySocketAcceptErrorDisposition.classify(ENOMEM), .retryAfterBackoff)
        XCTAssertEqual(RelaySocketAcceptErrorDisposition.classify(EINVAL), .fail)
    }

    func testSocketListenerDeliversCompleteMultiMegabyteResponse() async throws {
        let queue = DispatchQueue(label: "relay.socket.tests.listener")
        let listener = try RelaySocketListener(queue: queue)
        defer { listener.cancel() }
        let expected = Data((0..<(4 * 1_024 * 1_024)).map { UInt8(truncatingIfNeeded: $0) })
        let sent = expectation(description: "response sent")
        try listener.start(
            name: "RelaySocketTests-\(UUID().uuidString)", type: RelayWireContract.bonjourServiceType,
            txtRecord: Data([0]),
            accept: { connection, _ in
                connection.receive { request, error in
                    guard request != nil, error == nil else { connection.cancel(); return }
                    let response = RelayHTTPResponse(statusCode: 200, headers: [:], body: expected)
                    connection.send(response.serialized()) { error in
                        XCTAssertNil(error)
                        connection.cancel()
                        sent.fulfill()
                    }
                }
            }, failure: { XCTFail("Bonjour registration failed") }
        )
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        let url = try XCTUnwrap(URL(string: "http://127.0.0.1:\(listener.port)/segment.m4s"))
        let (received, response) = try await session.data(from: url)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        XCTAssertEqual(received, expected)
        await fulfillment(of: [sent], timeout: 2)
    }

    func testCancellationInterruptsReadAndRejectsFurtherWrites() async throws {
        var pair: [Int32] = [-1, -1]
        XCTAssertEqual(socketpair(AF_UNIX, SOCK_STREAM, 0, &pair), 0)
        guard pair[0] >= 0, pair[1] >= 0 else { return }
        defer { Darwin.close(pair[1]) }
        let connection = try RelaySocketConnection(descriptor: pair[0])
        defer { connection.cancel() }
        let ended = expectation(description: "blocked receive ended")
        connection.receive { data, _ in
            XCTAssertNil(data)
            ended.fulfill()
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.05) { connection.cancel() }
        await fulfillment(of: [ended], timeout: 2)
        let rejected = expectation(description: "write after cancellation rejected")
        connection.send(Data("must not send".utf8)) { error in
            XCTAssertNotNil(error)
            rejected.fulfill()
        }
        await fulfillment(of: [rejected], timeout: 2)
    }
}

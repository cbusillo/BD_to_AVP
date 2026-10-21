import Darwin
import Foundation
import XCTest
@testable import BluRayToVisionPro

/// The Mac end of the descriptor handoff, exercised with a client in this process that plays the
/// worker: it connects, sends the token, and receives the descriptor with `recvmsg`.
final class LiveSourceHandoffTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("handoff-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    func testTheWorkerReceivesTheMacsOpenFileAndTheSocketIsRemoved() async throws {
        let source = directory.appendingPathComponent("Disc.iso")
        try Data(repeating: 7, count: 512).write(to: source)
        let descriptor = open(source.path, O_RDONLY | O_CLOEXEC)
        defer { close(descriptor) }
        let handoff = try LiveSourceHandoff()
        XCTAssertLessThan(handoff.socketPath.utf8.count, 104)
        let marker = URL(fileURLWithPath: handoff.socketPath).deletingLastPathComponent()
            .appendingPathComponent(LiveLeftoverOwner.handoffMarkerName)
        XCTAssertEqual(LiveLeftoverOwner.read(marker), getpid(), "the socket directory is marked as this instance's")
        XCTAssertEqual(handoff.token.count, 64)
        let ownPID = getpid()
        let serving = Task { try await handoff.serve(descriptor: descriptor, expectedPeer: { ownPID }, deadline: 5) }

        let received = try Self.receive(from: handoff.socketPath, token: handoff.token)
        defer { close(received) }
        try await serving.value
        var expected = stat()
        var actual = stat()
        XCTAssertEqual(fstat(descriptor, &expected), 0)
        XCTAssertEqual(fstat(received, &actual), 0)
        XCTAssertEqual(actual.st_dev, expected.st_dev)
        XCTAssertEqual(actual.st_ino, expected.st_ino)
        XCTAssertTrue(handoff.isFinished)
        XCTAssertNotEqual(access(handoff.socketPath, F_OK), 0)
    }

    func testAPeerThatIsNotTheWorkerGetsNothingAndTheDeadlineClosesTheListener() async throws {
        let source = directory.appendingPathComponent("Disc.iso")
        try Data(repeating: 7, count: 512).write(to: source)
        let descriptor = open(source.path, O_RDONLY | O_CLOEXEC)
        defer { close(descriptor) }
        let handoff = try LiveSourceHandoff()
        // pid 1 is never this test process or in its process group.
        let serving = Task { try await handoff.serve(descriptor: descriptor, expectedPeer: { 1 }, deadline: 0.5) }

        XCTAssertThrowsError(try Self.receive(from: handoff.socketPath, token: handoff.token))
        do {
            try await serving.value
            XCTFail("Expected the handoff to time out")
        } catch {
            XCTAssertEqual(error as? LiveSourceHandoffError, .timedOut)
        }
        XCTAssertTrue(handoff.isFinished)
        XCTAssertThrowsError(try Self.connect(to: handoff.socketPath), "the listener is closed")
    }

    func testAWrongTokenEndsTheHandoffWithNothingSent() async throws {
        let source = directory.appendingPathComponent("Disc.iso")
        try Data(repeating: 7, count: 512).write(to: source)
        let descriptor = open(source.path, O_RDONLY | O_CLOEXEC)
        defer { close(descriptor) }
        let handoff = try LiveSourceHandoff()
        let ownPID = getpid()
        let serving = Task { try await handoff.serve(descriptor: descriptor, expectedPeer: { ownPID }, deadline: 5) }

        XCTAssertThrowsError(try Self.receive(from: handoff.socketPath, token: String(repeating: "0", count: 64)))
        do {
            try await serving.value
            XCTFail("Expected the handoff to be refused")
        } catch {
            XCTAssertEqual(error as? LiveSourceHandoffError, .refused)
        }
        XCTAssertTrue(handoff.isFinished)
    }

    private struct ClientFailure: Error {}

    private static func connect(to path: String) throws -> Int32 {
        let client = socket(AF_UNIX, SOCK_STREAM, 0)
        guard client >= 0 else { throw ClientFailure() }
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: Array(path.utf8)) }
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        let connected = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(client, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard connected == 0 else {
            close(client)
            throw ClientFailure()
        }
        var timeout = timeval(tv_sec: 2, tv_usec: 0)
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        return client
    }

    /// Plays the worker's side: send the token, then take one byte and one `SCM_RIGHTS` descriptor.
    private static func receive(from path: String, token: String) throws -> Int32 {
        let client = try connect(to: path)
        defer { close(client) }
        let bytes = Array(token.utf8)
        guard bytes.withUnsafeBytes({ write(client, $0.baseAddress, $0.count) }) == bytes.count else { throw ClientFailure() }
        var payload: UInt8 = 0
        var control = [UInt8](repeating: 0, count: 64)
        let received: Int = withUnsafeMutablePointer(to: &payload) { payloadPointer in
            var vector = iovec(iov_base: UnsafeMutableRawPointer(payloadPointer), iov_len: 1)
            return control.withUnsafeMutableBytes { controlRaw in
                withUnsafeMutablePointer(to: &vector) { vectorPointer in
                    var message = msghdr(
                        msg_name: nil, msg_namelen: 0, msg_iov: vectorPointer, msg_iovlen: 1,
                        msg_control: controlRaw.baseAddress, msg_controllen: socklen_t(controlRaw.count), msg_flags: 0
                    )
                    let count = recvmsg(client, &message, 0)
                    return count == 1 && message.msg_controllen >= 16 ? count : -1
                }
            }
        }
        guard received == 1 else { throw ClientFailure() }
        let header = control.withUnsafeBytes { $0.loadUnaligned(as: cmsghdr.self) }
        guard header.cmsg_level == SOL_SOCKET, header.cmsg_type == SCM_RIGHTS else { throw ClientFailure() }
        return control.withUnsafeBytes { $0.loadUnaligned(fromByteOffset: MemoryLayout<cmsghdr>.size, as: Int32.self) }
    }
}

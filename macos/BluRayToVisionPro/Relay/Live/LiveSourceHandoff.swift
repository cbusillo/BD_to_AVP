import Darwin
import Foundation
import Security

/// A source retainer that can lend its open descriptor to a worker.
protocol LiveSourceDescriptorHolding: AnyObject, Sendable {
    /// The descriptor the Mac opened under the approved folder. Valid while the holder lives.
    var descriptor: Int32 { get }
}

enum LiveSourceHandoffError: Error, Equatable {
    case unavailable
    case timedOut
    case refused
}

/// Hands the Mac's open source descriptor to one worker over a one-shot Unix socket.
///
/// Foundation's `Process` passes only stdio, so a descriptor cannot be inherited at spawn. The
/// app opens the ISO itself under the approved folder's security scope, where its consent
/// applies, and the worker reads that exact open file through `/dev/fd/N`.
///
/// - The socket lives in a fresh 0700 directory from `mkdtemp("$TMPDIR/bdtoavp-XXXXXX")`,
///   which keeps `sun_path` under 104 bytes.
/// - Only a peer whose `LOCAL_PEERPID` is the worker (or in the worker's process group) is read;
///   any other connection is closed unread and does not use up the handoff.
/// - That peer must send the 64-character token from the job spec. Then the Mac sends one byte
///   with the descriptor as `SCM_RIGHTS`. A wrong token ends the handoff with nothing sent.
/// - The whole exchange has a deadline. Afterwards, on any outcome, the listener is closed and
///   the directory removed. The lease keeps its own descriptor either way.
final class LiveSourceHandoff: @unchecked Sendable {
    static let tokenLength = 64

    let socketPath: String
    let token: String
    private let directory: String
    private let lock = NSLock()
    private var listener: Int32
    private var isClosed = false

    init(temporaryDirectory: String = NSTemporaryDirectory()) throws {
        var template = Array((temporaryDirectory.hasSuffix("/") ? temporaryDirectory : temporaryDirectory + "/").utf8CString.dropLast())
        template.append(contentsOf: Array("bdtoavp-XXXXXX".utf8CString))
        guard let created = mkdtemp(&template) else { throw LiveSourceHandoffError.unavailable }
        directory = String(cString: created)
        _ = chmod(directory, 0o700)
        socketPath = directory + "/s"
        var tokenBytes = [UInt8](repeating: 0, count: Self.tokenLength / 2)
        guard SecRandomCopyBytes(kSecRandomDefault, tokenBytes.count, &tokenBytes) == errSecSuccess else {
            rmdir(directory)
            throw LiveSourceHandoffError.unavailable
        }
        token = tokenBytes.map { String(format: "%02x", $0) }.joined()
        listener = socket(AF_UNIX, SOCK_STREAM, 0)
        guard listener >= 0 else {
            rmdir(directory)
            throw LiveSourceHandoffError.unavailable
        }
        _ = fcntl(listener, F_SETFD, FD_CLOEXEC)
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(socketPath.utf8)
        guard pathBytes.count < MemoryLayout.size(ofValue: address.sun_path) else {
            Darwin.close(listener)
            rmdir(directory)
            throw LiveSourceHandoffError.unavailable
        }
        withUnsafeMutableBytes(of: &address.sun_path) { raw in
            raw.copyBytes(from: pathBytes)
        }
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(listener, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bound == 0, listen(listener, 1) == 0 else {
            Darwin.close(listener)
            unlink(socketPath)
            rmdir(directory)
            throw LiveSourceHandoffError.unavailable
        }
    }

    deinit {
        close()
    }

    var jobField: WorkerJobSpec.Source.DescriptorHandoff {
        WorkerJobSpec.Source.DescriptorHandoff(socketPath: socketPath, token: token)
    }

    /// Whether the listener is closed and the socket directory removed.
    var isFinished: Bool {
        lock.withLock { isClosed } && access(directory, F_OK) != 0
    }

    /// Closes the listener and removes the socket directory. Idempotent.
    func close() {
        let descriptor: Int32? = lock.withLock {
            guard !isClosed else { return nil }
            isClosed = true
            defer { listener = -1 }
            return listener
        }
        guard let descriptor else { return }
        Darwin.close(descriptor)
        unlink(socketPath)
        rmdir(directory)
    }

    /// Serves exactly one worker, then closes. `expectedPeer` returns the worker's pid once it
    /// has launched. Blocking socket work runs off the caller's executor.
    func serve(
        descriptor: Int32,
        expectedPeer: @escaping @Sendable () -> pid_t?,
        deadline: TimeInterval = 10
    ) async throws {
        let end = Date().addingTimeInterval(deadline)
        let result: Result<Void, LiveSourceHandoffError> = await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                continuation.resume(returning: self.exchange(descriptor: descriptor, expectedPeer: expectedPeer, until: end))
            }
        }
        close()
        try result.get()
    }

    private func currentListener() -> Int32? {
        lock.withLock { isClosed ? nil : listener }
    }

    private func exchange(descriptor: Int32, expectedPeer: () -> pid_t?, until end: Date) -> Result<Void, LiveSourceHandoffError> {
        while true {
            guard let listener = currentListener() else { return .failure(.refused) }
            guard Self.waitReadable(listener, until: end) else { return .failure(.timedOut) }
            let connection = accept(listener, nil, nil)
            guard connection >= 0 else {
                if errno == EINTR || errno == EAGAIN || errno == ECONNABORTED { continue }
                return .failure(.unavailable)
            }
            defer { Darwin.close(connection) }
            _ = fcntl(connection, F_SETFD, FD_CLOEXEC)
            var noSignal: Int32 = 1
            setsockopt(connection, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
            guard let peer = Self.peerPID(connection), let worker = expectedPeer(),
                  peer == worker || getpgid(peer) == worker
            else {
                // Not the worker: close it unread and keep waiting for the worker.
                continue
            }
            guard let received = Self.readExactly(connection, count: Self.tokenLength, until: end),
                  received.count == Self.tokenLength,
                  Self.constantTimeEqual(received, Array(token.utf8))
            else {
                return .failure(.refused)
            }
            return Self.send(descriptor: descriptor, over: connection) ? .success(()) : .failure(.unavailable)
        }
    }

    private static func peerPID(_ connection: Int32) -> pid_t? {
        var pid: pid_t = 0
        var length = socklen_t(MemoryLayout<pid_t>.size)
        guard getsockopt(connection, SOL_LOCAL, LOCAL_PEERPID, &pid, &length) == 0, pid > 0 else { return nil }
        return pid
    }

    private static func waitReadable(_ descriptor: Int32, until end: Date) -> Bool {
        while true {
            let remaining = end.timeIntervalSinceNow
            guard remaining > 0 else { return false }
            var poller = pollfd(fd: descriptor, events: Int16(POLLIN), revents: 0)
            let ready = poll(&poller, 1, Int32(min(remaining, 0.2) * 1_000) + 1)
            if ready > 0 { return true }
            if ready < 0, errno != EINTR { return false }
        }
    }

    private static func readExactly(_ connection: Int32, count: Int, until end: Date) -> [UInt8]? {
        var buffer = [UInt8](repeating: 0, count: count)
        var filled = 0
        while filled < count {
            guard waitReadable(connection, until: end) else { return nil }
            let read = buffer.withUnsafeMutableBytes { raw in
                Darwin.read(connection, raw.baseAddress! + filled, count - filled)
            }
            if read < 0, errno == EINTR { continue }
            guard read > 0 else { return nil }
            filled += read
        }
        return buffer
    }

    private static func constantTimeEqual(_ left: [UInt8], _ right: [UInt8]) -> Bool {
        guard left.count == right.count else { return false }
        return zip(left, right).reduce(UInt8(0)) { $0 | ($1.0 ^ $1.1) } == 0
    }

    /// One data byte with the descriptor as `SCM_RIGHTS`. On Darwin, `cmsghdr` is 12 bytes with
    /// 4-byte alignment, so `CMSG_LEN(sizeof(int))` and `CMSG_SPACE(sizeof(int))` are both 16
    /// and the data starts at offset 12.
    private static func send(descriptor: Int32, over connection: Int32) -> Bool {
        let headerLength = MemoryLayout<cmsghdr>.size
        let controlLength = headerLength + MemoryLayout<Int32>.size
        var control = [UInt8](repeating: 0, count: controlLength)
        control.withUnsafeMutableBytes { raw in
            var header = cmsghdr(
                cmsg_len: socklen_t(controlLength),
                cmsg_level: SOL_SOCKET,
                cmsg_type: SCM_RIGHTS
            )
            withUnsafeBytes(of: &header) { raw.copyMemory(from: $0) }
            var value = descriptor
            withUnsafeBytes(of: &value) { (raw.baseAddress! + headerLength).copyMemory(from: $0.baseAddress!, byteCount: $0.count) }
        }
        var payload: UInt8 = 0
        return withUnsafeMutablePointer(to: &payload) { payloadPointer in
            var vector = iovec(iov_base: UnsafeMutableRawPointer(payloadPointer), iov_len: 1)
            return control.withUnsafeMutableBytes { controlRaw in
                withUnsafeMutablePointer(to: &vector) { vectorPointer in
                    var message = msghdr(
                        msg_name: nil,
                        msg_namelen: 0,
                        msg_iov: vectorPointer,
                        msg_iovlen: 1,
                        msg_control: controlRaw.baseAddress,
                        msg_controllen: socklen_t(controlLength),
                        msg_flags: 0
                    )
                    while true {
                        let sent = sendmsg(connection, &message, 0)
                        if sent < 0, errno == EINTR { continue }
                        return sent == 1
                    }
                }
            }
        }
    }
}

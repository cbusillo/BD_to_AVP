import Darwin
import dnssd
import Foundation

/// BSD sockets keep LAN media on the kernel TCP path. The Network framework
/// path reproduced large transfer delays on the physical reference Mac.
final class RelaySocketListener: @unchecked Sendable {
    let port: UInt16
    private let descriptor: Int32
    private let queue: DispatchQueue
    private let queueKey = DispatchSpecificKey<Void>()
    private var source: DispatchSourceRead?
    private var service: DNSServiceRef?
    private var cancelled = false
    private var failure: (@Sendable () -> Void)?

    init(queue: DispatchQueue) throws {
        self.queue = queue
        let descriptor = socket(AF_INET6, SOCK_STREAM, 0)
        self.descriptor = descriptor
        guard descriptor >= 0 else { throw Self.socketError() }
        do {
            var off: Int32 = 0
            var on: Int32 = 1
            guard setsockopt(descriptor, IPPROTO_IPV6, IPV6_V6ONLY, &off, socklen_t(MemoryLayout.size(ofValue: off))) == 0,
                  setsockopt(descriptor, SOL_SOCKET, SO_REUSEADDR, &on, socklen_t(MemoryLayout.size(ofValue: on))) == 0,
                  fcntl(descriptor, F_SETFL, O_NONBLOCK) == 0 else { throw Self.socketError() }
            var address = sockaddr_in6()
            address.sin6_len = UInt8(MemoryLayout<sockaddr_in6>.size)
            address.sin6_family = sa_family_t(AF_INET6)
            let bound = withUnsafePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in6>.size))
                }
            }
            guard bound == 0, listen(descriptor, 16) == 0 else { throw Self.socketError() }
            var length = socklen_t(MemoryLayout<sockaddr_in6>.size)
            let named = withUnsafeMutablePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(descriptor, $0, &length) }
            }
            guard named == 0 else { throw Self.socketError() }
            port = UInt16(bigEndian: address.sin6_port)
        } catch {
            Darwin.close(descriptor)
            throw error
        }
        queue.setSpecific(key: queueKey, value: ())
    }

    func start(
        name: String, type: String, txtRecord: Data,
        accept: @escaping @Sendable (RelaySocketConnection, String) -> Void,
        failure: @escaping @Sendable () -> Void
    ) throws {
        try queue.sync {
            self.failure = failure
            let result = txtRecord.withUnsafeBytes { bytes in
                DNSServiceRegister(
                    &service, 0, 0, name, type, "local.", nil, port.bigEndian,
                    UInt16(bytes.count), bytes.baseAddress,
                    { _, _, error, _, _, _, context in
                        guard error != kDNSServiceErr_NoError, let context else { return }
                        let listener = Unmanaged<RelaySocketListener>.fromOpaque(context).takeUnretainedValue()
                        listener.failure?()
                    }, Unmanaged.passUnretained(self).toOpaque()
                )
            }
            guard result == kDNSServiceErr_NoError, let service else {
                throw NSError(domain: "RelayBonjour", code: Int(result))
            }
            let scheduled = DNSServiceSetDispatchQueue(service, queue)
            guard scheduled == kDNSServiceErr_NoError else {
                DNSServiceRefDeallocate(service)
                self.service = nil
                throw NSError(domain: "RelayBonjour", code: Int(scheduled))
            }
            let source = DispatchSource.makeReadSource(fileDescriptor: descriptor, queue: queue)
            source.setEventHandler { [weak self] in self?.acceptAvailable(accept) }
            source.setCancelHandler { [descriptor] in Darwin.close(descriptor) }
            self.source = source
            source.resume()
        }
    }

    func cancel() {
        if DispatchQueue.getSpecific(key: queueKey) != nil { cancelOnQueue() }
        else { queue.sync { cancelOnQueue() } }
    }

    private func cancelOnQueue() {
        guard !cancelled else { return }
        cancelled = true
        if let service { DNSServiceRefDeallocate(service); self.service = nil }
        failure = nil
        if let source { source.cancel(); self.source = nil }
        else { Darwin.close(descriptor) }
    }

    private func acceptAvailable(_ handler: @Sendable (RelaySocketConnection, String) -> Void) {
        guard !cancelled else { return }
        // Yield between batches so admission and timeout work gets queue time.
        for _ in 0..<16 {
            var address = sockaddr_storage()
            var length = socklen_t(MemoryLayout<sockaddr_storage>.size)
            let accepted = withUnsafeMutablePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.accept(descriptor, $0, &length) }
            }
            guard accepted >= 0 else {
                if errno == EINTR { continue }
                if errno != EAGAIN, errno != EWOULDBLOCK { failure?() }
                return
            }
            var name = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            let result = withUnsafePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    getnameinfo($0, length, &name, socklen_t(name.count), nil, 0, NI_NUMERICHOST)
                }
            }
            guard result == 0 else { Darwin.close(accepted); continue }
            var host = String(cString: name)
            if host.hasPrefix("::ffff:") { host = String(host.dropFirst(7)) }
            do { handler(try RelaySocketConnection(descriptor: accepted), host) }
            catch { Darwin.close(accepted) }
        }
    }

    private static func socketError() -> POSIXError { POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }

    deinit { cancel() }
}

/// At most sixteen admitted connections each use one serial I/O queue. Shutdown
/// interrupts a blocked read/write; close runs on that queue after I/O returns,
/// preventing descriptor reuse races. Socket timeouts bound idle operations.
final class RelaySocketConnection: @unchecked Sendable {
    private let descriptor: Int32
    private let queue = DispatchQueue(label: "com.shinycomputers.bd-to-avp.relay.socket", qos: .userInitiated)
    private let lock = NSLock()
    private var cancelled = false

    init(descriptor: Int32) throws {
        var timeout = timeval(tv_sec: 10, tv_usec: 0)
        var on: Int32 = 1
        guard fcntl(descriptor, F_SETFL, 0) == 0,
              setsockopt(descriptor, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout.size(ofValue: on))) == 0,
              setsockopt(descriptor, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout))) == 0,
              setsockopt(descriptor, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout))) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        self.descriptor = descriptor
    }

    func receive(_ completion: @escaping @Sendable (Data?, Error?) -> Void) {
        queue.async { [self] in
            guard !lock.withLock({ cancelled }) else { completion(nil, CancellationError()); return }
            var bytes = [UInt8](repeating: 0, count: 64 * 1_024)
            var count: Int
            repeat { count = recv(descriptor, &bytes, bytes.count, 0) } while count < 0 && errno == EINTR
            if count > 0 { completion(Data(bytes.prefix(count)), nil) }
            else if count == 0 { completion(nil, nil) }
            else { completion(nil, POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)) }
        }
    }

    func send(_ data: Data, completion: @escaping @Sendable (Error?) -> Void) {
        queue.async { [self] in
            guard !lock.withLock({ cancelled }) else { completion(CancellationError()); return }
            var offset = 0
            while offset < data.count {
                let count = data.withUnsafeBytes { bytes in
                    Darwin.send(descriptor, bytes.baseAddress!.advanced(by: offset), data.count - offset, 0)
                }
                if count > 0 { offset += count }
                else if count < 0, errno == EINTR { continue }
                else { completion(POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)); return }
            }
            completion(nil)
        }
    }

    func cancel() {
        let shouldClose = lock.withLock {
            guard !cancelled else { return false }
            cancelled = true
            shutdown(descriptor, SHUT_RDWR)
            return true
        }
        if shouldClose { queue.async { [self] in Darwin.close(descriptor) } }
    }

    deinit { if !cancelled { Darwin.close(descriptor) } }
}

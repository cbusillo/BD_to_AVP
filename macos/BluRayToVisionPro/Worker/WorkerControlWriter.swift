import Darwin
import Foundation

enum WorkerControlError: Error, LocalizedError {
    case unavailable
    case queueFull
    case invalidFrame

    var errorDescription: String? {
        switch self {
        case .unavailable: "This conversion can no longer accept the request."
        case .queueFull: "Another conversion request is still being sent."
        case .invalidFrame: "The conversion request could not be prepared."
        }
    }
}

/// Each control is smaller than PIPE_BUF and is written atomically without
/// blocking. All file operations run off the main thread, in one serial queue.
final class WorkerControlWriter: @unchecked Sendable {
    private let handle: FileHandle
    private let queue = DispatchQueue(label: "com.shinycomputers.bd-to-avp.worker-control", qos: .utility)
    private let lock = NSLock()
    private var closed = false
    private var pendingWrites = 0

    init(handle: FileHandle) throws {
        self.handle = handle
        let descriptor = handle.fileDescriptor
        guard fcntl(descriptor, F_SETNOSIGPIPE, 1) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        let flags = fcntl(descriptor, F_GETFL)
        guard flags >= 0, fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
    }

    func send(_ command: WorkerWaitCommand) async throws {
        var data = try JSONEncoder().encode(command)
        data.append(0x0A)
        guard data.count <= 512 else { throw WorkerControlError.invalidFrame }
        let accepted = lock.withLock {
            guard !closed, pendingWrites < 8 else { return false }
            pendingWrites += 1
            return true
        }
        guard accepted else { throw WorkerControlError.queueFull }
        let frame = data
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            queue.async {
                defer { self.lock.withLock { self.pendingWrites -= 1 } }
                do {
                    guard !self.lock.withLock({ self.closed }) else {
                        throw WorkerControlError.unavailable
                    }
                    // Foundation's FileHandle.write retries EAGAIN internally. A
                    // single nonblocking syscall keeps a full pipe bounded.
                    let written = frame.withUnsafeBytes {
                        Darwin.write(self.handle.fileDescriptor, $0.baseAddress, $0.count)
                    }
                    guard written >= 0 else {
                        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
                    }
                    guard written == frame.count else {
                        self.close()
                        throw WorkerControlError.invalidFrame
                    }
                    continuation.resume()
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    func close() {
        let shouldClose = lock.withLock {
            guard !closed else { return false }
            closed = true
            return true
        }
        if shouldClose {
            queue.async { try? self.handle.close() }
        }
    }
}

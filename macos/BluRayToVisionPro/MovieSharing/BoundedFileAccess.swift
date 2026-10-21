import Foundation

/// Runs blocking file-system work on a background queue and gives up after a deadline.
///
/// Source folders can sit on a network share, where `open`, `stat`, or bookmark resolution may
/// wait indefinitely (for example on macOS network-volume consent). No source I/O may run on the
/// main actor or on an actor a request waits on; it goes through here instead. A result that
/// arrives after the deadline is dropped, which releases any descriptor or root it holds.
enum BoundedFileAccess {
    static let defaultDeadline: TimeInterval = 10

    /// The work's result, or nil when it did not finish within `deadline`.
    static func run<T: Sendable>(
        deadline: TimeInterval = defaultDeadline,
        _ work: @escaping @Sendable () -> T
    ) async -> T? {
        let outcome = Outcome<T>()
        return await withCheckedContinuation { continuation in
            outcome.install(continuation)
            DispatchQueue.global(qos: .userInitiated).async {
                outcome.finish(work())
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + deadline) {
                outcome.finish(nil)
            }
        }
    }

    private final class Outcome<T: Sendable>: @unchecked Sendable {
        private let lock = NSLock()
        private var continuation: CheckedContinuation<T?, Never>?
        private var isFinished = false

        func install(_ continuation: CheckedContinuation<T?, Never>) {
            lock.withLock { self.continuation = continuation }
        }

        func finish(_ value: T?) {
            let continuation: CheckedContinuation<T?, Never>? = lock.withLock {
                guard !isFinished else { return nil }
                isFinished = true
                defer { self.continuation = nil }
                return self.continuation
            }
            continuation?.resume(returning: value)
        }
    }
}

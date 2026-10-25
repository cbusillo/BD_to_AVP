import Foundation
import Network

/// Watches the reads of a movie that lives outside the app, such as on an SMB share, so the
/// player can say the connection is lost instead of freezing silently, and keeps those reads
/// going through a dropped connection instead of failing playback (#1131).
///
/// A read that fails is tried again from the same place every `retryInterval` until it works
/// or the monitor is cancelled; a read that hangs simply blocks. Once a read has gone
/// unanswered for `lostAfter` seconds, or the headset has no network while the movie is on a
/// network volume, the connection counts as lost until reads complete again. Thread-safe.
final class ShareConnectionMonitor: @unchecked Sendable {
    struct Timing: Equatable {
        var lostAfter: TimeInterval = 2
        var retryInterval: TimeInterval = 0.5
    }

    let timing: Timing

    private let now: @Sendable () -> TimeInterval
    private let condition = NSCondition()
    // Guarded by `condition`.
    /// When each unfinished read started, including the time its retries took.
    private var readStarts: [Int: TimeInterval] = [:]
    private var nextReadID = 0
    private var isNetworkUnavailable = false
    private var cancelled = false
    private var pathMonitor: NWPathMonitor?

    init(
        timing: Timing = Timing(),
        now: @escaping @Sendable () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }
    ) {
        self.timing = timing
        self.now = now
    }

    deinit {
        pathMonitor?.cancel()
    }

    /// Also counts the connection as lost while the headset has no usable network path. Only
    /// for a movie on a network volume; a movie stored on the headset plays without one.
    func watchNetwork() {
        let monitor = NWPathMonitor()
        monitor.pathUpdateHandler = { [weak self] path in
            self?.setNetworkAvailable(path.status == .satisfied)
        }
        condition.withLock {
            guard !cancelled, pathMonitor == nil else { return }
            pathMonitor = monitor
            monitor.start(queue: DispatchQueue(label: "com.shinycomputers.bd-to-avp.share-network"))
        }
    }

    func setNetworkAvailable(_ isAvailable: Bool) {
        condition.withLock { isNetworkUnavailable = !isAvailable }
    }

    var isConnectionLost: Bool {
        condition.withLock {
            guard !cancelled else { return false }
            if isNetworkUnavailable { return true }
            guard let oldest = readStarts.values.min() else { return false }
            return now() - oldest >= timing.lostAfter
        }
    }

    var isCancelled: Bool { condition.withLock { cancelled } }

    /// Stops retrying; a read waiting to retry gives up with its last error.
    func cancel() {
        condition.withLock {
            cancelled = true
            pathMonitor?.cancel()
            pathMonitor = nil
            condition.broadcast()
        }
    }

    /// Runs `body`, trying it again after each failure until it succeeds or the monitor is
    /// cancelled. `body` must start from the same place each time.
    func read<T>(_ body: () throws -> T) throws -> T {
        let id = beginRead()
        defer { endRead(id) }
        while true {
            do {
                return try body()
            } catch {
                guard waitToRetry() else { throw error }
            }
        }
    }

    /// Marks the start of a read, for callers that retry on their own, as the disc reader does.
    func beginRead() -> Int {
        condition.withLock {
            nextReadID += 1
            readStarts[nextReadID] = now()
            return nextReadID
        }
    }

    func endRead(_ id: Int) {
        condition.withLock { _ = readStarts.removeValue(forKey: id) }
    }

    /// Waits before a failed read is tried again. Returns false once cancelled.
    func waitToRetry() -> Bool {
        condition.lock()
        defer { condition.unlock() }
        guard !cancelled else { return false }
        _ = condition.wait(until: Date(timeIntervalSinceNow: timing.retryInterval))
        return !cancelled
    }
}

extension ShareConnectionMonitor {
    /// A monitor for a movie outside the app's own storage, or nil for one inside it, whose
    /// read errors are real failures. The network is watched only when the system reports the
    /// movie's volume as remote, so a movie on the headset never says its connection is lost.
    static func forMovie(at url: URL) -> ShareConnectionMonitor? {
        guard !isInsideAppStorage(url) else { return nil }
        let monitor = ShareConnectionMonitor()
        if (try? url.resourceValues(forKeys: [.volumeIsLocalKey]))?.volumeIsLocal == false {
            monitor.watchNetwork()
        }
        return monitor
    }

    static func isInsideAppStorage(_ url: URL, home: URL = URL(fileURLWithPath: NSHomeDirectory())) -> Bool {
        let homePath = home.resolvingSymlinksInPath().path
        let path = url.resolvingSymlinksInPath().path
        return path == homePath || path.hasPrefix(homePath.hasSuffix("/") ? homePath : homePath + "/")
    }
}

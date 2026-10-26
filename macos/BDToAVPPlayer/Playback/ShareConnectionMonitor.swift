import Foundation
import Network

/// Watches the reads of a movie that lives outside the app, such as on an SMB share, so the
/// player can say the connection is lost instead of freezing silently, and keeps those reads
/// going through a dropped connection instead of failing playback (#1131).
///
/// A read that fails with an error a reconnect can cure is tried again from the same place every
/// `retryInterval`. It stops when it works, when its caller no longer wants it, when the monitor
/// is cancelled, or when it has kept failing for `giveUpAfter` seconds while the headset has a
/// network. Time without any network does not count, so Wi-Fi can stay off as long as it likes.
/// An error no reconnect can cure, such as lost permission or a missing file, fails at once. A
/// read that hangs simply blocks. Once a read has gone unanswered for `lostAfter` seconds, or the
/// headset has no network while the movie is on a network volume, the connection counts as lost
/// until reads complete again. Thread-safe.
final class ShareConnectionMonitor: @unchecked Sendable {
    struct Timing: Equatable {
        var lostAfter: TimeInterval = 2
        var retryInterval: TimeInterval = 0.5
        var giveUpAfter: TimeInterval = 60
    }

    /// Why a share read stopped being retried, in words the player can show.
    enum ReadError: Error, Equatable, LocalizedError {
        case stoppedAnswering

        var errorDescription: String? {
            "The movie's share stopped answering, so playback stopped. Check the connection, then try again."
        }
    }

    /// One failing read's retry history, for callers that retry on their own.
    struct Retry {
        fileprivate var failingSince: TimeInterval?
        fileprivate(set) var gaveUp = false
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

    /// Watches the network when the system reports `url`'s volume as remote. It reads the file's
    /// volume, so call it off the main thread.
    func watchNetworkIfRemote(_ url: URL) {
        if (try? url.resourceValues(forKeys: [.volumeIsLocalKey]))?.volumeIsLocal == false {
            watchNetwork()
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

    /// Runs `body`, trying it again after each curable failure while `isWanted` holds, until it
    /// succeeds, the monitor is cancelled, or it gives up. `body` must start from the same place
    /// each time. A read that gives up throws `ReadError.stoppedAnswering`; one that fails for
    /// good, is no longer wanted, or is cancelled throws its own error.
    func read<T>(while isWanted: () -> Bool = { true }, _ body: () throws -> T) throws -> T {
        let id = beginRead()
        defer { endRead(id) }
        var retry = Retry()
        while true {
            do {
                return try body()
            } catch {
                guard isWanted(), waitToRetry(after: error, &retry), isWanted() else {
                    throw retry.gaveUp ? ReadError.stoppedAnswering : error
                }
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

    /// Decides what follows a failed read: waits and returns true to try it again, or returns
    /// false when the error cannot be cured, the monitor is cancelled, or the read has failed for
    /// `giveUpAfter` seconds with a network up, which sets `retry.gaveUp`.
    func waitToRetry(after error: Error, _ retry: inout Retry) -> Bool {
        guard !Self.isPermanent(error) else { return false }
        condition.lock()
        defer { condition.unlock() }
        guard !cancelled else { return false }
        let time = now()
        // Only failures while a network is up count toward giving up.
        if isNetworkUnavailable || retry.failingSince == nil {
            retry.failingSince = time
        } else if let since = retry.failingSince, time - since >= timing.giveUpAfter {
            retry.gaveUp = true
            return false
        }
        _ = condition.wait(until: Date(timeIntervalSinceNow: timing.retryInterval))
        return !cancelled
    }

    /// Errors a reconnect cannot cure: the file is gone, unreadable, or not a file.
    static func isPermanent(_ error: Error) -> Bool {
        let permanent: Set<Int32> = [EACCES, EPERM, ENOENT, ENOTDIR, EISDIR, EBADF, EINVAL, EROFS, ENAMETOOLONG]
        let nsError = error as NSError
        if nsError.domain == NSPOSIXErrorDomain {
            return permanent.contains(Int32(nsError.code))
        }
        if nsError.domain == NSCocoaErrorDomain {
            switch CocoaError.Code(rawValue: nsError.code) {
            case .fileReadNoPermission, .fileReadNoSuchFile, .fileNoSuchFile, .fileReadInvalidFileName:
                return true
            default:
                break
            }
            if let underlying = nsError.userInfo[NSUnderlyingErrorKey] as? Error {
                return isPermanent(underlying)
            }
        }
        return false
    }
}

extension ShareConnectionMonitor {
    /// A monitor for a movie outside the app's own storage, or nil for one inside it, whose
    /// read errors are real failures. This only looks at the path, so the session can hold the
    /// monitor, and cancel it, before anything touches the share; call `watchNetworkIfRemote`
    /// once off the main thread.
    static func forMovie(at url: URL) -> ShareConnectionMonitor? {
        isInsideAppStorage(url) ? nil : ShareConnectionMonitor()
    }

    /// Compares paths without touching the movie's file, which may be on a share that is down.
    static func isInsideAppStorage(_ url: URL, home: URL = URL(fileURLWithPath: NSHomeDirectory())) -> Bool {
        // `/var` is a link to `/private/var`, and either spelling can reach the app.
        func normalized(_ path: String) -> String {
            path.hasPrefix("/private/") ? String(path.dropFirst("/private".count)) : path
        }
        let homePath = normalized(home.standardizedFileURL.path)
        let path = normalized(url.standardizedFileURL.path)
        return path == homePath || path.hasPrefix(homePath.hasSuffix("/") ? homePath : homePath + "/")
    }
}

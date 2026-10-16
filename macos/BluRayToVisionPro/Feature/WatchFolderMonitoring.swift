import CoreServices
import Foundation

/// What a watch folder is doing right now, as shown in Settings.
enum WatchFolderStatus: Equatable {
    /// The folder has not been checked yet in this session.
    case checking
    case watching
    /// The person turned the folder off.
    case paused
    /// The folder cannot be reached, for example because its drive is disconnected.
    case offline
}

/// Whether newly admitted, automatically started items may start the queue.
enum AutomaticQueueStartDecision: Equatable {
    case startQueue
    case joinRunningQueue
    case leaveWaiting(AutomaticQueueStartHold)

    /// Never overrides a pause, a requested pause, a scheduled window, or other active work.
    static func decide(
        runState: PersistentQueueRunState,
        hasOffPeakSchedule: Bool,
        otherWorkIsActive: Bool
    ) -> AutomaticQueueStartDecision {
        switch runState {
        case .running:
            return .joinRunningQueue
        case .pauseAfterCurrent:
            return .leaveWaiting(.pauseRequested)
        case .paused:
            return .leaveWaiting(.paused)
        case .idle:
            if hasOffPeakSchedule {
                return .leaveWaiting(.scheduled)
            }
            if otherWorkIsActive {
                return .leaveWaiting(.otherWorkActive)
            }
            return .startQueue
        }
    }
}

enum AutomaticQueueStartHold: Equatable {
    case paused
    case pauseRequested
    case scheduled
    case otherWorkActive
}

/// Reports file-system changes inside one watch folder until it is stopped.
@MainActor
protocol WatchFolderEventSource: AnyObject {
    func stop()
}

/// Creates an event source for a folder, calling `onChange` on the main actor after changes inside it.
typealias WatchFolderEventSourceFactory = @MainActor (
    _ directoryURL: URL,
    _ onChange: @escaping @MainActor () -> Void
) -> (any WatchFolderEventSource)?

/// A pending timed action that can be cancelled.
@MainActor
protocol QueueAutomationTimer: AnyObject {
    func cancel()
}

/// Runs an action at a wall-clock time; tests substitute a manual scheduler.
@MainActor
protocol QueueAutomationTimerScheduling: AnyObject {
    func schedule(at date: Date, _ action: @escaping @MainActor () -> Void) -> any QueueAutomationTimer
}

/// Schedules timed re-scans with task sleeps on the continuous clock, which keeps counting while the Mac sleeps.
@MainActor
final class TaskQueueAutomationTimerScheduler: QueueAutomationTimerScheduling {
    private final class TaskTimer: QueueAutomationTimer {
        var task: Task<Void, Never>?

        func cancel() {
            task?.cancel()
            task = nil
        }
    }

    private let now: () -> Date

    init(now: @escaping () -> Date = Date.init) {
        self.now = now
    }

    func schedule(at date: Date, _ action: @escaping @MainActor () -> Void) -> any QueueAutomationTimer {
        let timer = TaskTimer()
        let delay = max(0, date.timeIntervalSince(now()))
        timer.task = Task { @MainActor in
            try? await Task.sleep(for: .seconds(delay))
            guard !Task.isCancelled else {
                return
            }
            action()
        }
        return timer
    }
}

/// Watches a folder tree with FSEvents.
///
/// FSEvents covers every subfolder and does not hold the folder open, so watching never stops a
/// drive from being ejected.
@MainActor
final class FSEventsWatchFolderEventSource: WatchFolderEventSource {
    nonisolated static let defaultLatency: TimeInterval = 2

    private var stream: FSEventStreamRef?
    private let directoryURL: URL
    private let isAccessingSecurityScope: Bool
    private let onChange: @MainActor () -> Void

    init?(directoryURL: URL, latency: TimeInterval = defaultLatency, onChange: @escaping @MainActor () -> Void) {
        self.directoryURL = directoryURL
        self.onChange = onChange
        isAccessingSecurityScope = directoryURL.startAccessingSecurityScopedResource()

        var context = FSEventStreamContext(
            version: 0,
            info: Unmanaged.passUnretained(self).toOpaque(),
            retain: nil,
            release: nil,
            copyDescription: nil
        )
        let callback: FSEventStreamCallback = { _, info, _, _, _, _ in
            guard let info else {
                return
            }
            let source = Unmanaged<FSEventsWatchFolderEventSource>.fromOpaque(info).takeUnretainedValue()
            MainActor.assumeIsolated {
                source.onChange()
            }
        }
        guard let stream = FSEventStreamCreate(
            kCFAllocatorDefault,
            callback,
            &context,
            [directoryURL.path] as CFArray,
            FSEventStreamEventId(kFSEventStreamEventIdSinceNow),
            latency,
            FSEventStreamCreateFlags(kFSEventStreamCreateFlagWatchRoot)
        ) else {
            if isAccessingSecurityScope {
                directoryURL.stopAccessingSecurityScopedResource()
            }
            return nil
        }
        FSEventStreamSetDispatchQueue(stream, DispatchQueue.main)
        guard FSEventStreamStart(stream) else {
            FSEventStreamInvalidate(stream)
            FSEventStreamRelease(stream)
            if isAccessingSecurityScope {
                directoryURL.stopAccessingSecurityScopedResource()
            }
            return nil
        }
        self.stream = stream
    }

    static func make(directoryURL: URL, onChange: @escaping @MainActor () -> Void) -> (any WatchFolderEventSource)? {
        FSEventsWatchFolderEventSource(directoryURL: directoryURL, onChange: onChange)
    }

    func stop() {
        guard let stream else {
            return
        }
        FSEventStreamStop(stream)
        FSEventStreamInvalidate(stream)
        FSEventStreamRelease(stream)
        self.stream = nil
        if isAccessingSecurityScope {
            directoryURL.stopAccessingSecurityScopedResource()
        }
    }

    deinit {
        guard let stream else {
            return
        }
        FSEventStreamStop(stream)
        FSEventStreamInvalidate(stream)
        FSEventStreamRelease(stream)
        if isAccessingSecurityScope {
            directoryURL.stopAccessingSecurityScopedResource()
        }
    }
}

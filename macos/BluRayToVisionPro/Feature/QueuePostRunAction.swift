import Foundation

/// What the Mac does once the queue finishes. Nothing happens until the person chooses an action.
enum QueuePostRunAction: String, Codable, CaseIterable, Identifiable {
    case doNothing
    case sleep
    case shutDown

    var id: String { rawValue }

    var title: String {
        switch self {
        case .doNothing: "Do Nothing"
        case .sleep: "Sleep"
        case .shutDown: "Shut Down"
        }
    }

    /// The phrase after "The Mac will", for countdowns and activity.
    var verbPhrase: String {
        switch self {
        case .doNothing: "do nothing"
        case .sleep: "sleep"
        case .shutDown: "shut down"
        }
    }
}

/// Work outside the queue that a person may be relying on, so the Mac must stay on.
///
/// These are the same activities that make the app ask before it quits.
enum QueuePostRunOtherWork: String, Codable, CaseIterable, Equatable {
    case conversion
    case preview
    case movieSharing
    case relaySession

    var phrase: String {
        switch self {
        case .conversion: "another conversion is running"
        case .preview: "a preview is running"
        case .movieSharing: "Movie Sharing is on"
        case .relaySession: "a relay session is active"
        }
    }
}

/// The parts of the queue the post-run decision reads.
struct QueuePostRunQueueSnapshot: Equatable {
    var runState: PersistentQueueRunState
    var hasPendingStartLaterSchedule: Bool
    var itemStates: [DurableQueueItemState]
    /// A conversion outside the queue, such as a single conversion, is running.
    var hasActiveConversion: Bool

    init(
        runState: PersistentQueueRunState = .idle,
        hasPendingStartLaterSchedule: Bool = false,
        itemStates: [DurableQueueItemState] = [],
        hasActiveConversion: Bool = false
    ) {
        self.runState = runState
        self.hasPendingStartLaterSchedule = hasPendingStartLaterSchedule
        self.itemStates = itemStates
        self.hasActiveConversion = hasActiveConversion
    }
}

/// What the Mac reports about the person and other work at one moment.
struct QueuePostRunConditions: Equatable {
    /// Seconds since the last keyboard, mouse, or trackpad input; `nil` when macOS does not report it.
    var idleTime: TimeInterval?
    var otherWork: [QueuePostRunOtherWork]

    init(idleTime: TimeInterval?, otherWork: [QueuePostRunOtherWork] = []) {
        self.idleTime = idleTime
        self.otherWork = otherWork
    }
}

/// Why a post-run action did not happen, or why its countdown ended early.
enum QueuePostRunReason: Equatable, Codable {
    /// Items failed, were interrupted, or wait for a choice; a person should see them first.
    case videosNeedAttention(count: Int)
    case videosHeldForReview(count: Int)
    /// Items are still waiting, stopped, or not started, for example after a run time ended.
    case videosNotFinished(count: Int)
    /// The queue is running or paused again.
    case queueNotIdle
    case startLaterPending
    /// Someone used the keyboard, mouse, or trackpad recently.
    case macInUse
    case idleTimeUnavailable
    case otherWork(QueuePostRunOtherWork)
    /// macOS does not allow the app to send the request.
    case permissionDenied
    /// macOS refused the request.
    case requestFailed
    case cancelledByPerson
    /// The setting changed during the countdown.
    case settingChanged
    /// The Mac slept or woke during the countdown, so its timing can no longer be trusted.
    case countdownInterrupted
    case appStopped

    var phrase: String {
        switch self {
        case let .videosNeedAttention(count):
            count == 1 ? "1 video needs attention" : "\(count) videos need attention"
        case let .videosHeldForReview(count):
            count == 1 ? "1 video is held for review" : "\(count) videos are held for review"
        case let .videosNotFinished(count):
            count == 1 ? "1 video has not finished" : "\(count) videos have not finished"
        case .queueNotIdle:
            "the queue started again"
        case .startLaterPending:
            "a Start Later window is still scheduled"
        case .macInUse:
            "someone used the Mac in the last few minutes"
        case .idleTimeUnavailable:
            "macOS did not report when the Mac was last used"
        case let .otherWork(work):
            work.phrase
        case .permissionDenied:
            "macOS has not allowed this app to shut down the Mac"
        case .requestFailed:
            "macOS did not accept the request"
        case .cancelledByPerson:
            "it was cancelled"
        case .settingChanged:
            "the setting changed"
        case .countdownInterrupted:
            "the Mac slept or woke during the countdown"
        case .appStopped:
            "the app stopped watching the queue"
        }
    }
}

/// What to do when a run finishes, decided from the setting, the queue, and the Mac's state alone.
enum QueuePostRunDecision: Equatable {
    /// The setting is Do Nothing; nothing is recorded.
    case off
    case skip(QueuePostRunReason)
    case proceed(QueuePostRunAction)

    /// How long the Mac must have gone without input before it may sleep or shut down on its own.
    static let defaultIdleThreshold: TimeInterval = 5 * 60

    /// Decides deterministically. Every check that could mean a person needs the Mac comes before
    /// the action, so any doubt, including an unknown idle time, keeps the Mac on.
    static func decide(
        action: QueuePostRunAction,
        queue: QueuePostRunQueueSnapshot,
        conditions: QueuePostRunConditions,
        idleThreshold: TimeInterval = defaultIdleThreshold
    ) -> QueuePostRunDecision {
        guard action != .doNothing else {
            return .off
        }
        guard queue.runState == .idle else {
            return .skip(.queueNotIdle)
        }
        var attention = 0
        var held = 0
        var unfinished = 0
        for state in queue.itemStates {
            switch state {
            case .completed:
                break
            case .needsChoice, .attention, .failed, .interrupted:
                attention += 1
            case .heldForReview:
                held += 1
            case .waiting, .notStarted, .stopped, .inspecting, .processing, .stopping:
                unfinished += 1
            }
        }
        if attention > 0 {
            return .skip(.videosNeedAttention(count: attention))
        }
        if held > 0 {
            return .skip(.videosHeldForReview(count: held))
        }
        if unfinished > 0 {
            return .skip(.videosNotFinished(count: unfinished))
        }
        if queue.hasPendingStartLaterSchedule {
            return .skip(.startLaterPending)
        }
        if queue.hasActiveConversion {
            return .skip(.otherWork(.conversion))
        }
        if let work = conditions.otherWork.first {
            return .skip(.otherWork(work))
        }
        guard let idleTime = conditions.idleTime else {
            return .skip(.idleTimeUnavailable)
        }
        guard idleTime >= idleThreshold else {
            return .skip(.macInUse)
        }
        return .proceed(action)
    }
}

/// A post-run action waiting for its countdown to end.
struct QueuePostRunCountdown: Equatable {
    let action: QueuePostRunAction
    let startedAt: Date
    let deadline: Date

    func remainingSeconds(at date: Date) -> Int {
        max(0, Int(deadline.timeIntervalSince(date).rounded(.up)))
    }

    func title(at date: Date) -> String {
        let seconds = remainingSeconds(at: date)
        let unit = seconds == 1 ? "second" : "seconds"
        return "The Mac will \(action.verbPhrase) in \(seconds) \(unit)"
    }

    static let detail = "The queue finished. Cancel, or use the keyboard or mouse, to keep the Mac on."
}

/// One thing the post-run setting did, kept with the run-policy activity for later review.
struct QueuePostRunActivity: Codable, Equatable, Identifiable {
    enum Kind: String, Codable, Equatable {
        /// The queue finished but the action did not start.
        case skipped
        case countdownStarted
        case countdownCancelled
        /// The app asked macOS to sleep or shut down.
        case requested
        case failed
    }

    let id: UUID
    let occurredAt: Date
    let action: QueuePostRunAction
    let kind: Kind
    let reason: QueuePostRunReason?

    init(id: UUID = UUID(), occurredAt: Date, action: QueuePostRunAction, kind: Kind, reason: QueuePostRunReason? = nil) {
        self.id = id
        self.occurredAt = occurredAt
        self.action = action
        self.kind = kind
        self.reason = reason
    }

    var message: String {
        let verb = action.verbPhrase
        let because = reason.map { " because \($0.phrase)" } ?? ""
        switch kind {
        case .skipped:
            return "The queue finished; did not \(verb)\(because)."
        case .countdownStarted:
            return "The queue finished; started a countdown to \(verb)."
        case .countdownCancelled:
            return "Did not \(verb)\(because)."
        case .requested:
            return action == .shutDown
                ? "Asked macOS to shut down. Apps with unsaved changes can still stop it."
                : "Asked macOS to sleep."
        case .failed:
            return "Could not \(verb)\(because)."
        }
    }
}

import Combine
import Foundation

/// Applies the "When the queue finishes" setting after a run ends with nothing left to do.
///
/// It decides with `QueuePostRunDecision.decide`, records every outcome, and runs a cancellable
/// countdown before asking macOS to sleep or shut down. During the countdown it checks again every
/// second, so any input, new work, or change to the queue cancels it. It lives in the automation
/// core, outside any window, and asks for system power actions only through its performer.
@MainActor
final class QueuePostRunController: ObservableObject {
    static let countdownDuration: TimeInterval = 60
    static let tickInterval: TimeInterval = 1
    /// A tick this late means the Mac slept or the app stalled, so the countdown is no longer trustworthy.
    static let lateTickTolerance: TimeInterval = 5

    @Published private(set) var countdown: QueuePostRunCountdown?

    /// Called when a countdown starts, so a window, notification, or helper can show it.
    var countdownStartedHandler: (@MainActor (QueuePostRunCountdown) -> Void)?
    /// Work outside the queue that keeps the Mac on, such as a preview or Movie Sharing.
    var otherWork: @MainActor () -> [QueuePostRunOtherWork] = { [] }

    let idleThreshold: TimeInterval
    private let store: QueueAutomationStore
    private let queueSnapshot: @MainActor () -> QueuePostRunQueueSnapshot
    private let idleTime: () -> TimeInterval?
    private let performer: any QueuePostRunPerforming
    private let timerScheduler: any QueueAutomationTimerScheduling
    private let now: () -> Date
    private var tickTimer: (any QueueAutomationTimer)?
    private var isFinishing = false

    init(
        store: QueueAutomationStore,
        queueSnapshot: @escaping @MainActor () -> QueuePostRunQueueSnapshot,
        idleTime: @escaping () -> TimeInterval?,
        performer: any QueuePostRunPerforming,
        timerScheduler: any QueueAutomationTimerScheduling,
        now: @escaping () -> Date,
        idleThreshold: TimeInterval = QueuePostRunDecision.defaultIdleThreshold
    ) {
        self.store = store
        self.queueSnapshot = queueSnapshot
        self.idleTime = idleTime
        self.performer = performer
        self.timerScheduler = timerScheduler
        self.now = now
        self.idleThreshold = idleThreshold
    }

    var action: QueuePostRunAction { store.runPolicy.postRunAction }
    var activity: [QueuePostRunActivity] { store.postRunActivity }

    /// The decision the setting would make right now.
    func currentDecision(for action: QueuePostRunAction? = nil) -> QueuePostRunDecision {
        QueuePostRunDecision.decide(
            action: action ?? self.action,
            queue: queueSnapshot(),
            conditions: QueuePostRunConditions(idleTime: idleTime(), otherWork: otherWork()),
            idleThreshold: idleThreshold
        )
    }

    /// Lets macOS ask the person for permission while they are choosing the setting.
    func requestPermission(for action: QueuePostRunAction) async -> QueuePostRunPermission {
        await performer.requestPermission(for: action)
    }

    func permission(for action: QueuePostRunAction) async -> QueuePostRunPermission {
        await performer.permission(for: action)
    }

    /// Called when a run ends with every adopted video done. Starts a countdown, or records why not.
    func queueDidFinish() async {
        guard countdown == nil, !isFinishing else {
            return
        }
        let action = self.action
        switch currentDecision(for: action) {
        case .off:
            return
        case let .skip(reason):
            record(.skipped, action: action, reason: reason)
        case let .proceed(action):
            // Permission is checked without a prompt: nobody may be there to answer one.
            guard await performer.permission(for: action) == .granted else {
                record(.skipped, action: action, reason: .permissionDenied)
                return
            }
            guard countdown == nil, !isFinishing, self.action == action else {
                return
            }
            if case let .skip(reason) = currentDecision(for: action) {
                record(.skipped, action: action, reason: reason)
                return
            }
            let startedAt = now()
            let countdown = QueuePostRunCountdown(
                action: action,
                startedAt: startedAt,
                deadline: startedAt.addingTimeInterval(Self.countdownDuration)
            )
            self.countdown = countdown
            record(.countdownStarted, action: action)
            countdownStartedHandler?(countdown)
            scheduleTick(after: startedAt, deadline: countdown.deadline)
        }
    }

    /// The person pressed Cancel.
    func cancel() {
        endCountdown(.cancelledByPerson)
    }

    /// The setting changed; a countdown for another action, or for none, stops.
    func settingDidChange() {
        guard let countdown, countdown.action != action else {
            return
        }
        endCountdown(.settingChanged)
    }

    /// The Mac is about to sleep or just woke; a countdown cannot continue across that.
    func systemPowerStateDidChange() {
        endCountdown(.countdownInterrupted)
    }

    /// The app stopped watching the queue, for example because it is quitting.
    func stop() {
        endCountdown(.appStopped)
    }

    /// Checks the countdown now; normally called by its own timer every second.
    func tick() async {
        guard let countdown, !isFinishing else {
            return
        }
        let date = now()
        if date < countdown.startedAt || date > countdown.deadline.addingTimeInterval(Self.lateTickTolerance) {
            endCountdown(.countdownInterrupted)
            return
        }
        guard action == countdown.action else {
            endCountdown(.settingChanged)
            return
        }
        if case let .skip(reason) = currentDecision(for: countdown.action) {
            endCountdown(reason)
            return
        }
        guard date >= countdown.deadline else {
            scheduleTick(after: date, deadline: countdown.deadline)
            return
        }
        await finish(countdown)
    }

    private func finish(_ countdown: QueuePostRunCountdown) async {
        isFinishing = true
        defer { isFinishing = false }
        tickTimer?.cancel()
        tickTimer = nil
        let permission = await performer.permission(for: countdown.action)
        // Cancel may have been pressed while permission was being checked.
        guard self.countdown == countdown else {
            return
        }
        guard permission == .granted else {
            endCountdown(.permissionDenied)
            return
        }
        if case let .skip(reason) = currentDecision(for: countdown.action) {
            endCountdown(reason)
            return
        }
        self.countdown = nil
        // Recorded first: after a sleep or shutdown request there may be no later chance.
        record(.requested, action: countdown.action)
        do {
            try await performer.perform(countdown.action)
        } catch {
            record(.failed, action: countdown.action, reason: .requestFailed)
        }
    }

    private func endCountdown(_ reason: QueuePostRunReason) {
        tickTimer?.cancel()
        tickTimer = nil
        guard let countdown else {
            return
        }
        self.countdown = nil
        record(.countdownCancelled, action: countdown.action, reason: reason)
    }

    private func scheduleTick(after date: Date, deadline: Date) {
        tickTimer?.cancel()
        let next = min(date.addingTimeInterval(Self.tickInterval), deadline)
        tickTimer = timerScheduler.schedule(at: next) { [weak self] in
            guard let self else {
                return
            }
            tickTimer = nil
            Task { @MainActor in await self.tick() }
        }
    }

    private func record(_ kind: QueuePostRunActivity.Kind, action: QueuePostRunAction, reason: QueuePostRunReason? = nil) {
        try? store.recordPostRunActivity(QueuePostRunActivity(
            occurredAt: now(),
            action: action,
            kind: kind,
            reason: reason
        ))
    }
}

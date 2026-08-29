import Combine
import Foundation

@MainActor
final class PersistentQueueNotificationCoordinator: ObservableObject {
    private enum ThreadIdentifier {
        static let attention = "queue.attention"
        static let completion = "queue.completion"
    }

    private struct Snapshot: Equatable {
        let items: [PersistentQueueItem]
        let runState: PersistentQueueRunState
        let completionRevision: UInt

        var itemsByID: [UUID: PersistentQueueItem] {
            Dictionary(uniqueKeysWithValues: items.map { ($0.id, $0) })
        }
    }

    private struct Session {
        let id = UUID()
        var participatingItemIDs: Set<UUID>
        var baselineTerminalItemIDs: Set<UUID>
        var sentAttention = false
        var sentCompletion = false
    }

    private let settings: AppSettings
    private let delivery: QueueNotificationDelivering
    private var cancellables = Set<AnyCancellable>()
    private var lastSnapshot: Snapshot
    private var requestedAuthorization = false
    private var session: Session?

    init(
        viewModel: ConversionViewModel,
        settings: AppSettings,
        delivery: QueueNotificationDelivering
    ) {
        self.settings = settings
        self.delivery = delivery
        lastSnapshot = Snapshot(
            items: viewModel.persistentQueueItems,
            runState: viewModel.persistentQueueRunState,
            completionRevision: viewModel.persistentQueueCompletionRevision
        )
        bind(viewModel: viewModel)
        bind(settings: settings)
    }

    init(
        settings: AppSettings,
        delivery: QueueNotificationDelivering,
        initialItems: [PersistentQueueItem] = [],
        initialRunState: PersistentQueueRunState = .idle,
        initialCompletionRevision: UInt = 0
    ) {
        self.settings = settings
        self.delivery = delivery
        lastSnapshot = Snapshot(
            items: initialItems,
            runState: initialRunState,
            completionRevision: initialCompletionRevision
        )
        bind(settings: settings)
    }

    func observe(
        items: [PersistentQueueItem],
        runState: PersistentQueueRunState,
        completionRevision: UInt = 0
    ) {
        let snapshot = Snapshot(
            items: items,
            runState: runState,
            completionRevision: completionRevision
        )
        defer { lastSnapshot = snapshot }

        if session == nil,
           lastSnapshot.runState != .running,
           runState == .running
        {
            session = Session(
                participatingItemIDs: Set(items.filter(\.isQueueNotificationParticipant).map(\.id)),
                baselineTerminalItemIDs: Set(items.filter(\.isQueueNotificationBaselineTerminal).map(\.id))
            )
        }

        guard session != nil else {
            return
        }

        refreshParticipation(with: snapshot, previous: lastSnapshot)
        deliverAttentionIfNeeded(items: snapshot.items)

        if completionRevision != lastSnapshot.completionRevision {
            deliverCompletionIfNeeded(items: snapshot.items)
            session = nil
        } else if runState == .idle, lastSnapshot.runState != .idle {
            session = nil
        } else if runState != .running,
                  let session,
                  sessionHasNoPendingItems(session, in: snapshot.items)
        {
            self.session = nil
        }
    }

    private func bind(viewModel: ConversionViewModel) {
        Publishers.CombineLatest3(
            viewModel.$persistentQueueItems,
            viewModel.$persistentQueueRunState,
            viewModel.$persistentQueueCompletionRevision
        )
            .sink { [weak self] items, runState, completionRevision in
                self?.observe(
                    items: items,
                    runState: runState,
                    completionRevision: completionRevision
                )
            }
            .store(in: &cancellables)
    }

    private func bind(settings: AppSettings) {
        settings.$notifyWhenQueueFinishes
            .combineLatest(settings.$notifyWhenQueueNeedsAttention)
            .map { $0 || $1 }
            .removeDuplicates()
            .sink { [weak self] enabled in
                self?.requestAuthorizationIfNeeded(enabled: enabled)
            }
            .store(in: &cancellables)
    }

    private func requestAuthorizationIfNeeded(enabled: Bool) {
        guard enabled, !requestedAuthorization else {
            return
        }
        requestedAuthorization = true
        Task { @MainActor [delivery] in
            _ = await delivery.requestAuthorization()
        }
    }

    private func refreshParticipation(with snapshot: Snapshot, previous: Snapshot) {
        guard var session else {
            return
        }
        let previousItems = previous.itemsByID
        for item in snapshot.items {
            let wasBaselineTerminal = session.baselineTerminalItemIDs.contains(item.id)
            let previousStatus = previousItems[item.id]?.status
            let changedFromBaselineTerminal = wasBaselineTerminal && previousStatus != item.status
            if item.isQueueNotificationParticipant || changedFromBaselineTerminal {
                session.participatingItemIDs.insert(item.id)
                session.baselineTerminalItemIDs.remove(item.id)
            }
        }
        self.session = session
    }

    private func deliverAttentionIfNeeded(items: [PersistentQueueItem]) {
        guard var session, !session.sentAttention else {
            return
        }
        let attentionCount = participatingItems(from: items, session: session).filter(\.isQueueNotificationAttentionTrigger).count
        guard attentionCount > 0 else {
            return
        }

        session.sentAttention = true
        self.session = session
        guard settings.notifyWhenQueueNeedsAttention else {
            return
        }

        let body = attentionCount == 1
            ? "1 queued item needs attention."
            : "\(attentionCount) queued items need attention."
        deliver(
            identifier: "queue-attention-\(session.id.uuidString)",
            threadIdentifier: ThreadIdentifier.attention,
            title: "Queue Needs Attention",
            body: body
        )
    }

    private func deliverCompletionIfNeeded(items: [PersistentQueueItem]) {
        guard var session, !session.sentCompletion else {
            return
        }
        let summary = PersistentQueueOutcomeSummary(items: participatingItems(from: items, session: session))
        guard summary.hasAnyResults else {
            return
        }

        session.sentCompletion = true
        self.session = session
        guard settings.notifyWhenQueueFinishes else {
            return
        }

        deliver(
            identifier: "queue-completion-\(session.id.uuidString)",
            threadIdentifier: ThreadIdentifier.completion,
            title: "Queue Finished",
            body: summary.notificationDescription
        )
    }

    private func participatingItems(from items: [PersistentQueueItem], session: Session) -> [PersistentQueueItem] {
        items.filter { session.participatingItemIDs.contains($0.id) }
    }

    private func sessionHasNoPendingItems(_ session: Session, in items: [PersistentQueueItem]) -> Bool {
        let itemsByID = Dictionary(uniqueKeysWithValues: items.map { ($0.id, $0) })
        return session.participatingItemIDs.allSatisfy { itemID in
            itemsByID[itemID]?.isQueueNotificationTerminalForSession ?? true
        }
    }

    private func deliver(
        identifier: String,
        threadIdentifier: String,
        title: String,
        body: String
    ) {
        let request = QueueNotificationRequest(
            identifier: identifier,
            threadIdentifier: threadIdentifier,
            title: title,
            body: body
        )
        Task { @MainActor [delivery] in
            await delivery.deliver(request)
        }
    }
}

private extension PersistentQueueItem {
    var isQueueNotificationParticipant: Bool {
        switch status {
        case .waiting, .inspecting, .processing, .stopping, .stopped, .notStarted:
            true
        case .needsChoice, .interrupted, .attention, .failed, .completed:
            false
        }
    }

    var isQueueNotificationBaselineTerminal: Bool {
        switch status {
        case .needsChoice, .interrupted, .attention, .failed, .completed:
            true
        case .waiting, .inspecting, .processing, .stopping, .stopped, .notStarted:
            false
        }
    }

    var isQueueNotificationAttentionTrigger: Bool {
        switch status {
        case .needsChoice, .interrupted, .attention, .failed:
            true
        case .waiting, .inspecting, .processing, .stopping, .completed, .stopped, .notStarted:
            false
        }
    }

    var isQueueNotificationTerminalForSession: Bool {
        switch status {
        case .needsChoice, .interrupted, .attention, .failed, .completed, .stopped, .notStarted:
            true
        case .waiting, .inspecting, .processing, .stopping:
            false
        }
    }
}

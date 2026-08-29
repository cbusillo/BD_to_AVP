import Combine
import Foundation
import SwiftUI

private enum ActiveRunMode: Equatable {
    case singleInspection
    case singleConversion
    case titleQueueInspection(itemID: UUID)
    case titleQueueConversion(itemID: UUID)
    case batchInspection(itemID: UUID)
    case batchConversion(itemID: UUID)
    case durableSingleInspection(itemID: UUID)
    case durableSingleConversion(itemID: UUID)

    var diagnosticName: String {
        switch self {
        case .singleInspection:
            "single_inspection"
        case .singleConversion:
            "single_conversion"
        case .titleQueueInspection:
            "title_queue_inspection"
        case .titleQueueConversion:
            "title_queue_conversion"
        case .batchInspection:
            "batch_inspection"
        case .batchConversion:
            "batch_conversion"
        case .durableSingleInspection:
            "durable_single_inspection"
        case .durableSingleConversion:
            "durable_single_conversion"
        }
    }
}

private struct TitleQueueTerminalSnapshot {
    let phase: WorkerPhase
    let decision: WorkerDecision?
    let failure: DurableQueueFailure
    let result: DurableQueueResult?
}

private struct SourceFolderTerminalSnapshot {
    let phase: WorkerPhase
    let inspection: SourceInspection?
    let decision: WorkerDecision?
    let failure: DurableQueueFailure
    let result: DurableQueueResult?
}

@MainActor
final class ConversionViewModel: ObservableObject, UpdateInstallPostponing {
    typealias ClientFactory = () throws -> any WorkerProcessRunning
    typealias SourceAvailabilityResolver = (ConversionSource) -> Bool

    @Published private(set) var source: ConversionSource?
    @Published private(set) var state = WorkerLifecycleState()
    @Published private(set) var liveObservabilityStatus = LiveObservabilityStatus.empty
    @Published private(set) var batchQueue: SourceFolderQueueState?
    @Published private(set) var queueItems: [ConversionQueueItem] = []
    @Published private(set) var persistentQueueItems: [PersistentQueueItem] = []
    @Published private(set) var persistentQueueProjectionError: PersistentQueueProjectionError?
    @Published private(set) var selectedPersistentQueueItemID: UUID?
    @Published private(set) var completedBatchResults: [ConversionResult]?
    @Published private(set) var durableQueueRuntimeDiagnostic: String?
    @Published private(set) var persistentQueueRunState: PersistentQueueRunState = .idle
    @Published private(set) var offPeakSchedule: OffPeakQueueSchedule?
    @Published private(set) var offPeakScheduleOutcome: OffPeakScheduleOutcome?
    @Published private(set) var offPeakScheduleErrorMessage: String?

    private let clientFactory: ClientFactory
    private let diagnosticClock: () -> Date
    private let diagnosticStorageProbe: any DiagnosticStorageProbing
    private let diagnosticBundleBuilder: DiagnosticBundleBuilder
    private let observabilityEventStore: any ObservabilityEventPersisting
    private let durableQueueStore: ConversionQueueStore
    private let offPeakScheduleStore: OffPeakScheduleStore
    private let sourceAvailabilityResolver: SourceAvailabilityResolver
    private let diagnosticRecorder = DiagnosticSessionRecorder()
    private var client: (any WorkerProcessRunning)?
    private var runTask: Task<Void, Never>?
    private var pendingTerminalEvent: WorkerEvent?
    private var lastConversionDraft: ConversionDraft?
    private var activeRunMode: ActiveRunMode?
    private var pendingBatchContinuation: Task<Void, Never>?
    private var actionsWaitingForIdle: [() -> Void] = []
    private var activeQueueItemID: UUID?
    private var titleQueueGroupID: UUID?
    private var titleQueueStopRequested = false
    private var pendingQueueTransition: Task<Void, Never>?
    private var pendingQueueTransitionID: UUID?
    private var sourceFolderQueueGroupID: UUID?
    private var sourceFolderStopRequested = false
    private var durableQueueStopRequested = false
    private var persistentQueueControlsActive = false
    private var adoptedItemIDs: Set<UUID> = []
    private var sourceFolderQueueCompletionPending = false
    private var sourceFolderRecoveryChoices: [UUID: String] = [:]
    private var durableSingleJobIDs: [UUID: UUID] = [:]
    private var batchItemDiagnosticJobIDs: [UUID: UUID] = [:]
    private var durableQueueSubscription: AnyCancellable?
    private var offPeakScheduleSubscription: AnyCancellable?
    private var offPeakRunWindowEnd: Date?

    init(
        clientFactory: @escaping ClientFactory = {
            WorkerProcessClient(configuration: try WorkerLaunchConfiguration.automatic())
        },
        diagnosticClock: @escaping () -> Date = Date.init,
        diagnosticStorageProbe: any DiagnosticStorageProbing = FileSystemDiagnosticStorageProbe(),
        diagnosticBundleBuilder: DiagnosticBundleBuilder? = nil,
        observabilityEventStore: any ObservabilityEventPersisting = NullObservabilityEventStore.shared,
        durableQueueStore: ConversionQueueStore? = nil,
        offPeakScheduleStore: OffPeakScheduleStore? = nil,
        sourceAvailabilityResolver: @escaping SourceAvailabilityResolver = ConversionViewModel.defaultSourceAvailability
    ) {
        self.clientFactory = clientFactory
        self.diagnosticClock = diagnosticClock
        self.diagnosticStorageProbe = diagnosticStorageProbe
        self.diagnosticBundleBuilder = diagnosticBundleBuilder
            ?? DiagnosticBundleBuilder(storageProbe: diagnosticStorageProbe)
        self.observabilityEventStore = observabilityEventStore
        self.durableQueueStore = durableQueueStore ?? ConversionQueueStore.inMemory()
        self.offPeakScheduleStore = offPeakScheduleStore ?? OffPeakScheduleStore.inMemory()
        self.sourceAvailabilityResolver = sourceAvailabilityResolver
        durableQueueSubscription = self.durableQueueStore.$document.sink { [weak self] document in
            self?.publishPersistentQueueProjection(items: document.items)
        }
        offPeakScheduleSubscription = self.offPeakScheduleStore.$document.sink { [weak self] document in
            self?.offPeakSchedule = document.schedule
            self?.offPeakScheduleOutcome = document.lastOutcome
        }
        offPeakSchedule = self.offPeakScheduleStore.schedule
        offPeakScheduleOutcome = self.offPeakScheduleStore.lastOutcome
        offPeakScheduleErrorMessage = self.offPeakScheduleStore.loadErrorMessage
    }

    var isRunning: Bool {
        state.phase.isRunning
    }

    var hasActiveWorker: Bool {
        runTask != nil
    }

    var hasActiveWork: Bool {
        hasActiveWorker
            || pendingQueueTransition != nil
            || pendingBatchContinuation != nil
            || isBatchRunning
            || hasQueuedWork
            || state.phase == .decisionRequired
    }

    var hasStoppableWork: Bool {
        hasActiveWorker
            || pendingQueueTransition != nil
            || pendingBatchContinuation != nil
            || isBatchRunning
            || (hasQueuedWork && state.phase != .decisionRequired)
    }

    var isBatchRunning: Bool {
        batchQueue?.isRunning == true
    }

    var activeBatchItem: SourceFolderQueueItem? {
        batchQueue?.activeItem
    }

    var canSelectSource: Bool {
        !hasActiveWork && state.phase != .decisionRequired
    }

    var hasQueuedWork: Bool {
        pendingQueueTransition != nil || activeQueueItemID != nil || durableQueueStore.items.contains { item in
            adoptedItemIDs.contains(item.id)
                && (item.state == .waiting || item.state == .inspecting || item.state == .processing || item.state == .stopping)
        }
    }

    var hasPendingWork: Bool {
        hasActiveWork
    }

    var canRetry: Bool {
        !hasActiveWork
            && state.recoveryDecision == nil
            && (state.phase == .cancelled || state.failureRetryable)
    }

    var hasDiagnosticEvidence: Bool {
        diagnosticRecorder.currentJobContext != nil
    }

    var restoredDurableQueueItems: [DurableConversionQueueItem] {
        durableQueueStore.items
    }

    var selectedPersistentQueueItem: PersistentQueueItem? {
        guard let selectedPersistentQueueItemID else {
            return nil
        }
        return persistentQueueItems.first(where: { $0.id == selectedPersistentQueueItemID })
    }

    var durableQueueLoadErrorMessage: String? {
        durableQueueStore.loadErrorMessage
    }

    var durableQueueWritesBlocked: Bool {
        durableQueueStore.writesBlocked
    }

    func selectPersistentQueueItem(_ itemID: UUID?) {
        selectedPersistentQueueItemID = itemID.flatMap { selectedID in
            persistentQueueItems.contains(where: { $0.id == selectedID }) ? selectedID : nil
        }
    }

    func movePersistentQueueItem(_ itemID: UUID, before targetID: UUID) async throws {
        try await durableQueueStore.moveWaitingItem(itemID, before: targetID)
    }

    func movePersistentQueueItem(_ itemID: UUID, after targetID: UUID) async throws {
        try await durableQueueStore.moveWaitingItem(itemID, after: targetID)
    }

    func movePersistentQueueItemNext(_ itemID: UUID) async throws {
        try await durableQueueStore.moveWaitingItemNext(itemID)
    }

    func removePersistentQueueItems(_ itemIDs: Set<UUID>) async throws -> PersistentQueueRemovalToken {
        let token = try await durableQueueStore.removeRemovableItems(itemIDs)
        for itemID in itemIDs {
            releaseAdoption(itemID)
        }
        return token
    }

    func restorePersistentQueueItems(_ token: PersistentQueueRemovalToken) async throws {
        try await durableQueueStore.restoreRemovedItems(token)
    }

    func updatePersistentQueueItem(
        _ itemID: UUID,
        draft: ConversionDraft,
        routeQualityConflict: RouteQualityConflict? = nil
    ) async throws {
        try await durableQueueStore.updateWaitingItemIntent(
            itemID,
            intent: DurableQueueItemIntent(draft: draft),
            routeQualityConflict: routeQualityConflict.map(DurableRouteQualityConflict.init)
        )
    }

    func clearCompletedPersistentQueueItems() async throws -> PersistentQueueRemovalToken {
        try await durableQueueStore.clearCompletedItems()
    }

    func appendPersistentQueueDrafts(
        _ drafts: [ConversionDraft],
        conflicts: [RouteQualityConflict?] = []
    ) async throws -> PersistentQueueAppendResult {
        let existingIdentities = Set(persistentQueueItems.map {
            ConversionQueueItem.stablePreviewID(for: $0.draft)
        })
        var admittedIdentities = existingIdentities
        let suppliedConflicts = conflicts + Array(repeating: nil, count: max(0, drafts.count - conflicts.count))
        var admittedEntries: [(draft: ConversionDraft, conflict: RouteQualityConflict?)] = []
        var duplicateDisplayNames: [String] = []
        for (index, draft) in drafts.enumerated() {
            let identity = ConversionQueueItem.stablePreviewID(for: draft)
            guard admittedIdentities.insert(identity).inserted else {
                duplicateDisplayNames.append(draft.selectedTitle?.name ?? draft.source.displayName)
                continue
            }
            admittedEntries.append((draft, suppliedConflicts[index]))
        }
        guard !admittedEntries.isEmpty else {
            return PersistentQueueAppendResult(addedCount: 0, duplicateDisplayNames: duplicateDisplayNames)
        }

        var groupIDsBySource: [QueueSourceIdentity: UUID] = [:]
        let sourcesRequestingRemoval: Set<QueueSourceIdentity> = Set(admittedEntries.compactMap { entry in
            let draft = entry.draft
            guard draft.options.job.removeOriginalAfterSuccess else {
                return nil
            }
            return QueueSourceIdentity(source: draft.source)
        })
        var finalRemovalIndexBySource: [QueueSourceIdentity: Int] = [:]
        for (offset, entry) in admittedEntries.enumerated() {
            let draft = entry.draft
            let sourceIdentity = QueueSourceIdentity(source: draft.source)
            if sourcesRequestingRemoval.contains(sourceIdentity) {
                finalRemovalIndexBySource[sourceIdentity] = offset
            }
        }
        let newItems = admittedEntries.enumerated().map { offset, entry in
            let originalDraft = entry.draft
            var options = originalDraft.options
            let sourceIdentity = QueueSourceIdentity(source: originalDraft.source)
            options.job.removeOriginalAfterSuccess = finalRemovalIndexBySource[sourceIdentity] == offset
            let draft = ConversionDraft(
                source: originalDraft.source,
                sourceDetails: originalDraft.sourceDetails,
                profile: originalDraft.profile,
                destinationURL: originalDraft.destinationURL,
                options: options,
                selectedTitle: originalDraft.selectedTitle
            )
            let origin: DurableQueueItemOrigin = draft.source.kind == .sourceFolder
                ? .sourceFolder
                : (draft.selectedTitle == nil ? .singleSource : .multiTitle)
            let itemGroupID: UUID?
            if origin == .singleSource {
                itemGroupID = nil
            } else {
                itemGroupID = groupIDsBySource[sourceIdentity] ?? {
                    let groupID = UUID()
                    groupIDsBySource[sourceIdentity] = groupID
                    return groupID
                }()
            }
            return DurableConversionQueueItem(
                ordinal: offset,
                groupID: itemGroupID,
                origin: origin,
                intent: DurableQueueItemIntent(draft: draft),
                inspection: draft.sourceDetails,
                state: entry.conflict == nil ? .waiting : .needsChoice,
                routeQualityConflict: entry.conflict.map(DurableRouteQualityConflict.init)
            )
        }
        try await durableQueueStore.appendAdmittedItems(newItems)
        selectedPersistentQueueItemID = newItems.first?.id
        return PersistentQueueAppendResult(
            addedCount: newItems.count,
            duplicateDisplayNames: duplicateDisplayNames
        )
    }

    var persistentQueueResolutionGroups: [QueueResolutionGroup] {
        QueueResolutionGroup.group(
            persistentQueueItems.compactMap { item in
                guard case let .needsChoice(conflict) = item.status else {
                    return nil
                }
                return QueueResolutionCandidate(id: item.id, draft: item.draft, conflict: conflict)
            }
        )
    }

    func resolvePersistentQueueItems(
        group: QueueResolutionGroup,
        selection: QueueResolutionSelection
    ) async throws {
        let application = try QueueResolutionApplication.apply(group: group, selection: selection).get()
        let intents = application.resolvedDrafts.mapValues { draft in
            DurableQueueItemIntent(draft: draft)
        }
        let traces = application.resolvedDrafts.mapValues { draft in
            DurableQueueResolutionTrace(
                conflictID: group.conflict.stableID,
                resolutionID: application.resolution.id,
                qualityOutcome: "\(draft.options.videoRoutePlan.qualityTitle) quality",
                fileOutcome: draft.options.job.intermediatePolicy.createsReusableArtifacts
                    ? "Reusable files kept"
                    : "Reusable files removed"
            )
        }
        try await durableQueueStore.resolveHeldItems(
            Set(application.resolvedDrafts.keys),
            intents: intents,
            traces: traces
        )
    }

    func saveOffPeakSchedule(startAt: Date, endAt: Date) async throws {
        guard !hasActiveWorker,
              pendingQueueTransition == nil,
              pendingBatchContinuation == nil,
              !isBatchRunning,
              state.phase != .decisionRequired,
              persistentQueueRunState != .running,
              persistentQueueRunState != .pauseAfterCurrent
        else {
            throw OffPeakScheduleStoreError.queueIsActive
        }
        guard durableQueueStore.items.contains(where: Self.isScheduleEligible) else {
            throw OffPeakScheduleStoreError.noEligibleItems
        }
        guard startAt > diagnosticClock(), endAt > startAt else {
            throw OffPeakScheduleStoreError.invalidWindow
        }
        try await offPeakScheduleStore.save(OffPeakQueueSchedule(
            startAt: startAt,
            endAt: endAt,
            createdAt: diagnosticClock()
        ))
        offPeakScheduleErrorMessage = nil
    }

    func cancelOffPeakSchedule() async throws {
        try await offPeakScheduleStore.cancel()
        offPeakScheduleErrorMessage = nil
    }

    func clearOffPeakScheduleOutcome() async throws {
        try await offPeakScheduleStore.clearOutcome()
        offPeakScheduleErrorMessage = nil
    }

    @discardableResult
    func evaluateOffPeakSchedule(appLaunched: Bool = false) async -> OffPeakScheduleEvaluation {
        guard let schedule = offPeakScheduleStore.schedule else {
            return .none
        }
        if hasActiveWorker
            || pendingQueueTransition != nil
            || state.phase == .decisionRequired
            || persistentQueueRunState == .running
            || persistentQueueRunState == .pauseAfterCurrent
        {
            return .waiting(schedule)
        }
        var consumedSchedule: OffPeakQueueSchedule?
        do {
            let evaluation = try await offPeakScheduleStore.evaluate(
                at: diagnosticClock(),
                appLaunched: appLaunched
            )
            offPeakScheduleErrorMessage = nil
            guard case let .start(startedSchedule) = evaluation else {
                return evaluation
            }
            consumedSchedule = startedSchedule
            try await parkUnavailableScheduledQueueItems()
            let outcome = await startPersistentQueue(windowEnd: startedSchedule.endAt)
            if case let .rejected(rejection) = outcome {
                offPeakRunWindowEnd = nil
                let reason: OffPeakScheduleMissReason
                switch rejection {
                case .noEligibleItems:
                    reason = .noRunnableItems
                case .unresolvedChoices:
                    reason = .unresolvedChoices
                case .noActiveItem, .queueIsNotRunning, .otherWorkIsActive:
                    reason = .queueBecameActive
                }
                try await offPeakScheduleStore.markStartedScheduleMissed(
                    scheduleID: startedSchedule.id,
                    reason: reason,
                    at: diagnosticClock()
                )
            }
            consumedSchedule = nil
            return evaluation
        } catch {
            if let consumedSchedule {
                try? await offPeakScheduleStore.markStartedScheduleMissed(
                    scheduleID: consumedSchedule.id,
                    reason: .queuePersistenceFailed,
                    at: diagnosticClock()
                )
            }
            offPeakScheduleErrorMessage = error.localizedDescription
            return .none
        }
    }

    private static func isScheduleEligible(_ item: DurableConversionQueueItem) -> Bool {
        switch item.state {
        case .waiting, .interrupted, .stopped, .notStarted:
            true
        case .needsChoice, .inspecting, .processing, .stopping, .attention, .failed, .completed:
            false
        }
    }

    private func parkUnavailableScheduledQueueItems() async throws {
        let eligibleItemIDs = Set(durableQueueStore.items
            .filter(Self.isScheduleEligible)
            .map(\.id))
        try await parkUnavailableQueueItems(
            eligibleItemIDs: eligibleItemIDs,
            failure: { item in
                let isPhysicalDisc = item.intent.source.kind == ConversionSourceKind.physicalDisc.rawValue
                return DurableQueueFailure(
                    code: isPhysicalDisc ? "scheduled_disc_unavailable" : "scheduled_source_unavailable",
                    message: isPhysicalDisc
                        ? "The required Blu-ray disc is not inserted."
                        : "The scheduled source is no longer available.",
                    details: item.intent.source.path,
                    retryable: true
                )
            }
        )
    }

    private func parkUnavailableManualQueueItems() async throws {
        let eligibleItemIDs = Set(durableQueueStore.items.compactMap { item -> UUID? in
            switch item.state {
            case .waiting, .interrupted, .stopped, .notStarted:
                item.id
            case .needsChoice, .inspecting, .processing, .stopping, .attention, .failed, .completed:
                nil
            }
        })
        try await parkUnavailableQueueItems(
            eligibleItemIDs: eligibleItemIDs,
            failure: { item in
                let isPhysicalDisc = item.intent.source.kind == ConversionSourceKind.physicalDisc.rawValue
                return DurableQueueFailure(
                    code: "source_unavailable",
                    message: isPhysicalDisc
                        ? "The required Blu-ray disc is not inserted."
                        : "The queued source is no longer available.",
                    details: item.intent.source.path,
                    retryable: true
                )
            }
        )
    }

    private func parkUnavailableQueueItems(
        eligibleItemIDs: Set<UUID>,
        failure: (DurableConversionQueueItem) -> DurableQueueFailure
    ) async throws {
        struct UnavailableSource: Hashable {
            let kind: String
            let path: String
            let workerSourcePath: String?
            let mediaIdentifier: String?
        }

        var unavailableSources: [UnavailableSource: DurableQueueFailure] = [:]
        for item in durableQueueStore.items where eligibleItemIDs.contains(item.id) {
            let source = try? durableSource(for: item)
            guard let source, sourceAvailabilityResolver(source) else {
                let sourceIdentity = UnavailableSource(
                    kind: item.intent.source.kind,
                    path: item.intent.source.path,
                    workerSourcePath: item.intent.source.workerSourcePath,
                    mediaIdentifier: item.intent.source.mediaIdentifier
                )
                unavailableSources[sourceIdentity] = failure(item)
                continue
            }
        }
        guard !unavailableSources.isEmpty else {
            return
        }
        var parkedItemIDs: Set<UUID> = []
        try await durableQueueStore.mutateItems { items in
            for (sourceIdentity, failure) in unavailableSources {
                let matchingIndices = items.indices.filter { index in
                    let source = items[index].intent.source
                    return eligibleItemIDs.contains(items[index].id)
                        && source.kind == sourceIdentity.kind
                        && source.path == sourceIdentity.path
                        && source.workerSourcePath == sourceIdentity.workerSourcePath
                        && source.mediaIdentifier == sourceIdentity.mediaIdentifier
                }
                for (offset, index) in matchingIndices.enumerated() {
                    items[index].state = offset == 0 ? .failed : .stopped
                    items[index].decision = nil
                    items[index].failure = offset == 0 ? failure : nil
                    if offset == 0,
                       let attemptIndex = items[index].attempts.lastIndex(where: { $0.endedAt == nil })
                    {
                        items[index].attempts[attemptIndex].endedAt = diagnosticClock()
                    }
                    parkedItemIDs.insert(items[index].id)
                }
            }
        }
        for itemID in parkedItemIDs {
            releaseAdoption(itemID)
        }
    }

    @discardableResult
    func startPersistentQueue(windowEnd: Date? = nil) async -> PersistentQueueCommandOutcome {
        if persistentQueueRunState == .running {
            return .noChange(.running)
        }
        if persistentQueueRunState == .pauseAfterCurrent {
            return .noChange(.pauseAfterCurrent)
        }
        guard !hasActiveWorker || activeDurableQueueItem != nil else {
            return .rejected(.otherWorkIsActive)
        }
        guard !persistentQueueItems.contains(where: { item in
            if case .needsChoice = item.status {
                return true
            }
            return false
        }) else {
            return .rejected(.unresolvedChoices)
        }
        if windowEnd == nil {
            do {
                try await parkUnavailableManualQueueItems()
            } catch {
                durableQueueRuntimeDiagnostic = "Queue could not park unavailable sources safely: \(error.localizedDescription)"
                return .rejected(.noEligibleItems)
            }
        }
        let candidates = persistentQueueItems.filter { item in
            switch item.status {
            case .waiting, .interrupted, .stopped, .notStarted:
                true
            case .needsChoice, .inspecting, .processing, .stopping, .attention, .failed, .completed:
                false
            }
        }
        guard !candidates.isEmpty else {
            return .rejected(.noEligibleItems)
        }
        let previousRunState = persistentQueueRunState
        let previousControlsActive = persistentQueueControlsActive
        let previousWindowEnd = offPeakRunWindowEnd
        parkActiveDurableQueueItemForResume()
        offPeakRunWindowEnd = windowEnd
        persistentQueueControlsActive = true
        persistentQueueRunState = .running
        durableQueueStopRequested = false
        var adoptedCount = 0
        for item in candidates {
            if await adoptPersistentQueueItem(item.id) {
                adoptedCount += 1
            }
        }
        guard adoptedCount > 0 else {
            persistentQueueRunState = previousRunState
            persistentQueueControlsActive = previousControlsActive
            offPeakRunWindowEnd = previousWindowEnd
            return .rejected(.noEligibleItems)
        }
        return .accepted(.running)
    }

    @discardableResult
    func pausePersistentQueueAfterCurrent() -> PersistentQueueCommandOutcome {
        switch persistentQueueRunState {
        case .pauseAfterCurrent:
            return .noChange(.pauseAfterCurrent)
        case .paused:
            return .noChange(.paused)
        case .idle:
            return .rejected(.queueIsNotRunning)
        case .running:
            persistentQueueRunState = .pauseAfterCurrent
            if !hasActiveWorker, activeQueueItemID == nil {
                enqueueQueueTransition { [weak self] in
                    await self?.pauseDurableQueueNow()
                }
            }
            return .accepted(.pauseAfterCurrent)
        }
    }

    @discardableResult
    func stopCurrentPersistentQueueItem() -> PersistentQueueCommandOutcome {
        if persistentQueueRunState == .paused {
            return .noChange(.paused)
        }
        guard persistentQueueRunState == .running || persistentQueueRunState == .pauseAfterCurrent else {
            return .rejected(.queueIsNotRunning)
        }
        guard hasActiveWorker, let item = activeDurableQueueItem else {
            return .rejected(.noActiveItem)
        }
        persistentQueueRunState = .paused
        state.requestStop()
        recordDiagnosticWorkflow(name: "cancel.requested", mode: activeRunMode, jobID: state.jobID)
        client?.cancel()
        enqueueQueueTransition { [weak self] in
            await self?.persistCurrentDurableQueueStop(itemID: item.id)
        }
        return .accepted(.paused)
    }

    @discardableResult
    func adoptPersistentQueueItem(
        _ itemID: UUID,
        recoveryChoice: WorkerRecoveryChoice? = nil
    ) async -> Bool {
        if persistentQueueRunState == .pauseAfterCurrent, hasActiveWorker {
            return false
        }
        if hasActiveWorker {
            switch activeRunMode {
            case .singleInspection, .singleConversion, nil:
                return false
            case .titleQueueInspection, .titleQueueConversion, .batchInspection, .batchConversion,
                 .durableSingleInspection, .durableSingleConversion:
                break
            }
        }
        guard !durableQueueStopRequested,
              activeQueueItemID != itemID,
              let existing = durableQueueStore.items.first(where: { $0.id == itemID }),
              let source = try? durableSource(for: existing),
              sourceAvailabilityResolver(source)
        else {
            return false
        }

        do {
            var recoveryChoiceValue: String?
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }) else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                switch items[index].state {
                case .waiting:
                    break
                case .interrupted, .stopped, .notStarted:
                    items[index].state = .waiting
                    items[index].decision = nil
                    items[index].failure = nil
                case .failed:
                    guard items[index].failure?.retryable == true else {
                        throw ConversionQueueStoreError.invalidDocument
                    }
                    items[index].state = .waiting
                    items[index].failure = nil
                    items[index].decision = nil
                    items[index].result = nil
                case .attention:
                    guard let recoveryChoice,
                          recoveryChoice != .cancel,
                          let decision = items[index].decision,
                          decision.choices.contains(recoveryChoice.rawValue)
                    else {
                        throw ConversionQueueStoreError.invalidDocument
                    }
                    var draft = try conversionDraft(for: items[index], preserveStoredSourceRemoval: true)
                    guard let recoveredDraft = draft.retrying(
                        decision: WorkerDecision(
                            identifier: decision.identifier,
                            prompt: decision.prompt,
                            choices: decision.choices,
                            details: decision.details
                        ),
                        choice: recoveryChoice
                    ) else {
                        throw ConversionQueueStoreError.invalidDocument
                    }
                    draft = recoveredDraft
                    items[index].intent = DurableQueueItemIntent(draft: draft)
                    items[index].state = .waiting
                    items[index].failure = nil
                    items[index].decision = nil
                    items[index].result = nil
                    recoveryChoiceValue = recoveryChoice.rawValue
                default:
                    throw ConversionQueueStoreError.invalidDocument
                }
            }
            adoptedItemIDs.insert(itemID)
            persistentQueueControlsActive = true
            persistentQueueRunState = .running
            if let recoveryChoiceValue {
                sourceFolderRecoveryChoices[itemID] = recoveryChoiceValue
            }
            if existing.origin == .multiTitle {
                titleQueueStopRequested = false
                if titleQueueGroupID == existing.groupID {
                    publishTitleQueueProjection()
                }
            } else if existing.origin == .sourceFolder {
                sourceFolderStopRequested = false
                if sourceFolderQueueGroupID == existing.groupID {
                    publishSourceFolderQueueProjection()
                }
            }
            enqueueQueueTransition { [weak self] in
                await self?.pumpDurableQueue()
            }
            return true
        } catch {
            return false
        }
    }

    func captureDiagnosticBundle(
        in outputDirectory: URL? = nil,
        userComment: DiagnosticUserComment? = nil
    ) async throws -> DiagnosticBundleArtifact {
        let capturedAt = diagnosticClock()
        let processSnapshot = client?.diagnosticSnapshot()
            ?? diagnosticRecorder.latestProcessSnapshot
        diagnosticRecorder.updateProcessSnapshot(processSnapshot)
        let snapshot = diagnosticRecorder.snapshot(
            capturedAt: capturedAt,
            lifecycle: state,
            activeMode: activeRunMode?.diagnosticName,
            batchSummary: diagnosticBatchSummary,
            process: processSnapshot,
            observabilityPersistence: observabilityEventStore.snapshot()
        )
        let builder = diagnosticBundleBuilder
        let buildTask = Task.detached(priority: .utility) {
            try builder.createBundle(
                from: snapshot,
                userComment: userComment,
                outputDirectory: outputDirectory
            )
        }
        return try await withTaskCancellationHandler {
            try await buildTask.value
        } onCancel: {
            buildTask.cancel()
        }
    }

    func selectSource(_ sourceURL: URL) {
        guard !hasActiveWork else {
            return
        }
        resetQueue()
        lastConversionDraft = nil
        batchQueue = nil
        guard let source = ConversionSource.infer(from: sourceURL) else {
            resetDiagnosticSession()
            self.source = nil
            state.selectSource(sourceURL.standardizedFileURL)
            state.failTransport(
                message: "Choose a 3D Blu-ray disc, ISO, Blu-ray folder, MKV, MTS, or M2TS source.",
                retryable: false
            )
            return
        }
        selectSource(source)
    }

    func selectSource(_ source: ConversionSource) {
        guard !hasActiveWork else {
            return
        }
        resetQueue()
        lastConversionDraft = nil
        resetDiagnosticSession()
        self.source = source
        state.clear()
        if source.kind == .sourceFolder {
            batchQueue = SourceFolderQueueState(
                folderSource: source,
                sources: SourceFolderDiscovery.discoverSources(in: source.url)
            )
            return
        }
        batchQueue = nil
        guard source.kind.supportsMetadataInspection else {
            return
        }
        state.selectSource(source.url)
        validateSelectedSourceAndStart()
    }

    func startInspection() {
        guard !hasActiveWork,
              let source,
              state.sourceURL == source.url
        else {
            return
        }
        startInspection(source: source, mode: .singleInspection)
    }

    private func startInspection(source: ConversionSource, mode: ActiveRunMode) {
        let job = WorkerJobSpec(source: source)
        do {
            try state.begin(jobID: job.jobID)
            liveObservabilityStatus = .empty
            pendingTerminalEvent = nil
            activeRunMode = mode
            diagnosticRecorder.beginJob(
                context: DiagnosticJobContext(jobID: job.jobID, source: source),
                lifecycle: state,
                activeMode: mode.diagnosticName,
                recordedAt: diagnosticClock()
            )
            trackDiagnosticJob(job.jobID, mode: mode)
            scheduleDiagnosticStorageSample(recordedAt: diagnosticClock(), force: true)
            let client = try clientFactory()
            self.client = client
            runTask = Task { [weak self] in
                guard let self else {
                    return
                }
                do {
                    let runResult = try await client.run(job: job) { [weak self] event in
                        guard let self else {
                            return
                        }
                        try self.receive(event)
                    }
                    self.finish(runResult)
                } catch {
                    self.fail(error)
                }
            }
        } catch {
            state.failTransport(message: error.localizedDescription)
            recordDiagnosticWorkflow(
                name: "job.launch_failed",
                mode: mode,
                message: error.localizedDescription,
                jobID: job.jobID
            )
            activeRunMode = nil
            clearActiveWorker(runDeferredActions: false)
            handleSynchronousRunFailure(mode)
        }
    }

    func startConversion(draft: ConversionDraft, jobID: UUID = UUID()) {
        guard !hasActiveWork else {
            return
        }
        guard !durableQueueStore.writesBlocked else {
            failClosedTitleQueuePersistence(ConversionQueueStoreError.recoveryRequired)
            return
        }
        guard conversionContextIsValid(for: draft, mode: .singleConversion) else {
            state.failTransport(
                message: "Analyze the selected source before starting conversion.",
                retryable: false
            )
            return
        }
        guard draft.source.kind.supportsConversion,
              sourceAvailabilityResolver(draft.source)
        else {
            state.failTransport(
                message: "Conversion requires an inserted Blu-ray disc or existing Blu-ray folder, ISO, MKV, MTS, or M2TS source.",
                retryable: false
            )
            return
        }
        if draft.source.kind == .physicalDisc,
           Self.isInsideSourceVolume(draft.destinationURL, sourceURL: draft.source.url)
        {
            state.failTransport(
                message: "Choose a destination outside the Blu-ray disc.",
                retryable: false
            )
            return
        }
        resetQueue()
        durableQueueStopRequested = false
        let itemID = UUID()
        enqueueQueueTransition { [weak self] in
            guard let self else { return }
            do {
                try await self.durableQueueStore.mutateItems { items in
                    items.append(DurableConversionQueueItem(
                        id: itemID,
                        ordinal: items.count,
                        origin: .singleSource,
                        intent: DurableQueueItemIntent(draft: draft),
                        inspection: draft.sourceDetails
                    ))
                }
                self.adoptedItemIDs.insert(itemID)
                self.durableSingleJobIDs[itemID] = jobID
                await self.pumpDurableQueue()
            } catch {
                self.failClosedTitleQueuePersistence(error)
            }
        }
    }

    func startConversionQueue(drafts: [ConversionDraft]) {
        _ = startConversionQueue(
            entries: drafts.map { QueueStartEntry(id: UUID(), draft: $0, resolutionTrace: nil) },
            preserveSingleEntry: false
        )
    }

    private struct QueueStartEntry {
        let id: UUID
        let draft: ConversionDraft
        let resolutionTrace: DurableQueueResolutionTrace?
    }

    private struct QueueSourceIdentity: Hashable {
        let kind: ConversionSourceKind
        let path: String
        let workerSourcePath: String?
        let mediaIdentifier: String?

        init(source: ConversionSource) {
            kind = source.kind
            path = source.url.path
            workerSourcePath = source.workerSourcePath
            mediaIdentifier = source.mediaIdentifier
        }
    }

    private func startConversionQueue(entries: [QueueStartEntry], preserveSingleEntry: Bool = false) -> Bool {
        guard !hasActiveWork,
              !durableQueueStore.writesBlocked,
              !entries.isEmpty,
              entries.allSatisfy({ entry in
                  if case .failure = RouteQualityEngine.validate(entry.draft.options) { return false }
                  return true
              }),
              let firstEntry = entries.first
        else {
            return false
        }
        if entries.count == 1, !preserveSingleEntry {
            startConversion(draft: firstEntry.draft)
            return true
        }
        let groupID = UUID()
        let sourcesRequestingRemoval = Set(entries.compactMap { entry in
            entry.draft.options.job.removeOriginalAfterSuccess
                ? QueueSourceIdentity(source: entry.draft.source)
                : nil
        })
        var finalRemovalIndexBySource: [QueueSourceIdentity: Int] = [:]
        for (offset, entry) in entries.enumerated() {
            let sourceIdentity = QueueSourceIdentity(source: entry.draft.source)
            if sourcesRequestingRemoval.contains(sourceIdentity) {
                finalRemovalIndexBySource[sourceIdentity] = offset
            }
        }
        let normalizedEntries = entries.enumerated().map { offset, entry in
            var options = entry.draft.options
            let sourceIdentity = QueueSourceIdentity(source: entry.draft.source)
            options.job.removeOriginalAfterSuccess = finalRemovalIndexBySource[sourceIdentity] == offset
            let draft = ConversionDraft(
                source: entry.draft.source,
                sourceDetails: entry.draft.sourceDetails,
                profile: entry.draft.profile,
                destinationURL: entry.draft.destinationURL,
                options: options,
                selectedTitle: entry.draft.selectedTitle
            )
            return QueueStartEntry(id: entry.id, draft: draft, resolutionTrace: entry.resolutionTrace)
        }
        activeQueueItemID = nil
        titleQueueGroupID = groupID
        titleQueueStopRequested = false
        durableQueueStopRequested = false
        durableQueueRuntimeDiagnostic = nil
        completedBatchResults = nil
        enqueueTitleQueueTransition { [weak self] in
            guard let self else {
                return
            }
            do {
                var admittedIDs: [UUID] = []
                try await self.durableQueueStore.mutateItems { items in
                    let startingOrdinal = items.count
                    let admitted = normalizedEntries.enumerated().map { offset, entry in
                        let item = DurableConversionQueueItem(
                            id: entry.id,
                            ordinal: startingOrdinal + offset,
                            groupID: groupID,
                            origin: entry.draft.source.kind == .sourceFolder
                                ? .sourceFolder
                                : (entry.draft.selectedTitle == nil ? .singleSource : .multiTitle),
                            intent: DurableQueueItemIntent(draft: entry.draft),
                            inspection: entry.draft.sourceDetails,
                            resolutionTrace: entry.resolutionTrace
                        )
                        admittedIDs.append(item.id)
                        return item
                    }
                    items.append(contentsOf: admitted)
                }
                self.adoptedItemIDs.formUnion(admittedIDs)
                self.publishTitleQueueProjection()
                if self.titleQueueStopRequested {
                    try await self.stopWaitingTitleQueueItems()
                    self.publishTitleQueueProjection()
                    return
                }
                await self.pumpDurableQueue()
            } catch {
                self.failClosedTitleQueuePersistence(error)
            }
        }
        return true
    }

    func moveWaitingQueueItem(_ itemID: UUID, before targetID: UUID) -> Bool {
        guard itemID != targetID,
              let groupID = titleQueueGroupID,
              !durableQueueStore.writesBlocked,
              currentTitleQueueItems.contains(where: { $0.id == itemID && $0.state == .waiting }),
              currentTitleQueueItems.contains(where: { $0.id == targetID && $0.state == .waiting })
        else {
            return false
        }
        enqueueTitleQueueTransition { [weak self] in
            guard let self else {
                return
            }
            do {
                guard self.currentTitleQueueItems.contains(where: { $0.id == itemID && $0.groupID == groupID }),
                      self.currentTitleQueueItems.contains(where: { $0.id == targetID && $0.groupID == groupID })
                else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                try await self.durableQueueStore.moveWaitingItem(itemID, before: targetID)
                self.publishTitleQueueProjection()
            } catch {
                self.durableQueueRuntimeDiagnostic = "Queue order could not be saved: \(error.localizedDescription)"
            }
        }
        return true
    }

    @discardableResult
    private func startConversion(
        draft: ConversionDraft,
        jobID: UUID = UUID(),
        mode: ActiveRunMode
    ) -> Bool {
        guard !hasActiveWorker else {
            return false
        }
        guard conversionContextIsValid(for: draft, mode: mode) else {
            state.failTransport(
                message: "Analyze the selected source before starting conversion.",
                retryable: false
            )
            handleSynchronousRunFailure(mode)
            return false
        }
        guard draft.source.kind.supportsConversion,
              sourceAvailabilityResolver(draft.source)
        else {
            state.failTransport(
                message: "Conversion requires an inserted Blu-ray disc or existing Blu-ray folder, ISO, MKV, MTS, or M2TS source.",
                retryable: false
            )
            handleSynchronousRunFailure(mode)
            return false
        }
        if draft.source.kind == .physicalDisc,
           Self.isInsideSourceVolume(draft.destinationURL, sourceURL: draft.source.url)
        {
            state.failTransport(
                message: "Choose a destination outside the Blu-ray disc.",
                retryable: false
            )
            handleSynchronousRunFailure(mode)
            return false
        }
        if case .titleQueueConversion = mode, let inspection = draft.sourceDetails {
            state.prepareQueuedConversion(sourceURL: draft.source.url, inspection: inspection)
        }
        let job = WorkerJobSpec(draft: draft, jobID: jobID)
        do {
            try state.begin(jobID: job.jobID, operationKind: .conversion)
            liveObservabilityStatus = .empty
            switch mode {
            case .singleConversion, .titleQueueConversion, .durableSingleConversion:
                lastConversionDraft = draft
            case .singleInspection, .titleQueueInspection, .batchInspection, .batchConversion, .durableSingleInspection:
                break
            }
            pendingTerminalEvent = nil
            activeRunMode = mode
            diagnosticRecorder.beginJob(
                context: DiagnosticJobContext(jobID: job.jobID, draft: draft),
                lifecycle: state,
                activeMode: mode.diagnosticName,
                recordedAt: diagnosticClock()
            )
            trackDiagnosticJob(job.jobID, mode: mode)
            scheduleDiagnosticStorageSample(recordedAt: diagnosticClock(), force: true)
            let client = try clientFactory()
            self.client = client
            runTask = Task { [weak self] in
                guard let self else {
                    return
                }
                do {
                    let runResult = try await client.run(job: job) { [weak self] event in
                        guard let self else {
                            return
                        }
                        try self.receive(event)
                    }
                    self.finish(runResult)
                } catch {
                    self.fail(error)
                }
            }
            return true
        } catch {
            state.failTransport(message: error.localizedDescription)
            recordDiagnosticWorkflow(
                name: "job.launch_failed",
                mode: mode,
                message: error.localizedDescription,
                jobID: job.jobID
            )
            activeRunMode = nil
            clearActiveWorker(runDeferredActions: false)
            handleSynchronousRunFailure(mode)
            return false
        }
    }

    private func conversionContextIsValid(
        for draft: ConversionDraft,
        mode: ActiveRunMode
    ) -> Bool {
        if case .titleQueueConversion = mode {
            return draft.sourceDetails != nil
        }
        guard state.sourceURL == draft.source.url, state.result != nil else {
            return false
        }
        switch mode {
        case let .batchConversion(itemID):
            return (batchQueue?.activeItemID == itemID
                && batchQueue?.activeItem?.source == draft.source)
                || durableQueueStore.items.contains(where: {
                    $0.id == itemID && $0.origin == .sourceFolder && $0.inspection != nil
                })
        case .singleConversion:
            return source == draft.source
        case .titleQueueConversion:
            return false
        case let .durableSingleConversion(itemID):
            return durableQueueStore.items.contains(where: {
                $0.id == itemID && $0.origin == .singleSource && $0.inspection != nil
            })
        case .singleInspection, .titleQueueInspection, .batchInspection, .durableSingleInspection:
            return false
        }
    }

    func startBatchConversion(
        profile: EncodingProfile,
        destinationURL: URL,
        options: ConversionOptions,
        titleSelection: DiscTitleSelection = .main
    ) {
        guard !hasActiveWork,
              !isBatchRunning,
              let source,
              source.kind == .sourceFolder,
              var queue = batchQueue,
              queue.folderSource == source
        else {
            return
        }

        if queue.hasStarted {
            queue = SourceFolderQueueState(
                folderSource: source,
                sources: SourceFolderDiscovery.discoverSources(in: source.url)
            )
        }
        guard !queue.items.isEmpty else {
            batchQueue = queue
            return
        }

        guard !durableQueueStore.writesBlocked else {
            failClosedSourceFolderQueuePersistence(ConversionQueueStoreError.recoveryRequired)
            return
        }

        let discTitleSelection: SourceFolderDiscTitleSelection = titleSelection.isAll
            ? .all3DVideos
            : .mainFeature

        queue.prepareForRun(
            profile: profile,
            destinationURL: destinationURL,
            options: options
        )
        batchQueue = queue
        sourceFolderQueueGroupID = nil
        sourceFolderStopRequested = false
        durableQueueStopRequested = false
        sourceFolderQueueCompletionPending = false
        sourceFolderRecoveryChoices.removeAll()
        recordDiagnosticWorkflow(name: "batch.started", message: "source_folder")
        enqueueSourceFolderQueueTransition { [weak self] in
            await self?.admitAndStartSourceFolderQueue(titleSelection: discTitleSelection)
        }
    }

    func stopActiveWorker() {
        guard hasStoppableWork else {
            return
        }
        persistentQueueRunState = .idle
        persistentQueueControlsActive = false
        if hasActiveWorker {
            state.requestStop()
            recordDiagnosticWorkflow(name: "cancel.requested", mode: activeRunMode, jobID: state.jobID)
            client?.cancel()

            switch activeRunMode {
            case .batchInspection, .batchConversion:
                durableQueueStopRequested = true
                sourceFolderStopRequested = true
                if var queue = batchQueue {
                    queue.stopRequested = true
                    queue.markPendingItemsStopped()
                    if let activeItemIndex = queue.activeItemIndex {
                        queue.items[activeItemIndex].status = .stopping
                    }
                    batchQueue = queue
                }
                enqueueSourceFolderQueueTransition { [weak self] in
                    await self?.persistSourceFolderStopRequest()
                }
            case .titleQueueInspection, .titleQueueConversion:
                durableQueueStopRequested = true
                titleQueueStopRequested = true
                if let activeQueueItemID {
                    enqueueTitleQueueTransition { [weak self] in
                        await self?.persistTitleQueueStop(itemID: activeQueueItemID)
                    }
                }
            case .durableSingleInspection, .durableSingleConversion:
                durableQueueStopRequested = true
                if let activeQueueItemID {
                    enqueueQueueTransition { [weak self] in
                        await self?.persistDurableSingleStop(itemID: activeQueueItemID)
                    }
                }
            case .singleInspection, .singleConversion, nil:
                break
            }
            return
        }

        durableQueueStopRequested = true
        if batchQueue?.hasStarted == true {
            sourceFolderStopRequested = true
            if var queue = batchQueue {
                queue.stopRequested = true
                queue.markPendingItemsStopped()
                if let activeItemIndex = queue.activeItemIndex {
                    queue.items[activeItemIndex].status = .stopping
                }
                batchQueue = queue
            }
            enqueueSourceFolderQueueTransition { [weak self] in
                await self?.persistSourceFolderStopRequest()
            }
            return
        }
        if titleQueueGroupID != nil,
           currentTitleQueueItems.contains(where: { adoptedItemIDs.contains($0.id) && $0.state == .waiting })
        {
            titleQueueStopRequested = true
            enqueueTitleQueueTransition { [weak self] in
                await self?.persistWaitingTitleQueueStop()
            }
            return
        }
        enqueueQueueTransition { [weak self] in
            await self?.stopUnstartedAdoptedItems()
        }
    }

    @discardableResult
    func stopAllPersistentQueue() -> PersistentQueueCommandOutcome {
        guard hasStoppableWork else {
            return .rejected(.noActiveItem)
        }
        stopActiveWorker()
        return .accepted(.idle)
    }

    func prepareForRetry() {
        guard !hasActiveWork else {
            return
        }
        let previousJobID = state.jobID
        state.prepareForRetry()
        recordDiagnosticWorkflow(name: "retry.prepared", jobID: previousJobID)
    }

    @discardableResult
    func resolveRecoveryChoice(_ choice: WorkerRecoveryChoice) -> Bool {
        guard !hasActiveWorker,
              !isBatchRunning,
              let decision = state.recoveryDecision,
              decision.supportedChoices.contains(choice)
        else {
            return false
        }
        if activeDurableQueueItem == nil, sourceFolderQueueGroupID != nil {
            return false
        }
        let decisionJobID = state.jobID
        recordDiagnosticWorkflow(
            name: "recovery.choice_selected",
            message: choice.rawValue,
            jobID: decisionJobID
        )
        if choice == .cancel {
            state.cancelRecoveryDecision()
            recordDiagnosticWorkflow(name: "recovery.cancelled", jobID: decisionJobID)
            if let activeItem = activeDurableQueueItem {
                enqueueQueueTransition { [weak self] in
                    guard let self else { return }
                    switch activeItem.origin {
                    case .singleSource:
                        await self.persistDurableSingleDecisionCancellation(itemID: activeItem.id)
                    case .multiTitle:
                        await self.persistTitleQueueDecisionCancellation(itemID: activeItem.id)
                    case .sourceFolder:
                        break
                    }
                }
            } else {
                runDeferredActionsIfIdle()
            }
            return true
        }
        guard let lastConversionDraft,
              let retryDraft = lastConversionDraft.retrying(decision: decision, choice: choice)
        else {
            state.failTransport(
                message: "This recovery option is not available for the current conversion.",
                retryable: false
            )
            if let activeItem = activeDurableQueueItem {
                enqueueQueueTransition { [weak self] in
                    guard let self else { return }
                    switch activeItem.origin {
                    case .singleSource:
                        await self.persistDurableSingleTerminal(
                            itemID: activeItem.id,
                            snapshot: self.sourceFolderTerminalSnapshot()
                        )
                    case .multiTitle:
                        await self.persistTitleQueueTerminalState(itemID: activeItem.id)
                    case .sourceFolder:
                        break
                    }
                }
            }
            return false
        }
        guard sourceAvailabilityResolver(retryDraft.source) else {
            state.failTransport(
                message: "Conversion requires an inserted Blu-ray disc or existing Blu-ray folder, ISO, MKV, MTS, or M2TS source.",
                retryable: false
            )
            if let activeItem = activeDurableQueueItem {
                enqueueQueueTransition { [weak self] in
                    await self?.persistSourceUnavailable(itemID: activeItem.id)
                }
            }
            return false
        }
        state.prepareForRetry()
        if let activeItem = activeDurableQueueItem {
            enqueueQueueTransition { [weak self] in
                guard let self else { return }
                switch activeItem.origin {
                case .singleSource:
                    await self.retryDurableSingleQueueItem(itemID: activeItem.id, draft: retryDraft, choice: choice)
                case .multiTitle:
                    await self.retryDurableTitleQueueItem(itemID: activeItem.id, draft: retryDraft, choice: choice)
                case .sourceFolder:
                    break
                }
            }
        } else {
            _ = startConversion(draft: retryDraft, mode: .singleConversion)
        }
        return activeDurableQueueItem != nil || state.phase.isRunning
    }

    func clearSource() {
        if state.phase == .decisionRequired, let activeItem = activeDurableQueueItem {
            state.cancelRecoveryDecision()
            enqueueQueueTransition { [weak self] in
                guard let self else { return }
                switch activeItem.origin {
                case .singleSource:
                    await self.persistClearedDurableSingleDecisionCancellation(itemID: activeItem.id)
                case .multiTitle:
                    await self.persistClearedTitleQueueDecisionCancellation(
                        itemID: activeItem.id,
                        groupID: activeItem.groupID
                    )
                case .sourceFolder:
                    break
                }
                self.activeQueueItemID = nil
                self.clearSourceNow()
            }
            return
        }
        guard !hasStoppableWork else {
            return
        }
        clearSourceNow()
    }

    private func clearSourceNow() {
        source = nil
        lastConversionDraft = nil
        batchQueue = nil
        resetQueue()
        state.clear()
        resetDiagnosticSession()
        runDeferredActionsIfIdle()
    }

    func sourceVolumeDidUnmount(_ volumeURL: URL) {
        guard let source,
              source.kind == .physicalDisc,
              source.url == volumeURL.standardizedFileURL
        else {
            return
        }
        if hasStoppableWork {
            stopActiveWorker()
        } else {
            if state.phase == .decisionRequired {
                _ = resolveRecoveryChoice(.cancel)
            }
            clearSource()
        }
    }

    func stopForQuit() async {
        guard hasActiveWork else {
            return
        }
        if state.phase == .decisionRequired, !hasActiveWorker {
            _ = resolveRecoveryChoice(.cancel)
        }
        stopActiveWorker()
        if let task = runTask {
            await task.value
        }
        if let pendingQueueTransition {
            await pendingQueueTransition.value
        }
        await waitForBatchQueueSettled()
        runDeferredActionsIfIdle()
    }

    func waitForBatchQueueSettled() async {
        while true {
            if let task = runTask {
                await task.value
                continue
            }
            if let transition = pendingQueueTransition {
                await transition.value
                continue
            }
            if let continuation = pendingBatchContinuation {
                await continuation.value
                continue
            }
            return
        }
    }

    func postponeInstallUntilIdle(_ installHandler: @escaping () -> Void) -> Bool {
        guard hasActiveWork else {
            return false
        }
        actionsWaitingForIdle.append(installHandler)
        return true
    }

    func retryBatchItem(_ itemID: UUID, recoveryChoice: WorkerRecoveryChoice? = nil) {
        guard !hasActiveWork,
              !durableQueueStore.writesBlocked,
              let queue = batchQueue,
              let itemIndex = queue.items.firstIndex(where: { $0.id == itemID }),
              queue.items[itemIndex].canRetry,
              sourceFolderQueueGroupID != nil
        else {
            return
        }
        recordDiagnosticWorkflow(
            name: "batch.retry_requested",
            message: recoveryChoice?.rawValue,
            jobID: batchItemDiagnosticJobIDs[itemID]
        )
        if var projected = batchQueue {
            projected.completionID = nil
            batchQueue = projected
        }
        sourceFolderStopRequested = false
        durableQueueStopRequested = false
        sourceFolderQueueCompletionPending = false
        enqueueSourceFolderQueueTransition { [weak self] in
            await self?.retrySourceFolderQueueItem(itemID: itemID, recoveryChoice: recoveryChoice)
        }
    }

    func restartInspection() {
        guard canRetry else {
            return
        }
        let previousJobID = state.jobID
        state.prepareForRetry()
        recordDiagnosticWorkflow(
            name: "retry.inspection_requested",
            jobID: previousJobID
        )
        validateSelectedSourceAndStart()
    }

    private func validateSelectedSourceAndStart() {
        guard let source, state.sourceURL == source.url else {
            state.failTransport(message: "Choose a source before continuing.", retryable: false)
            return
        }
        guard source.kind.supportsMetadataInspection else {
            state.failTransport(
                message: "Choose a Blu-ray disc, Blu-ray folder, ISO, MKV, MTS, or M2TS source.",
                retryable: false
            )
            return
        }
        guard FileManager.default.fileExists(atPath: source.url.path) else {
            state.failTransport(message: "The selected source no longer exists.", retryable: false)
            return
        }
        startInspection()
    }

    private static func isInsideSourceVolume(_ destinationURL: URL, sourceURL: URL) -> Bool {
        let destinationPath = destinationURL.standardizedFileURL.path
        let sourcePath = sourceURL.standardizedFileURL.path
        let sourcePrefix = sourcePath.hasSuffix("/") ? sourcePath : "\(sourcePath)/"
        return destinationPath == sourcePath || destinationPath.hasPrefix(sourcePrefix)
    }

    private func receive(_ event: WorkerEvent) throws {
        let recordedAt = diagnosticClock()
        if let observabilityEvent = event.payload.observabilityEvent {
            observabilityEventStore.append(observabilityEvent)
            liveObservabilityStatus.receive(observabilityEvent, receivedAt: recordedAt)
        }
        if event.type.isTerminal {
            diagnosticRecorder.record(
                event: event,
                lifecycle: state,
                activeMode: activeRunMode?.diagnosticName,
                recordedAt: recordedAt
            )
            scheduleDiagnosticStorageSample(recordedAt: recordedAt, force: true)
            pendingTerminalEvent = event
            return
        }
        var nextState = state
        try nextState.receive(event)
        state = nextState
        diagnosticRecorder.record(
            event: event,
            lifecycle: state,
            activeMode: activeRunMode?.diagnosticName,
            recordedAt: recordedAt
        )
        if event.type == .heartbeat || event.type == .stageStarted || event.type == .artifactReady {
            scheduleDiagnosticStorageSample(
                recordedAt: recordedAt,
                force: event.type == .artifactReady
            )
        }
    }

    private func finish(_ result: WorkerRunResult) {
        let completedMode = activeRunMode
        let completedJobID = result.terminalEvent.jobID
        let processSnapshot = result.diagnosticSnapshot == .empty
            ? client?.diagnosticSnapshot() ?? .empty
            : result.diagnosticSnapshot
        diagnosticRecorder.updateProcessSnapshot(processSnapshot)
        if let pendingTerminalEvent {
            do {
                var nextState = state
                try nextState.receive(pendingTerminalEvent)
                state = nextState
            } catch {
                state.failTransport(
                    message: error.localizedDescription,
                    details: result.diagnostics.isEmpty ? nil : result.diagnostics
                )
            }
        } else if state.phase == .stopping {
            state.completeStop()
        } else {
            state.failTransport(
                message: state.operationKind == .inspection
                    ? "The source analysis ended before results were available."
                    : "The conversion ended before an output was available.",
                details: result.diagnostics.isEmpty ? nil : result.diagnostics
            )
        }
        diagnosticRecorder.recordWorkflow(
            name: "process.exited",
            lifecycle: state,
            activeMode: completedMode?.diagnosticName,
            recordedAt: diagnosticClock(),
            jobID: completedJobID,
            exitStatus: result.exitStatus
        )
        scheduleDiagnosticStorageSample(recordedAt: diagnosticClock(), force: true)
        pendingTerminalEvent = nil
        activeRunMode = nil
        clearActiveWorker(runDeferredActions: false)
        if let completedMode {
            handleCompletedRun(completedMode)
        }
        runDeferredActionsIfIdle()
    }

    private func fail(_ error: Error) {
        let completedMode = activeRunMode
        let completedJobID = state.jobID ?? diagnosticRecorder.currentJobContext?.jobID
        if let processSnapshot = client?.diagnosticSnapshot() {
            diagnosticRecorder.updateProcessSnapshot(processSnapshot)
        }
        let clientError = error as? WorkerClientError
        if state.phase == .stopping {
            state.completeStop()
        } else {
            state.failTransport(
                message: error.localizedDescription,
                details: clientError?.technicalDetails
            )
        }
        diagnosticRecorder.recordWorkflow(
            name: "process.failed",
            lifecycle: state,
            activeMode: completedMode?.diagnosticName,
            recordedAt: diagnosticClock(),
            message: error.localizedDescription,
            details: clientError?.technicalDetails,
            jobID: completedJobID,
            exitStatus: clientError?.processExitStatus
        )
        scheduleDiagnosticStorageSample(recordedAt: diagnosticClock(), force: true)
        pendingTerminalEvent = nil
        activeRunMode = nil
        clearActiveWorker(runDeferredActions: false)
        if let completedMode {
            handleCompletedRun(completedMode)
        }
        runDeferredActionsIfIdle()
    }

    private func clearActiveWorker(runDeferredActions: Bool = true) {
        client = nil
        runTask = nil
        if runDeferredActions {
            runDeferredActionsIfIdle()
        }
    }

    private func handleCompletedRun(_ mode: ActiveRunMode) {
        switch mode {
        case .singleInspection, .singleConversion:
            return
        case let .titleQueueInspection(itemID):
            let snapshot = sourceFolderTerminalSnapshot()
            enqueueTitleQueueTransition { [weak self] in
                await self?.persistTitleQueueInspectionTerminal(itemID: itemID, snapshot: snapshot)
            }
        case let .durableSingleInspection(itemID):
            completeDurableSingleInspection(itemID: itemID)
        case let .durableSingleConversion(itemID):
            completeDurableSingleConversion(itemID: itemID)
        case let .titleQueueConversion(itemID):
            let snapshot = TitleQueueTerminalSnapshot(
                phase: state.phase,
                decision: state.recoveryDecision,
                failure: DurableQueueFailure(
                    code: state.failureCode,
                    message: state.failureMessage ?? "Conversion failed.",
                    details: state.failureDetails,
                    retryable: state.failureRetryable
                ),
                result: state.conversionResult.map(DurableQueueResult.init)
            )
            enqueueTitleQueueTransition { [weak self] in
                await self?.persistTitleQueueTerminalState(itemID: itemID, snapshot: snapshot)
            }
        case let .batchInspection(itemID):
            completeBatchInspection(itemID: itemID)
        case let .batchConversion(itemID):
            completeBatchConversion(itemID: itemID)
        }
    }

    private func handleSynchronousRunFailure(_ mode: ActiveRunMode) {
        switch mode {
        case .singleInspection, .singleConversion:
            handleCompletedRun(mode)
            runDeferredActionsIfIdle()
        case .titleQueueInspection:
            handleCompletedRun(mode)
        case .durableSingleInspection, .durableSingleConversion:
            handleCompletedRun(mode)
        case let .titleQueueConversion(itemID):
            guard activeQueueItemID == itemID else {
                return
            }
        case .batchInspection, .batchConversion:
            pendingBatchContinuation = Task { @MainActor [weak self] in
                await Task.yield()
                guard let self else {
                    return
                }
                self.pendingBatchContinuation = nil
                self.handleCompletedRun(mode)
                self.runDeferredActionsIfIdle()
            }
        }
    }

    private func completeBatchInspection(itemID: UUID) {
        let snapshot = sourceFolderTerminalSnapshot()
        enqueueSourceFolderQueueTransition { [weak self] in
            await self?.persistSourceFolderInspectionTerminal(itemID: itemID, snapshot: snapshot)
        }
    }

    private func completeBatchConversion(itemID: UUID) {
        let snapshot = sourceFolderTerminalSnapshot()
        enqueueSourceFolderQueueTransition { [weak self] in
            await self?.persistSourceFolderConversionTerminal(itemID: itemID, snapshot: snapshot)
        }
    }

    private func completeDurableSingleInspection(itemID: UUID) {
        let snapshot = sourceFolderTerminalSnapshot()
        enqueueQueueTransition { [weak self] in
            await self?.persistDurableSingleTerminal(itemID: itemID, snapshot: snapshot)
        }
    }

    private func completeDurableSingleConversion(itemID: UUID) {
        let snapshot = sourceFolderTerminalSnapshot()
        enqueueQueueTransition { [weak self] in
            await self?.persistDurableSingleTerminal(itemID: itemID, snapshot: snapshot)
        }
    }

    private func runDeferredActionsIfIdle() {
        guard !hasActiveWork else {
            return
        }
        let actions = actionsWaitingForIdle
        actionsWaitingForIdle.removeAll()
        for action in actions {
            action()
        }
    }

    private var currentSourceFolderQueueItems: [DurableConversionQueueItem] {
        guard let sourceFolderQueueGroupID else {
            return []
        }
        return durableQueueStore.items
            .filter { $0.groupID == sourceFolderQueueGroupID && $0.origin == .sourceFolder }
            .sorted { $0.ordinal < $1.ordinal }
    }

    private func sourceFolderQueueItem(id itemID: UUID) -> DurableConversionQueueItem? {
        guard let groupID = sourceFolderQueueGroupID else {
            return nil
        }
        for item in durableQueueStore.items
            where item.id == itemID && item.groupID == groupID && item.origin == .sourceFolder
        {
            return item
        }
        return nil
    }

    private func enqueueSourceFolderQueueTransition(_ transition: @escaping @MainActor () async -> Void) {
        enqueueQueueTransition(transition)
    }

    private func enqueueTitleQueueTransition(_ transition: @escaping @MainActor () async -> Void) {
        enqueueQueueTransition(transition)
    }

    private func enqueueQueueTransition(_ transition: @escaping @MainActor () async -> Void) {
        let previousTransition = pendingQueueTransition
        let transitionID = UUID()
        pendingQueueTransitionID = transitionID
        pendingQueueTransition = Task { @MainActor [weak self] in
            await previousTransition?.value
            guard !Task.isCancelled else {
                return
            }
            await transition()
            if self?.pendingQueueTransitionID == transitionID {
                self?.pendingQueueTransition = nil
                self?.pendingQueueTransitionID = nil
                self?.runDeferredActionsIfIdle()
            }
        }
    }

    private func admitAndStartSourceFolderQueue(titleSelection: SourceFolderDiscTitleSelection) async {
        guard let queue = batchQueue,
              queue.hasStarted,
              let source = self.source,
              source == queue.folderSource,
              source.kind == .sourceFolder
        else {
            return
        }
        let groupID = UUID()
        do {
            var admittedIDs: [UUID] = []
            try await durableQueueStore.mutateItems { items in
                let startingOrdinal = items.count
                let admitted = try queue.items.enumerated().map { offset, queueItem in
                    guard let draft = queueItem.draft else {
                        throw ConversionQueueStoreError.invalidDocument
                    }
                    return DurableConversionQueueItem(
                        id: UUID(),
                        ordinal: startingOrdinal + offset,
                        groupID: groupID,
                        origin: .sourceFolder,
                        intent: DurableQueueItemIntent(
                            draft: draft,
                            sourceFolderDiscTitleSelection: titleSelection
                        ),
                        state: sourceFolderStopRequested ? .notStarted : .waiting
                    )
                }
                var outputCounts: [String: Int] = [:]
                for item in admitted {
                    let outputKey = try conversionDraft(for: item, preserveStoredSourceRemoval: true)
                        .proposedOutputURL.standardizedFileURL.path.lowercased()
                    outputCounts[outputKey, default: 0] += 1
                }
                let resolvedItems = try admitted.map { item -> DurableConversionQueueItem in
                    var item = item
                    let proposedOutputURL = try conversionDraft(for: item, preserveStoredSourceRemoval: true)
                        .proposedOutputURL.standardizedFileURL
                    let outputKey = proposedOutputURL.path.lowercased()
                    if outputCounts[outputKey, default: 0] > 1 {
                        item.state = .failed
                        item.failure = DurableQueueFailure(
                            code: "output_collision",
                            message: "Another queued source would create the same output file.",
                            details: proposedOutputURL.path,
                            retryable: false
                        )
                    }
                    return item
                }
                items.append(contentsOf: resolvedItems)
                admittedIDs = resolvedItems.map(\.id)
            }
            adoptedItemIDs.formUnion(admittedIDs)
            sourceFolderQueueGroupID = groupID
            publishSourceFolderQueueProjection()
            await pumpDurableQueue()
        } catch {
            failClosedSourceFolderQueuePersistence(error)
        }
    }

    private func pumpSourceFolderQueue() async {
        await pumpDurableQueue()
    }

    private func pumpDurableQueue() async {
        if durableQueueStopRequested {
            await stopUnstartedAdoptedItems()
            persistentQueueRunState = .idle
            finishSourceFolderQueueIfNeeded()
            return
        }
        if persistentQueueRunState == .paused {
            await pauseDurableQueueNow()
            return
        }
        if persistentQueueRunState == .pauseAfterCurrent {
            await pauseDurableQueueNow()
            return
        }
        guard !hasActiveWorker, activeQueueItemID == nil else {
            return
        }
        if let offPeakRunWindowEnd, diagnosticClock() >= offPeakRunWindowEnd {
            self.offPeakRunWindowEnd = nil
            await pauseDurableQueueNow()
            return
        }
        guard let item = durableQueueStore.items
            .filter({ adoptedItemIDs.contains($0.id) && $0.state == .waiting })
            .min(by: { $0.ordinal < $1.ordinal })
        else {
            if persistentQueueRunState == .running {
                persistentQueueRunState = .idle
            }
            persistentQueueControlsActive = false
            offPeakRunWindowEnd = nil
            finishSourceFolderQueueIfNeeded()
            return
        }

        switch item.origin {
        case .multiTitle:
            await startDurableTitleQueueItem(item)
        case .sourceFolder:
            await startDurableSourceFolderQueueItem(item)
        case .singleSource:
            await startDurableSingleSourceItem(item)
        }
    }

    private func startDurableSingleSourceItem(_ item: DurableConversionQueueItem) async {
        do {
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == item.id }),
                      items[index].state == .waiting
                else { throw ConversionQueueStoreError.invalidDocument }
                let kind = ConversionSourceKind(rawValue: items[index].intent.source.kind)
                items[index].state = items[index].inspection == nil && kind?.supportsMetadataInspection == true
                    ? .inspecting
                    : .processing
                items[index].attempts.append(DurableQueueAttempt(
                    startedAt: diagnosticClock(),
                    recoveryChoice: sourceFolderRecoveryChoices.removeValue(forKey: item.id)
                ))
            }
            activeQueueItemID = item.id
            let updated = try durableQueueItem(id: item.id)
            let source = try durableSource(for: updated)
            guard sourceAvailabilityResolver(source) else {
                await persistSourceUnavailable(itemID: item.id)
                return
            }
            state.prepareQueuedConversion(sourceURL: source.url, inspection: updated.inspection)
            self.source = source
            if updated.state == .inspecting {
                startInspection(source: source, mode: .durableSingleInspection(itemID: item.id))
            } else {
                let draft = try conversionDraft(for: updated, preserveStoredSourceRemoval: true)
                let jobID = durableSingleJobIDs.removeValue(forKey: item.id) ?? UUID()
                _ = startConversion(draft: draft, jobID: jobID, mode: .durableSingleConversion(itemID: item.id))
            }
        } catch {
            failClosedTitleQueuePersistence(error)
        }
    }

    private func startDurableSourceFolderQueueItem(_ item: DurableConversionQueueItem) async {
        guard let groupID = item.groupID else {
            failClosedSourceFolderQueuePersistence(ConversionQueueStoreError.invalidDocument)
            return
        }
        sourceFolderQueueGroupID = groupID
        if sourceFolderStopRequested {
            await persistSourceFolderStopRequest()
            finishSourceFolderQueueIfNeeded()
            return
        }
        do {
            if item.inspection != nil, item.intent.selectedTitle != nil {
                try await durableQueueStore.mutateItems { items in
                    guard let index = items.firstIndex(where: { $0.id == item.id }),
                          items[index].groupID == groupID,
                          items[index].state == .waiting
                    else {
                        throw ConversionQueueStoreError.invalidDocument
                    }
                    items[index].state = .processing
                    items[index].attempts.append(DurableQueueAttempt(
                        startedAt: diagnosticClock(),
                        recoveryChoice: sourceFolderRecoveryChoices.removeValue(forKey: item.id)
                    ))
                }
                activeQueueItemID = item.id
                publishSourceFolderQueueProjection()
                if sourceFolderStopRequested {
                    await stopSourceFolderQueueBeforeSpawn(itemID: item.id)
                    return
                }
                try await startPersistedSourceFolderConversion(itemID: item.id)
                return
            }
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == item.id }),
                      items[index].groupID == groupID,
                      items[index].state == .waiting
                else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                let kind = ConversionSourceKind(rawValue: items[index].intent.source.kind)
                items[index].state = items[index].inspection == nil && kind?.supportsMetadataInspection == true
                    ? .inspecting
                    : .processing
                items[index].attempts.append(DurableQueueAttempt(
                    startedAt: diagnosticClock(),
                    recoveryChoice: sourceFolderRecoveryChoices.removeValue(forKey: item.id)
                ))
            }
            activeQueueItemID = item.id
            publishSourceFolderQueueProjection()
            if sourceFolderStopRequested {
                await stopSourceFolderQueueBeforeSpawn(itemID: item.id)
                return
            }
            let updated = try durableQueueItem(id: item.id)
            let source = try durableSource(for: updated)
            guard sourceAvailabilityResolver(source) else {
                await persistSourceUnavailable(itemID: item.id)
                return
            }
            state.clear()
            state.selectSource(source.url)
            if updated.state == .inspecting {
                startInspection(source: source, mode: .batchInspection(itemID: item.id))
            } else {
                state.prepareQueuedConversion(sourceURL: source.url, inspection: updated.inspection)
                let draft = try conversionDraft(for: updated, preserveStoredSourceRemoval: true)
                _ = startConversion(draft: draft, mode: .batchConversion(itemID: item.id))
            }
        } catch {
            failClosedSourceFolderQueuePersistence(error)
        }
    }

    private func persistSourceFolderInspectionTerminal(
        itemID: UUID,
        snapshot: SourceFolderTerminalSnapshot
    ) async {
        guard let groupID = sourceFolderQueueGroupID else {
            return
        }
        do {
            let item = sourceFolderQueueItem(id: itemID)
            guard let item else {
                throw ConversionQueueStoreError.invalidDocument
            }
            if sourceFolderStopRequested || snapshot.phase == .cancelled {
                try await persistSourceFolderTerminal(itemID: itemID, snapshot: snapshot, stopped: true)
                await pumpSourceFolderQueue()
                return
            }
            guard snapshot.phase == .completed,
                  let inspection = snapshot.inspection
            else {
                try await persistSourceFolderTerminal(itemID: itemID, snapshot: snapshot)
                await pumpSourceFolderQueue()
                return
            }
            try await persistCompletedSourceFolderInspection(
                itemID: itemID,
                groupID: groupID,
                item: item,
                inspection: inspection
            )
        } catch {
            failClosedSourceFolderQueuePersistence(error)
        }
    }

    private func persistCompletedSourceFolderInspection(
        itemID: UUID,
        groupID: UUID,
        item: DurableConversionQueueItem,
        inspection: SourceInspection
    ) async throws {
        let inspectedDraft = try conversionDraft(for: item, preserveStoredSourceRemoval: true)
            .withSourceDetails(inspection)
        let titleSelection = item.intent.sourceFolderDiscTitleSelection ?? .mainFeature
        let resolvedDrafts: [(draft: ConversionDraft, titleIndex: Int?)]
        if inspectedDraft.source.kind.isDiscWorkflow {
            let resolvedTitles: [(offset: Int?, element: SourceTitle)]
            if let titleIndex = item.intent.sourceFolderTitleIndex {
                let previousTitle = item.intent.selectedTitle
                let resolvedIndex = if let previousTitle {
                    inspection.titles.firstIndex(where: { $0.id == previousTitle.id })
                        ?? inspection.titles.firstIndex(where: { $0.outputName == previousTitle.outputName })
                        ?? (previousTitle.mainFeature
                            ? inspection.titles.firstIndex(where: \.mainFeature)
                            : inspection.titles.firstIndex(where: { $0.name == previousTitle.name }))
                } else {
                    inspection.titles.indices.contains(titleIndex) ? titleIndex : nil
                }
                guard let resolvedIndex else {
                    let failure = SourceFolderTerminalSnapshot(
                        phase: .failed,
                        inspection: inspection,
                        decision: nil,
                        failure: DurableQueueFailure(
                            code: "title_unavailable",
                            message: "The previously selected 3D video is no longer available.",
                            details: "Analyze the source again after confirming the disc title list has not changed.",
                            retryable: true
                        ),
                        result: nil
                    )
                    try await persistSourceFolderTerminal(itemID: itemID, snapshot: failure)
                    await pumpSourceFolderQueue()
                    return
                }
                resolvedTitles = [(resolvedIndex, inspection.titles[resolvedIndex])]
            } else if titleSelection == .all3DVideos {
                resolvedTitles = inspection.titles.enumerated().map { ($0.offset, $0.element) }
            } else if let mainTitle = inspection.mainTitle {
                resolvedTitles = [(nil, mainTitle)]
            } else {
                resolvedTitles = []
            }
            guard !resolvedTitles.isEmpty else {
                let failure = SourceFolderTerminalSnapshot(
                    phase: .failed,
                    inspection: inspection,
                    decision: nil,
                    failure: DurableQueueFailure(
                        code: "no_convertible_title",
                        message: "No convertible 3D title was found in this source.",
                        details: "Analyze the source again after confirming it contains an MVC Blu-ray title.",
                        retryable: true
                    ),
                    result: nil
                )
                try await persistSourceFolderTerminal(itemID: itemID, snapshot: failure)
                await pumpSourceFolderQueue()
                return
            }
            var inspectedOptions = inspectedDraft.options
            if inspection.titles.count > 1 {
                inspectedOptions.job.removeOriginalAfterSuccess = false
            }
            resolvedDrafts = resolvedTitles.map { titleIndex, title in
                (
                    ConversionDraft(
                        source: inspectedDraft.source,
                        sourceDetails: inspection,
                        profile: inspectedDraft.profile,
                        destinationURL: inspectedDraft.destinationURL,
                        options: inspectedOptions,
                        selectedTitle: title
                    ),
                    titleIndex
                )
            }
        } else {
            resolvedDrafts = [(inspectedDraft, nil)]
        }
        var resolvedItemIDs: [UUID] = []
        try await durableQueueStore.mutateItems { items in
            guard let index = items.firstIndex(where: { $0.id == itemID }),
                  items[index].groupID == groupID,
                  items[index].state == .inspecting
            else {
                throw ConversionQueueStoreError.invalidDocument
            }
            var existingOutputOwners: [String: String] = [:]
            for candidate in items where candidate.id != itemID
                && candidate.groupID == groupID
                && candidate.origin == .sourceFolder
                && candidate.inspection != nil
                && candidate.state != .notStarted
                && candidate.state != .stopped
            {
                guard let candidateDraft = try? conversionDraft(for: candidate, preserveStoredSourceRemoval: true) else {
                    continue
                }
                let outputKey = candidateDraft.proposedOutputURL.standardizedFileURL.path.lowercased()
                existingOutputOwners[outputKey] = candidate.intent.selectedTitle.map {
                    "\(candidate.intent.source.displayName) — \($0.name)"
                } ?? candidate.intent.source.displayName
            }

            var outputCounts: [String: Int] = [:]
            for resolved in resolvedDrafts {
                let outputKey = resolved.draft.proposedOutputURL.standardizedFileURL.path.lowercased()
                outputCounts[outputKey, default: 0] += 1
            }

            var resolvedItems: [DurableConversionQueueItem] = []
            for (offset, resolved) in resolvedDrafts.enumerated() {
                var resolvedItem = offset == 0
                    ? items[index]
                    : DurableConversionQueueItem(
                        ordinal: items[index].ordinal + offset,
                        groupID: groupID,
                        origin: .sourceFolder,
                        intent: DurableQueueItemIntent(
                            draft: resolved.draft,
                            sourceFolderDiscTitleSelection: titleSelection,
                            sourceFolderTitleIndex: resolved.titleIndex
                        ),
                        inspection: inspection
                    )
                if offset == 0 {
                    if let inspectionAttempt = resolvedItem.attempts.lastIndex(where: { $0.endedAt == nil }) {
                        resolvedItem.attempts[inspectionAttempt].endedAt = diagnosticClock()
                    }
                    resolvedItem.inspection = inspection
                    resolvedItem.intent = DurableQueueItemIntent(
                        draft: resolved.draft,
                        sourceFolderDiscTitleSelection: titleSelection,
                        sourceFolderTitleIndex: resolved.titleIndex
                    )
                }
                let outputURL = resolved.draft.proposedOutputURL.standardizedFileURL
                let outputKey = outputURL.path.lowercased()
                let conflictingOwner = existingOutputOwners[outputKey]
                if outputCounts[outputKey, default: 0] > 1 || conflictingOwner != nil {
                    resolvedItem.state = .failed
                    resolvedItem.failure = DurableQueueFailure(
                        code: "output_collision",
                        message: "Another queued source resolves to the same output file.",
                        details: conflictingOwner.map { "\(outputURL.path) is already reserved by \($0)." }
                            ?? "Multiple 3D videos resolve to \(outputURL.path).",
                        retryable: false
                    )
                } else {
                    resolvedItem.state = offset == 0 ? .processing : .waiting
                    resolvedItem.decision = nil
                    resolvedItem.failure = nil
                    if offset == 0 {
                        resolvedItem.attempts.append(DurableQueueAttempt(startedAt: diagnosticClock()))
                    }
                }
                resolvedItems.append(resolvedItem)
            }

            items.replaceSubrange(index ... index, with: resolvedItems)
            resolvedItemIDs = resolvedItems.map(\.id)
            for itemIndex in items.indices {
                items[itemIndex].ordinal = itemIndex
            }
        }
        adoptedItemIDs.formUnion(resolvedItemIDs)
        publishSourceFolderQueueProjection()
        try await startPersistedSourceFolderConversion(itemID: itemID)
    }

    private func startPersistedSourceFolderConversion(itemID: UUID) async throws {
        let updated = sourceFolderQueueItem(id: itemID)
        guard let updated else {
            if activeQueueItemID == itemID {
                activeQueueItemID = nil
            }
            await pumpSourceFolderQueue()
            return
        }
        guard updated.state == .processing else {
            if activeQueueItemID == itemID {
                activeQueueItemID = nil
            }
            await pumpSourceFolderQueue()
            return
        }
        if sourceFolderStopRequested {
            await stopSourceFolderQueueBeforeSpawn(itemID: itemID)
            return
        }
        let conversionDraft = try conversionDraft(for: updated, preserveStoredSourceRemoval: true)
        guard sourceAvailabilityResolver(conversionDraft.source) else {
            await persistSourceUnavailable(itemID: itemID)
            return
        }
        guard !hasActiveWorker else {
            throw ConversionQueueStoreError.invalidDocument
        }
        state.prepareQueuedConversion(
            sourceURL: conversionDraft.source.url,
            inspection: updated.inspection
        )
        _ = startConversion(draft: conversionDraft, mode: .batchConversion(itemID: itemID))
    }

    private func persistSourceFolderConversionTerminal(
        itemID: UUID,
        snapshot: SourceFolderTerminalSnapshot
    ) async {
        do {
            try await persistSourceFolderTerminal(
                itemID: itemID,
                snapshot: snapshot,
                stopped: snapshot.phase == .cancelled
                    || (sourceFolderStopRequested && snapshot.phase != .completed)
            )
            await pumpSourceFolderQueue()
        } catch {
            failClosedSourceFolderQueuePersistence(error)
        }
    }

    private func persistDurableSingleTerminal(
        itemID: UUID,
        snapshot: SourceFolderTerminalSnapshot
    ) async {
        guard activeQueueItemID == itemID else {
            return
        }
        do {
            var remainsRunnable = false
            var requiresDecision = false
            var groupID: UUID?
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }),
                      items[index].origin == .singleSource
                else { throw ConversionQueueStoreError.invalidDocument }
                groupID = items[index].groupID
                if let attemptIndex = items[index].attempts.lastIndex(where: { $0.endedAt == nil }) {
                    items[index].attempts[attemptIndex].endedAt = diagnosticClock()
                }
                if snapshot.phase == .completed, let inspection = snapshot.inspection,
                   items[index].state == .inspecting
                {
                    items[index].inspection = inspection
                    items[index].state = .waiting
                    items[index].failure = nil
                    items[index].decision = nil
                    remainsRunnable = true
                    return
                }
                if snapshot.phase == .completed, let result = snapshot.result {
                    items[index].state = .completed
                    items[index].result = result
                    items[index].failure = nil
                    items[index].decision = nil
                } else if let decision = snapshot.decision {
                    items[index].state = .attention
                    items[index].decision = DurableQueueDecision(decision: decision)
                    items[index].failure = nil
                    requiresDecision = true
                } else {
                    items[index].state = snapshot.phase == .cancelled ? .stopped : .failed
                    items[index].failure = snapshot.phase == .cancelled ? nil : snapshot.failure
                    items[index].decision = nil
                }
            }
            publishTitleQueueProjection()
            if requiresDecision {
                if persistentQueueControlsActive || hasAdoptedWaitingDurableQueueItem(excluding: itemID) {
                    parkActiveDurableQueueItemForResume()
                    await pumpDurableQueue()
                }
                return
            }
            activeQueueItemID = nil
            if !remainsRunnable {
                releaseAdoption(itemID)
                if groupID != nil,
                   titleQueueGroupID == groupID,
                   !currentTitleQueueItems.contains(where: {
                       adoptedItemIDs.contains($0.id) && $0.state == .waiting
                   })
                {
                    publishCompletedTitleQueueResults()
                }
            }
            await pumpDurableQueue()
        } catch {
            failClosedTitleQueuePersistence(error)
        }
    }

    private func persistSourceFolderTerminal(
        itemID: UUID,
        snapshot: SourceFolderTerminalSnapshot,
        stopped: Bool = false
    ) async throws {
        guard let groupID = sourceFolderQueueGroupID else {
            throw ConversionQueueStoreError.invalidDocument
        }
        try await durableQueueStore.mutateItems { items in
            guard let index = items.firstIndex(where: { $0.id == itemID }),
                  items[index].groupID == groupID
            else {
                throw ConversionQueueStoreError.invalidDocument
            }
            if let attemptIndex = items[index].attempts.lastIndex(where: { $0.endedAt == nil }) {
                items[index].attempts[attemptIndex].endedAt = diagnosticClock()
            }
            if stopped {
                items[index].state = .stopped
                items[index].decision = nil
                items[index].failure = nil
            } else if snapshot.phase == .completed, let result = snapshot.result {
                items[index].state = .completed
                items[index].result = result
                items[index].decision = nil
                items[index].failure = nil
            } else if let decision = snapshot.decision {
                items[index].state = .attention
                items[index].failure = nil
                items[index].decision = DurableQueueDecision(decision: decision)
            } else {
                items[index].state = .failed
                items[index].failure = snapshot.failure
                items[index].decision = nil
            }
        }
        if activeQueueItemID == itemID {
            activeQueueItemID = nil
        }
        releaseAdoption(itemID)
        publishSourceFolderQueueProjection()
    }

    private func persistSourceFolderStopRequest() async {
        guard let groupID = sourceFolderQueueGroupID else {
            if var queue = batchQueue, queue.hasStarted {
                queue.markPendingItemsStopped()
                if let activeItemIndex = queue.activeItemIndex {
                    queue.items[activeItemIndex].status = .stopping
                } else if let firstPendingIndex = queue.nextPendingIndex {
                    queue.items[firstPendingIndex].status = .stopped
                }
                batchQueue = queue
            }
            return
        }
        do {
            var stoppedItemIDs: Set<UUID> = []
            try await durableQueueStore.mutateItems { items in
                for index in items.indices where items[index].groupID == groupID && items[index].origin == .sourceFolder {
                    switch items[index].state {
                    case .waiting:
                        items[index].state = .notStarted
                        stoppedItemIDs.insert(items[index].id)
                    case .inspecting, .processing:
                        items[index].state = .stopping
                    default:
                        break
                    }
                }
            }
            for itemID in stoppedItemIDs {
                releaseAdoption(itemID)
            }
            publishSourceFolderQueueProjection()
            if !hasActiveWorker {
                await stopUnstartedAdoptedItems()
            }
        } catch {
            failClosedSourceFolderQueuePersistence(error)
        }
    }

    private func stopSourceFolderQueueBeforeSpawn(itemID: UUID) async {
        guard let groupID = sourceFolderQueueGroupID else {
            return
        }
        do {
            var stoppedItemIDs: Set<UUID> = []
            try await durableQueueStore.mutateItems { items in
                guard let activeIndex = items.firstIndex(where: { $0.id == itemID }),
                      items[activeIndex].groupID == groupID,
                      items[activeIndex].origin == .sourceFolder
                else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[activeIndex].state = .stopped
                stoppedItemIDs.insert(items[activeIndex].id)
                items[activeIndex].decision = nil
                items[activeIndex].failure = nil
                if let attemptIndex = items[activeIndex].attempts.lastIndex(where: { $0.endedAt == nil }) {
                    items[activeIndex].attempts[attemptIndex].endedAt = diagnosticClock()
                }
                for index in items.indices where items[index].groupID == groupID && items[index].state == .waiting {
                    items[index].state = .notStarted
                    stoppedItemIDs.insert(items[index].id)
                }
            }
            for stoppedItemID in stoppedItemIDs {
                releaseAdoption(stoppedItemID)
            }
            if activeQueueItemID == itemID {
                activeQueueItemID = nil
            }
            await stopUnstartedAdoptedItems()
            publishSourceFolderQueueProjection()
            finishSourceFolderQueueIfNeeded()
        } catch {
            failClosedSourceFolderQueuePersistence(error)
        }
    }

    private func retrySourceFolderQueueItem(itemID: UUID, recoveryChoice: WorkerRecoveryChoice?) async {
        guard let groupID = sourceFolderQueueGroupID else {
            return
        }
        do {
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }),
                      items[index].groupID == groupID,
                      (items[index].state == .failed || items[index].state == .attention)
                else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                let discTitleSelection = items[index].intent.sourceFolderDiscTitleSelection
                let titleIndex = items[index].intent.sourceFolderTitleIndex
                var draft = try conversionDraft(for: items[index], preserveStoredSourceRemoval: true)
                if let decision = items[index].decision {
                    guard let recoveryChoice,
                          let recoveredDraft = draft.retrying(
                              decision: WorkerDecision(
                                  identifier: decision.identifier,
                                  prompt: decision.prompt,
                                  choices: decision.choices,
                                  details: decision.details
                              ),
                              choice: recoveryChoice
                          )
                    else {
                        throw ConversionQueueStoreError.invalidDocument
                    }
                    draft = recoveredDraft
                } else if recoveryChoice != nil {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[index].inspection = nil
                draft = ConversionDraft(
                    source: draft.source,
                    sourceDetails: nil,
                    profile: draft.profile,
                    destinationURL: draft.destinationURL,
                    options: draft.options,
                    selectedTitle: draft.selectedTitle
                )
                items[index].intent = DurableQueueItemIntent(
                    draft: draft,
                    sourceFolderDiscTitleSelection: discTitleSelection,
                    sourceFolderTitleIndex: titleIndex
                )
                items[index].state = .waiting
                items[index].decision = nil
                items[index].failure = nil
                items[index].result = nil
            }
            if let recoveryChoice {
                sourceFolderRecoveryChoices[itemID] = recoveryChoice.rawValue
            }
            adoptedItemIDs.insert(itemID)
            persistentQueueControlsActive = true
            persistentQueueRunState = .running
            publishSourceFolderQueueProjection()
            await pumpSourceFolderQueue()
        } catch {
            failClosedSourceFolderQueuePersistence(error)
        }
    }

    private func finishSourceFolderQueueIfNeeded() {
        guard !hasActiveWorker,
              !sourceFolderQueueCompletionPending,
              let queue = batchQueue,
              queue.hasStarted,
              !currentSourceFolderQueueItems.isEmpty,
              !currentSourceFolderQueueItems.contains(where: { item in
                  item.state == .waiting || item.state == .inspecting || item.state == .processing || item.state == .stopping
              })
        else {
            return
        }
        sourceFolderQueueCompletionPending = true
        if var projected = batchQueue {
            projected.completionID = UUID()
            batchQueue = projected
        }
        recordDiagnosticWorkflow(name: "batch.finished")
        state.clear()
    }

    private func sourceFolderSource(for item: DurableConversionQueueItem) throws -> ConversionSource {
        guard let kind = ConversionSourceKind(rawValue: item.intent.source.kind) else {
            throw ConversionQueueStoreError.invalidDocument
        }
        return ConversionSource(
            kind: kind,
            url: URL(fileURLWithPath: item.intent.source.path),
            displayName: item.intent.source.displayName,
            workerSourcePath: item.intent.source.workerSourcePath,
            mediaIdentifier: item.intent.source.mediaIdentifier
        )
    }

    private func sourceFolderTerminalSnapshot() -> SourceFolderTerminalSnapshot {
        SourceFolderTerminalSnapshot(
            phase: state.phase,
            inspection: state.result,
            decision: state.recoveryDecision,
            failure: DurableQueueFailure(
                code: state.failureCode,
                message: state.failureMessage ?? state.recoveryDecision?.prompt ?? "The queued source could not be processed.",
                details: state.failureDetails ?? state.recoveryDecision?.details,
                retryable: state.failureRetryable || state.recoveryDecision != nil
            ),
            result: state.conversionResult.map(DurableQueueResult.init)
        )
    }

    private func publishSourceFolderQueueProjection() {
        guard let existingQueue = batchQueue else {
            return
        }
        guard let groupID = sourceFolderQueueGroupID else {
            return
        }
        let items = durableQueueStore.items
            .filter { $0.groupID == groupID && $0.origin == .sourceFolder }
            .sorted { $0.ordinal < $1.ordinal }
            .compactMap(sourceFolderProjectionItem)
        batchQueue = SourceFolderQueueState(
            folderSource: existingQueue.folderSource,
            items: items,
            activeItemID: items.first(where: { item in
                item.status == .inspecting || item.status == .converting || item.status == .stopping
            })?.id,
            stopRequested: sourceFolderStopRequested,
            hasStarted: true,
            completionID: existingQueue.completionID
        )
    }

    private func sourceFolderProjectionItem(_ item: DurableConversionQueueItem) -> SourceFolderQueueItem? {
        guard let source = try? sourceFolderSource(for: item) else {
            return nil
        }
        var projected = SourceFolderQueueItem(id: item.id, source: source)
        projected.draft = try? conversionDraft(for: item, preserveStoredSourceRemoval: true)
        projected.inspection = item.inspection
        projected.conversionResult = item.result.map {
            ConversionResult(
                outputPath: $0.outputPath,
                durationSeconds: $0.durationSeconds,
                sizeBytes: $0.sizeBytes,
                titleID: $0.titleID
            )
        }
        projected.failureMessage = item.failure?.message
        projected.failureDetails = item.failure?.details
        projected.failureRetryable = item.failure?.retryable ?? false
        projected.recoveryDecision = item.decision.map {
            WorkerDecision(identifier: $0.identifier, prompt: $0.prompt, choices: $0.choices, details: $0.details)
        }
        projected.status = switch item.state {
        case .waiting, .interrupted:
            .pending
        case .needsChoice:
            .failed
        case .inspecting:
            .inspecting
        case .processing:
            .converting
        case .stopping:
            .stopping
        case .completed:
            .completed
        case .failed, .attention:
            .failed
        case .stopped:
            .stopped
        case .notStarted:
            .notStarted
        }
        return projected
    }

    private func failClosedSourceFolderQueuePersistence(_ error: Error) {
        durableQueueRuntimeDiagnostic = "Queue changes are unavailable: \(error.localizedDescription)"
        persistentQueueRunState = .idle
        persistentQueueControlsActive = false
        offPeakRunWindowEnd = nil
        if let projected = batchQueue {
            if sourceFolderQueueGroupID == nil {
                batchQueue = SourceFolderQueueState(
                    folderSource: projected.folderSource,
                    sources: projected.items.map(\.source)
                )
            } else {
                var failedProjection = projected
                failedProjection.activeItemID = nil
                batchQueue = failedProjection
            }
        }
        sourceFolderQueueGroupID = nil
        activeQueueItemID = nil
        sourceFolderStopRequested = false
        sourceFolderQueueCompletionPending = false
        adoptedItemIDs.removeAll()
        durableSingleJobIDs.removeAll()
        sourceFolderRecoveryChoices.removeAll()
        durableQueueStopRequested = false
        state.failTransport(message: durableQueueRuntimeDiagnostic ?? "Queue changes are unavailable.", retryable: false)
        runDeferredActionsIfIdle()
    }

    private var currentTitleQueueItems: [DurableConversionQueueItem] {
        guard let titleQueueGroupID else {
            return []
        }
        return durableQueueStore.items
            .filter { $0.groupID == titleQueueGroupID && $0.origin != .sourceFolder }
            .sorted { $0.ordinal < $1.ordinal }
    }

    private var activeDurableQueueItem: DurableConversionQueueItem? {
        guard let activeQueueItemID else {
            return nil
        }
        return durableQueueStore.items.first(where: { $0.id == activeQueueItemID })
    }

    private func startNextDurableTitleQueueItem() async {
        await pumpDurableQueue()
    }

    private func startDurableTitleQueueItem(_ item: DurableConversionQueueItem) async {
        guard !hasActiveWorker,
              activeQueueItemID == nil,
              item.origin == .multiTitle
        else { return }
        guard let groupID = item.groupID else {
            failClosedTitleQueuePersistence(ConversionQueueStoreError.invalidDocument)
            return
        }
        titleQueueGroupID = groupID

        do {
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == item.id }),
                      items[index].state == .waiting
                else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                let sourceKind = ConversionSourceKind(rawValue: items[index].intent.source.kind)
                items[index].state = items[index].inspection == nil && sourceKind?.supportsMetadataInspection == true
                    ? .inspecting
                    : .processing
                items[index].attempts.append(DurableQueueAttempt(
                    startedAt: diagnosticClock(),
                    recoveryChoice: sourceFolderRecoveryChoices.removeValue(forKey: item.id)
                ))
            }
        } catch {
            failClosedTitleQueuePersistence(error)
            return
        }

        activeQueueItemID = item.id
        publishTitleQueueProjection()
        if titleQueueStopRequested {
            await stopDurableTitleQueueBeforeSpawn(itemID: item.id)
            return
        }
        let updated = try? durableQueueItem(id: item.id)
        guard let updated,
              let source = try? durableSource(for: updated),
              sourceAvailabilityResolver(source)
        else {
            await persistSourceUnavailable(itemID: item.id)
            return
        }
        state.prepareQueuedConversion(sourceURL: source.url, inspection: updated.inspection)
        self.source = source
        if updated.state == .inspecting {
            startInspection(source: source, mode: .titleQueueInspection(itemID: item.id))
        } else {
            guard let draft = try? conversionDraft(
                for: updated,
                removeOriginalAfterSuccess: shouldRemoveOriginalAfterSuccess(for: updated)
            ) else {
                failClosedTitleQueuePersistence(ConversionQueueStoreError.invalidDocument)
                return
            }
            if !startConversion(draft: draft, mode: .titleQueueConversion(itemID: item.id)) {
                await persistTitleQueueTerminalState(itemID: item.id)
            }
        }
    }

    private func persistTitleQueueInspectionTerminal(
        itemID: UUID,
        snapshot: SourceFolderTerminalSnapshot
    ) async {
        guard activeQueueItemID == itemID else {
            return
        }
        do {
            var remainsRunnable = false
            var requiresDecision = false
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }),
                      items[index].origin == .multiTitle
                else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                if let attemptIndex = items[index].attempts.lastIndex(where: { $0.endedAt == nil }) {
                    items[index].attempts[attemptIndex].endedAt = diagnosticClock()
                }
                if snapshot.phase == .completed, let inspection = snapshot.inspection {
                    items[index].inspection = inspection
                    items[index].state = .waiting
                    items[index].failure = nil
                    items[index].decision = nil
                    remainsRunnable = true
                } else if let decision = snapshot.decision {
                    items[index].state = .attention
                    items[index].decision = DurableQueueDecision(decision: decision)
                    items[index].failure = nil
                    requiresDecision = true
                } else {
                    items[index].state = snapshot.phase == .cancelled ? .stopped : .failed
                    items[index].failure = snapshot.phase == .cancelled ? nil : snapshot.failure
                    items[index].decision = nil
                }
            }
            publishTitleQueueProjection()
            if requiresDecision {
                if persistentQueueControlsActive || hasAdoptedWaitingDurableQueueItem(excluding: itemID) {
                    parkActiveDurableQueueItemForResume()
                    await pumpDurableQueue()
                }
                return
            }
            activeQueueItemID = nil
            if !remainsRunnable {
                releaseAdoption(itemID)
            }
            await pumpDurableQueue()
        } catch {
            failClosedTitleQueuePersistence(error)
        }
    }

    private func persistTitleQueueTerminalState(
        itemID: UUID,
        snapshot: TitleQueueTerminalSnapshot? = nil
    ) async {
        guard activeQueueItemID == itemID else {
            return
        }
        do {
            let terminalSnapshot = snapshot ?? TitleQueueTerminalSnapshot(
                phase: state.phase,
                decision: state.recoveryDecision,
                failure: DurableQueueFailure(
                    code: state.failureCode,
                    message: state.failureMessage ?? "Conversion failed.",
                    details: state.failureDetails,
                    retryable: state.failureRetryable
                ),
                result: state.conversionResult.map(DurableQueueResult.init)
            )
            var effectivePhase = terminalSnapshot.phase
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }) else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                if let attemptIndex = items[index].attempts.lastIndex(where: { $0.endedAt == nil }) {
                    items[index].attempts[attemptIndex].endedAt = diagnosticClock()
                }
                switch terminalSnapshot.phase {
                case .completed:
                    guard let result = terminalSnapshot.result else {
                        items[index].state = .failed
                        items[index].failure = DurableQueueFailure(
                            code: nil,
                            message: "The conversion completed without an output result.",
                            details: nil,
                            retryable: false
                        )
                        effectivePhase = .failed
                        return
                    }
                    items[index].state = .completed
                    items[index].result = result
                    items[index].failure = nil
                    items[index].decision = nil
                case .decisionRequired:
                    guard let decision = terminalSnapshot.decision else {
                        throw ConversionQueueStoreError.invalidDocument
                    }
                    items[index].state = .attention
                    items[index].decision = DurableQueueDecision(decision: decision)
                    items[index].failure = nil
                case .cancelled:
                    items[index].state = .stopped
                    items[index].decision = nil
                    items[index].failure = nil
                case .failed:
                    items[index].state = .failed
                    items[index].failure = terminalSnapshot.failure
                    items[index].decision = nil
                default:
                    throw ConversionQueueStoreError.invalidDocument
                }
            }
            switch effectivePhase {
            case .completed:
                publishTitleQueueProjection()
                activeQueueItemID = nil
                releaseAdoption(itemID)
                if titleQueueStopRequested {
                    try await stopWaitingTitleQueueItems()
                    await stopUnstartedAdoptedItems()
                    publishTitleQueueProjection()
                    publishCompletedTitleQueueResults()
                    runDeferredActionsIfIdle()
                } else if currentTitleQueueItems.contains(where: {
                    adoptedItemIDs.contains($0.id) && $0.state == .waiting
                }) {
                    await startNextDurableTitleQueueItem()
                } else {
                    publishCompletedTitleQueueResults()
                    await pumpDurableQueue()
                }
            case .decisionRequired:
                publishTitleQueueProjection()
                if persistentQueueControlsActive || hasAdoptedWaitingDurableQueueItem(excluding: itemID) {
                    parkActiveDurableQueueItemForResume()
                    await pumpDurableQueue()
                }
            case .cancelled, .failed:
                if durableQueueStopRequested || titleQueueStopRequested {
                    try await stopWaitingTitleQueueItems()
                }
                publishTitleQueueProjection()
                activeQueueItemID = nil
                releaseAdoption(itemID)
                publishCompletedTitleQueueResults()
                await pumpDurableQueue()
            default:
                break
            }
        } catch {
            publishCompletedTitleQueueResults()
            failClosedTitleQueuePersistence(error)
        }
    }

    private func persistTitleQueueStop(itemID: UUID) async {
        guard activeQueueItemID == itemID else {
            return
        }
        do {
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }) else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[index].state = .stopping
                items[index].decision = nil
            }
            publishTitleQueueProjection()
        } catch {
            durableQueueRuntimeDiagnostic = "Queue stop state could not be saved: \(error.localizedDescription)"
        }
    }

    private func persistDurableSingleStop(itemID: UUID) async {
        guard activeQueueItemID == itemID else {
            return
        }
        do {
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }),
                      items[index].origin == .singleSource
                else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[index].state = .stopping
                items[index].decision = nil
            }
        } catch {
            durableQueueRuntimeDiagnostic = "Queue stop state could not be saved: \(error.localizedDescription)"
        }
    }

    private func persistCurrentDurableQueueStop(itemID: UUID) async {
        guard activeQueueItemID == itemID else {
            return
        }
        do {
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }) else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[index].state = .stopping
                items[index].decision = nil
            }
            publishTitleQueueProjection()
            publishSourceFolderQueueProjection()
        } catch {
            durableQueueRuntimeDiagnostic = "Queue stop state could not be saved: \(error.localizedDescription)"
        }
    }

    private func persistTitleQueueDecisionCancellation(itemID: UUID) async {
        guard activeQueueItemID == itemID else {
            return
        }
        do {
            var stoppedItemIDs: Set<UUID> = []
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }) else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[index].state = .stopped
                stoppedItemIDs.insert(items[index].id)
                items[index].decision = nil
                items[index].failure = nil
                if let attemptIndex = items[index].attempts.lastIndex(where: { $0.endedAt == nil }) {
                    items[index].attempts[attemptIndex].endedAt = diagnosticClock()
                }
                guard let titleQueueGroupID else {
                    return
                }
                for pendingIndex in items.indices where items[pendingIndex].groupID == titleQueueGroupID && items[pendingIndex].state == .waiting {
                    items[pendingIndex].state = .stopped
                    stoppedItemIDs.insert(items[pendingIndex].id)
                }
            }
            activeQueueItemID = nil
            for stoppedItemID in stoppedItemIDs {
                releaseAdoption(stoppedItemID)
            }
            publishTitleQueueProjection()
            await pumpDurableQueue()
        } catch {
            failClosedTitleQueuePersistence(error)
        }
    }

    private func persistClearedTitleQueueDecisionCancellation(itemID: UUID, groupID: UUID?) async {
        guard let groupID else {
            return
        }
        do {
            var stoppedItemIDs: Set<UUID> = []
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }) else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[index].state = .stopped
                stoppedItemIDs.insert(items[index].id)
                items[index].decision = nil
                items[index].failure = nil
                if let attemptIndex = items[index].attempts.lastIndex(where: { $0.endedAt == nil }) {
                    items[index].attempts[attemptIndex].endedAt = diagnosticClock()
                }
                for pendingIndex in items.indices where items[pendingIndex].groupID == groupID && items[pendingIndex].state == .waiting {
                    items[pendingIndex].state = .stopped
                    stoppedItemIDs.insert(items[pendingIndex].id)
                }
            }
            for stoppedItemID in stoppedItemIDs {
                releaseAdoption(stoppedItemID)
            }
        } catch {
            durableQueueRuntimeDiagnostic = "Queue decision cancellation could not be saved: \(error.localizedDescription)"
        }
    }

    private func persistClearedDurableSingleDecisionCancellation(itemID: UUID) async {
        do {
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }),
                      items[index].origin == .singleSource
                else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[index].state = .stopped
                items[index].decision = nil
                items[index].failure = nil
                if let attemptIndex = items[index].attempts.lastIndex(where: { $0.endedAt == nil }) {
                    items[index].attempts[attemptIndex].endedAt = diagnosticClock()
                }
            }
            releaseAdoption(itemID)
        } catch {
            durableQueueRuntimeDiagnostic = "Queue decision cancellation could not be saved: \(error.localizedDescription)"
        }
    }

    private func persistWaitingTitleQueueStop() async {
        do {
            try await stopWaitingTitleQueueItems()
            await stopUnstartedAdoptedItems()
            publishTitleQueueProjection()
            runDeferredActionsIfIdle()
        } catch {
            failClosedTitleQueuePersistence(error)
        }
    }

    private func persistDurableSingleDecisionCancellation(itemID: UUID) async {
        guard activeQueueItemID == itemID else {
            return
        }
        do {
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }),
                      items[index].origin == .singleSource,
                      items[index].state == .attention
                else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[index].state = .stopped
                items[index].decision = nil
                items[index].failure = nil
                if let attemptIndex = items[index].attempts.lastIndex(where: { $0.endedAt == nil }) {
                    items[index].attempts[attemptIndex].endedAt = diagnosticClock()
                }
            }
            activeQueueItemID = nil
            releaseAdoption(itemID)
            publishTitleQueueProjection()
            await pumpDurableQueue()
        } catch {
            failClosedTitleQueuePersistence(error)
        }
    }

    private func retryDurableSingleQueueItem(
        itemID: UUID,
        draft: ConversionDraft,
        choice: WorkerRecoveryChoice
    ) async {
        guard activeQueueItemID == itemID else {
            return
        }
        do {
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }),
                      items[index].origin == .singleSource,
                      items[index].state == .attention
                else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[index].intent = DurableQueueItemIntent(draft: draft)
                items[index].state = .processing
                items[index].decision = nil
                items[index].failure = nil
                items[index].attempts.append(DurableQueueAttempt(
                    startedAt: diagnosticClock(),
                    recoveryChoice: choice.rawValue
                ))
            }
            durableQueueStopRequested = false
            persistentQueueRunState = .running
            state.prepareQueuedConversion(sourceURL: draft.source.url, inspection: draft.sourceDetails)
            source = draft.source
            _ = startConversion(draft: draft, mode: .durableSingleConversion(itemID: itemID))
        } catch {
            failClosedTitleQueuePersistence(error)
        }
    }

    private func retryDurableTitleQueueItem(
        itemID: UUID,
        draft: ConversionDraft,
        choice: WorkerRecoveryChoice
    ) async {
        guard activeQueueItemID == itemID else {
            return
        }
        do {
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }),
                      items[index].state == .attention
                else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[index].intent = DurableQueueItemIntent(draft: draft)
                items[index].state = .processing
                items[index].decision = nil
                items[index].failure = nil
                items[index].attempts.append(DurableQueueAttempt(
                    startedAt: diagnosticClock(),
                    recoveryChoice: choice.rawValue
                ))
            }
            publishTitleQueueProjection()
            persistentQueueRunState = .running
            if titleQueueStopRequested {
                await stopDurableTitleQueueBeforeSpawn(itemID: itemID)
                return
            }
            guard startConversion(draft: draft, mode: .titleQueueConversion(itemID: itemID)) else {
                await persistTitleQueueTerminalState(itemID: itemID)
                return
            }
        } catch {
            failClosedTitleQueuePersistence(error)
        }
    }

    private func stopWaitingTitleQueueItems() async throws {
        guard let titleQueueGroupID else {
            return
        }
        var stoppedItemIDs: Set<UUID> = []
        try await durableQueueStore.mutateItems { items in
            for index in items.indices where items[index].groupID == titleQueueGroupID && items[index].state == .waiting {
                items[index].state = .stopped
                stoppedItemIDs.insert(items[index].id)
            }
        }
        for itemID in stoppedItemIDs {
            releaseAdoption(itemID)
        }
    }

    private func stopDurableTitleQueueBeforeSpawn(itemID: UUID) async {
        do {
            var stoppedItemIDs: Set<UUID> = []
            try await durableQueueStore.mutateItems { items in
                guard let activeIndex = items.firstIndex(where: { $0.id == itemID }) else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[activeIndex].state = .stopped
                stoppedItemIDs.insert(items[activeIndex].id)
                if let attemptIndex = items[activeIndex].attempts.lastIndex(where: { $0.endedAt == nil }) {
                    items[activeIndex].attempts[attemptIndex].endedAt = diagnosticClock()
                }
                guard let titleQueueGroupID else {
                    return
                }
                for index in items.indices where items[index].groupID == titleQueueGroupID && items[index].state == .waiting {
                    items[index].state = .stopped
                    stoppedItemIDs.insert(items[index].id)
                }
            }
            activeQueueItemID = nil
            for stoppedItemID in stoppedItemIDs {
                releaseAdoption(stoppedItemID)
            }
            await stopUnstartedAdoptedItems()
            publishTitleQueueProjection()
            runDeferredActionsIfIdle()
        } catch {
            failClosedTitleQueuePersistence(error)
        }
    }

    private func conversionDraft(
        for item: DurableConversionQueueItem,
        removeOriginalAfterSuccess: Bool
    ) throws -> ConversionDraft {
        guard let sourceKind = ConversionSourceKind(rawValue: item.intent.source.kind) else {
            throw ConversionQueueStoreError.invalidDocument
        }
        var options = item.intent.options
        options.job.removeOriginalAfterSuccess = removeOriginalAfterSuccess
        return ConversionDraft(
            source: ConversionSource(
                kind: sourceKind,
                url: URL(fileURLWithPath: item.intent.source.path),
                displayName: item.intent.source.displayName,
                workerSourcePath: item.intent.source.workerSourcePath,
                mediaIdentifier: item.intent.source.mediaIdentifier
            ),
            sourceDetails: item.inspection,
            profile: EncodingProfile(
                id: item.intent.profile.id,
                name: item.intent.profile.name,
                options: options.encoding,
                kind: item.intent.profile.kind,
                systemImage: "slider.horizontal.3"
            ),
            destinationURL: URL(fileURLWithPath: item.intent.destinationPath),
            options: options,
            selectedTitle: item.intent.selectedTitle
        )
    }

    private func shouldRemoveOriginalAfterSuccess(for item: DurableConversionQueueItem) -> Bool {
        guard item.origin == .multiTitle, let groupID = item.groupID else {
            return item.intent.options.job.removeOriginalAfterSuccess
        }
        let groupItems = durableQueueStore.items.filter {
            $0.groupID == groupID
                && $0.origin == .multiTitle
                && $0.intent.source.path == item.intent.source.path
                && $0.intent.source.workerSourcePath == item.intent.source.workerSourcePath
                && $0.intent.source.mediaIdentifier == item.intent.source.mediaIdentifier
        }
        let removalRequested = groupItems.contains {
            $0.intent.options.job.removeOriginalAfterSuccess
        }
        let hasUnfinishedSibling = groupItems.contains {
            $0.id != item.id && $0.state != .completed
        }
        return removalRequested && !hasUnfinishedSibling
    }

    private func durableQueueItem(id: UUID) throws -> DurableConversionQueueItem {
        guard let item = durableQueueStore.items.first(where: { $0.id == id }) else {
            throw ConversionQueueStoreError.invalidDocument
        }
        return item
    }

    private func durableSource(for item: DurableConversionQueueItem) throws -> ConversionSource {
        guard let kind = ConversionSourceKind(rawValue: item.intent.source.kind) else {
            throw ConversionQueueStoreError.invalidDocument
        }
        return ConversionSource(
            kind: kind,
            url: URL(fileURLWithPath: item.intent.source.path),
            displayName: item.intent.source.displayName,
            workerSourcePath: item.intent.source.workerSourcePath,
            mediaIdentifier: item.intent.source.mediaIdentifier
        )
    }

    private func persistSourceUnavailable(itemID: UUID) async {
        do {
            var origin: DurableQueueItemOrigin?
            var groupID: UUID?
            var parkedItemIDs: Set<UUID> = []
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }) else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                origin = items[index].origin
                groupID = items[index].groupID
                let unavailableSourcePath = items[index].intent.source.path
                let failure = DurableQueueFailure(
                    code: "source_unavailable",
                    message: "The queued source is no longer available.",
                    details: unavailableSourcePath,
                    retryable: true
                )
                items[index].state = .failed
                items[index].decision = nil
                items[index].failure = failure
                if let attemptIndex = items[index].attempts.lastIndex(where: { $0.endedAt == nil }) {
                    items[index].attempts[attemptIndex].endedAt = diagnosticClock()
                }
                for peerIndex in items.indices where peerIndex != index
                    && items[peerIndex].state == .waiting
                    && items[peerIndex].intent.source.path == unavailableSourcePath
                {
                    items[peerIndex].state = .stopped
                    items[peerIndex].decision = nil
                    items[peerIndex].failure = nil
                    parkedItemIDs.insert(items[peerIndex].id)
                }
            }
            activeQueueItemID = nil
            releaseAdoption(itemID)
            for parkedItemID in parkedItemIDs {
                releaseAdoption(parkedItemID)
            }
            state.failTransport(message: "The queued source is no longer available.", retryable: true)
            switch origin {
            case .multiTitle:
                titleQueueGroupID = groupID
                publishTitleQueueProjection()
                publishCompletedTitleQueueResults()
            case .sourceFolder:
                sourceFolderQueueGroupID = groupID
                publishSourceFolderQueueProjection()
            case .singleSource, nil:
                break
            }
            await pumpDurableQueue()
        } catch {
            failClosedTitleQueuePersistence(error)
        }
    }

    nonisolated private static func defaultSourceAvailability(_ source: ConversionSource) -> Bool {
        guard FileManager.default.fileExists(atPath: source.url.path) else {
            return false
        }
        return source.kind != .physicalDisc || DiscSourceDetector.isCurrentPhysicalDisc(source)
    }

    private func releaseAdoption(_ itemID: UUID) {
        adoptedItemIDs.remove(itemID)
        durableSingleJobIDs.removeValue(forKey: itemID)
        sourceFolderRecoveryChoices.removeValue(forKey: itemID)
    }

    private func parkActiveDurableQueueItemForResume() {
        guard let item = activeDurableQueueItem,
              item.state == .attention
        else {
            return
        }
        activeQueueItemID = nil
        releaseAdoption(item.id)
        if state.phase == .decisionRequired {
            state.clear()
        }
    }

    private func hasAdoptedWaitingDurableQueueItem(excluding itemID: UUID) -> Bool {
        guard let parkedItem = durableQueueStore.items.first(where: { $0.id == itemID }) else {
            return false
        }
        return durableQueueStore.items.contains { item in
            item.id != itemID
                && adoptedItemIDs.contains(item.id)
                && item.state == .waiting
                && item.intent.source.path != parkedItem.intent.source.path
        }
    }

    private func pauseDurableQueueNow() async {
        guard !hasActiveWorker, activeQueueItemID == nil else {
            return
        }
        let pendingItemIDs = durableQueueStore.items.compactMap { item in
            adoptedItemIDs.contains(item.id) && item.state == .waiting ? item.id : nil
        }
        for itemID in pendingItemIDs {
            releaseAdoption(itemID)
        }
        persistentQueueRunState = .paused
        publishTitleQueueProjection()
        publishSourceFolderQueueProjection()
        finishSourceFolderQueueIfNeeded()
    }

    private func stopUnstartedAdoptedItems() async {
        let itemIDs = adoptedItemIDs
        guard !itemIDs.isEmpty else {
            durableQueueStopRequested = false
            return
        }
        do {
            var stoppedItemIDs: Set<UUID> = []
            try await durableQueueStore.mutateItems { items in
                for index in items.indices
                    where itemIDs.contains(items[index].id) && items[index].state == .waiting
                {
                    items[index].state = .notStarted
                    stoppedItemIDs.insert(items[index].id)
                }
            }
            for itemID in stoppedItemIDs {
                releaseAdoption(itemID)
            }
            durableQueueStopRequested = false
            publishTitleQueueProjection()
            publishSourceFolderQueueProjection()
        } catch {
            failClosedTitleQueuePersistence(error)
        }
    }

    private func conversionDraft(
        for item: DurableConversionQueueItem,
        preserveStoredSourceRemoval: Bool
    ) throws -> ConversionDraft {
        try conversionDraft(
            for: item,
            removeOriginalAfterSuccess: preserveStoredSourceRemoval
                && item.intent.options.job.removeOriginalAfterSuccess
        )
    }

    private func publishTitleQueueProjection() {
        queueItems = currentTitleQueueItems.compactMap { item in
            guard let draft = try? conversionDraft(for: item, removeOriginalAfterSuccess: false) else {
                return nil
            }
            return ConversionQueueItem(id: item.id, draft: draft, status: queueStatus(for: item))
        }
    }

    func publishPersistentQueueProjection(items: [DurableConversionQueueItem]) {
        let projectedItems: [PersistentQueueItem]
        do {
            projectedItems = try items.map(PersistentQueueItem.init(item:))
        } catch let error as PersistentQueueProjectionError {
            persistentQueueItems = []
            selectedPersistentQueueItemID = nil
            persistentQueueProjectionError = error
            return
        } catch {
            persistentQueueItems = []
            selectedPersistentQueueItemID = nil
            persistentQueueProjectionError = .unexpected
            return
        }
        persistentQueueItems = projectedItems
        persistentQueueProjectionError = nil
        if let selectedPersistentQueueItemID,
           persistentQueueItems.contains(where: { $0.id == selectedPersistentQueueItemID })
        {
            return
        }
        selectedPersistentQueueItemID = persistentQueueItems.first?.id
    }

    private func publishCompletedTitleQueueResults() {
        let results = queueItems.compactMap { item -> ConversionResult? in
            guard case let .completed(result) = item.status else {
                return nil
            }
            return result
        }
        completedBatchResults = results.isEmpty ? nil : results
    }

    private func queueStatus(for item: DurableConversionQueueItem) -> ConversionQueueItemStatus {
        switch item.state {
        case .waiting, .interrupted:
            .waiting
        case .needsChoice:
            .attention("Needs a route-quality choice")
        case .inspecting, .processing, .stopping:
            .processing
        case .attention:
            .attention(item.decision?.prompt ?? "Choose how to continue.")
        case .completed:
            .completed(ConversionResult(
                outputPath: item.result?.outputPath ?? "",
                durationSeconds: item.result?.durationSeconds,
                sizeBytes: item.result?.sizeBytes,
                titleID: item.result?.titleID
            ))
        case .failed:
            .failed(item.failure?.message ?? "Conversion failed.")
        case .stopped, .notStarted:
            .cancelled
        }
    }

    private func failClosedTitleQueuePersistence(_ error: Error) {
        durableQueueRuntimeDiagnostic = "Queue changes are unavailable: \(error.localizedDescription)"
        persistentQueueRunState = .idle
        persistentQueueControlsActive = false
        offPeakRunWindowEnd = nil
        activeQueueItemID = nil
        titleQueueGroupID = nil
        adoptedItemIDs.removeAll()
        durableSingleJobIDs.removeAll()
        sourceFolderRecoveryChoices.removeAll()
        durableQueueStopRequested = false
        state.failTransport(message: durableQueueRuntimeDiagnostic ?? "Queue changes are unavailable.", retryable: false)
        runDeferredActionsIfIdle()
    }

    private func resetQueue() {
        queueItems.removeAll()
        persistentQueueRunState = .idle
        persistentQueueControlsActive = false
        offPeakRunWindowEnd = nil
        activeQueueItemID = nil
        titleQueueGroupID = nil
        titleQueueStopRequested = false
        sourceFolderQueueGroupID = nil
        sourceFolderStopRequested = false
        sourceFolderQueueCompletionPending = false
        sourceFolderRecoveryChoices.removeAll()
        adoptedItemIDs.removeAll()
        durableSingleJobIDs.removeAll()
        durableQueueStopRequested = false
        durableQueueRuntimeDiagnostic = nil
        completedBatchResults = nil
    }

    private func resetDiagnosticSession() {
        diagnosticRecorder.reset()
        batchItemDiagnosticJobIDs.removeAll(keepingCapacity: true)
        liveObservabilityStatus = .empty
    }

    private func recordDiagnosticWorkflow(
        name: String,
        mode: ActiveRunMode? = nil,
        message: String? = nil,
        details: String? = nil,
        jobID: UUID? = nil
    ) {
        diagnosticRecorder.recordWorkflow(
            name: name,
            lifecycle: state,
            activeMode: (mode ?? activeRunMode)?.diagnosticName,
            recordedAt: diagnosticClock(),
            message: message,
            details: details,
            jobID: jobID
        )
    }

    private func trackDiagnosticJob(_ jobID: UUID, mode: ActiveRunMode) {
        switch mode {
        case let .batchInspection(itemID), let .batchConversion(itemID):
            batchItemDiagnosticJobIDs[itemID] = jobID
        case .singleInspection, .singleConversion, .titleQueueInspection, .titleQueueConversion, .durableSingleInspection, .durableSingleConversion:
            break
        }
    }

    private func scheduleDiagnosticStorageSample(recordedAt: Date, force: Bool) {
        guard let request = diagnosticRecorder.makeStorageSampleRequest(
            recordedAt: recordedAt,
            force: force
        ) else {
            return
        }
        let probe = diagnosticStorageProbe
        Task.detached(priority: .utility) { [weak self] in
            let samples = request.targets.map { target in
                RawDiagnosticStorageSample(
                    probe: probe.probe(
                        role: target.role,
                        url: target.url,
                        capturedAt: request.capturedAt
                    )
                )
            }
            await self?.recordDiagnosticStorageSamples(samples, for: request.jobID)
        }
    }

    private func recordDiagnosticStorageSamples(
        _ samples: [RawDiagnosticStorageSample],
        for jobID: UUID
    ) {
        diagnosticRecorder.recordStorageSamples(samples, for: jobID)
    }

    private var diagnosticBatchSummary: DiagnosticBatchSummary? {
        if let batchQueue {
            var counts: [String: Int] = [:]
            for item in batchQueue.items {
                counts[item.status.rawValue, default: 0] += 1
            }
            return DiagnosticBatchSummary(
                kind: "source_folder",
                totalItems: batchQueue.items.count,
                activeItems: batchQueue.activeItemID == nil ? 0 : 1,
                statusCounts: counts
            )
        }
        guard !queueItems.isEmpty else {
            return nil
        }
        var counts: [String: Int] = [:]
        for item in queueItems {
            let status: String
            switch item.status {
            case .waiting:
                status = "waiting"
            case .processing:
                status = "processing"
            case .attention:
                status = "attention"
            case .completed:
                status = "completed"
            case .failed:
                status = "failed"
            case .cancelled:
                status = "cancelled"
            }
            counts[status, default: 0] += 1
        }
        return DiagnosticBatchSummary(
            kind: "title_queue",
            totalItems: queueItems.count,
            activeItems: activeQueueItemID == nil ? 0 : 1,
            statusCounts: counts
        )
    }
}

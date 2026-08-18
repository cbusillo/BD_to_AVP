import Combine
import Foundation
import SwiftUI

private enum ActiveRunMode: Equatable {
    case singleInspection
    case singleConversion
    case titleQueueConversion(itemID: UUID)
    case batchInspection(itemID: UUID)
    case batchConversion(itemID: UUID)

    var diagnosticName: String {
        switch self {
        case .singleInspection:
            "single_inspection"
        case .singleConversion:
            "single_conversion"
        case .titleQueueConversion:
            "title_queue_conversion"
        case .batchInspection:
            "batch_inspection"
        case .batchConversion:
            "batch_conversion"
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

    private let clientFactory: ClientFactory
    private let diagnosticClock: () -> Date
    private let diagnosticStorageProbe: any DiagnosticStorageProbing
    private let diagnosticBundleBuilder: DiagnosticBundleBuilder
    private let observabilityEventStore: any ObservabilityEventPersisting
    private let durableQueueStore: ConversionQueueStore
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
    private var pendingTitleQueueTransition: Task<Void, Never>?
    private var pendingTitleQueueTransitionID: UUID?
    private var sourceFolderQueueGroupID: UUID?
    private var sourceFolderStopRequested = false
    private var pendingSourceFolderQueueTransition: Task<Void, Never>?
    private var pendingSourceFolderQueueTransitionID: UUID?
    private var sourceFolderQueueCompletionPending = false
    private var sourceFolderRecoveryChoices: [UUID: String] = [:]
    private var batchItemDiagnosticJobIDs: [UUID: UUID] = [:]
    private var durableQueueSubscription: AnyCancellable?

    init(
        clientFactory: @escaping ClientFactory = {
            WorkerProcessClient(configuration: try WorkerLaunchConfiguration.automatic())
        },
        diagnosticClock: @escaping () -> Date = Date.init,
        diagnosticStorageProbe: any DiagnosticStorageProbing = FileSystemDiagnosticStorageProbe(),
        diagnosticBundleBuilder: DiagnosticBundleBuilder? = nil,
        observabilityEventStore: any ObservabilityEventPersisting = NullObservabilityEventStore.shared,
        durableQueueStore: ConversionQueueStore? = nil
    ) {
        self.clientFactory = clientFactory
        self.diagnosticClock = diagnosticClock
        self.diagnosticStorageProbe = diagnosticStorageProbe
        self.diagnosticBundleBuilder = diagnosticBundleBuilder
            ?? DiagnosticBundleBuilder(storageProbe: diagnosticStorageProbe)
        self.observabilityEventStore = observabilityEventStore
        self.durableQueueStore = durableQueueStore ?? ConversionQueueStore.inMemory()
        durableQueueSubscription = self.durableQueueStore.$document.sink { [weak self] document in
            self?.publishPersistentQueueProjection(items: document.items)
        }
    }

    var isRunning: Bool {
        state.phase.isRunning
    }

    var hasActiveWorker: Bool {
        runTask != nil
    }

    var hasActiveWork: Bool {
        hasActiveWorker
            || pendingTitleQueueTransition != nil
            || pendingSourceFolderQueueTransition != nil
            || pendingBatchContinuation != nil
            || isBatchRunning
            || hasQueuedWork
            || state.phase == .decisionRequired
    }

    var hasStoppableWork: Bool {
        hasActiveWorker
            || pendingTitleQueueTransition != nil
            || pendingSourceFolderQueueTransition != nil
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
        activeQueueItemID != nil || currentTitleQueueItems.contains { item in
            item.state == .waiting || item.state == .processing || item.state == .stopping
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

    func movePersistentQueueItemNext(_ itemID: UUID) async throws {
        try await durableQueueStore.moveWaitingItemNext(itemID)
    }

    func removePersistentQueueItems(_ itemIDs: Set<UUID>) async throws -> PersistentQueueRemovalToken {
        try await durableQueueStore.removeWaitingItems(itemIDs)
    }

    func restorePersistentQueueItems(_ token: PersistentQueueRemovalToken) async throws {
        try await durableQueueStore.restoreRemovedItems(token)
    }

    func updatePersistentQueueItem(_ itemID: UUID, draft: ConversionDraft) async throws {
        try await durableQueueStore.updateWaitingItemIntent(
            itemID,
            intent: DurableQueueItemIntent(draft: draft)
        )
    }

    func clearCompletedPersistentQueueItems() async throws -> PersistentQueueRemovalToken {
        try await durableQueueStore.clearCompletedItems()
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
        guard !hasActiveWork, let source, state.sourceURL == source.url else {
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
        resetQueue()
        _ = startConversion(draft: draft, jobID: jobID, mode: .singleConversion)
    }

    func startConversionQueue(drafts: [ConversionDraft]) {
        guard !hasActiveWork,
              !drafts.isEmpty,
              drafts.allSatisfy({ draft in
                  if case .failure = RouteQualityEngine.validate(draft.options) { return false }
                  return true
              }),
              let firstDraft = drafts.first
        else {
            return
        }
        if drafts.count == 1 {
            startConversion(draft: firstDraft)
            return
        }
        let groupID = UUID()
        let removeOriginalAfterFinalSuccess = drafts.contains { $0.options.job.removeOriginalAfterSuccess }
        let normalizedDrafts = drafts.enumerated().map { offset, draft in
            var options = draft.options
            options.job.removeOriginalAfterSuccess = removeOriginalAfterFinalSuccess && offset == drafts.count - 1
            return ConversionDraft(
                source: draft.source,
                sourceDetails: draft.sourceDetails,
                profile: draft.profile,
                destinationURL: draft.destinationURL,
                options: options,
                selectedTitle: draft.selectedTitle
            )
        }
        activeQueueItemID = nil
        titleQueueGroupID = groupID
        titleQueueStopRequested = false
        durableQueueRuntimeDiagnostic = nil
        completedBatchResults = nil
        enqueueTitleQueueTransition { [weak self] in
            guard let self else {
                return
            }
            do {
                try await self.durableQueueStore.mutateItems { items in
                    let startingOrdinal = items.count
                    items.append(contentsOf: normalizedDrafts.enumerated().map { offset, draft in
                        DurableConversionQueueItem(
                            ordinal: startingOrdinal + offset,
                            groupID: groupID,
                            origin: .multiTitle,
                            intent: DurableQueueItemIntent(draft: draft),
                            inspection: draft.sourceDetails
                        )
                    })
                }
                self.publishTitleQueueProjection()
                if self.titleQueueStopRequested {
                    try await self.stopWaitingTitleQueueItems()
                    self.publishTitleQueueProjection()
                    return
                }
                await self.startNextDurableTitleQueueItem()
            } catch {
                self.failClosedTitleQueuePersistence(error)
            }
        }
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
              FileManager.default.fileExists(atPath: draft.source.url.path)
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
        let job = WorkerJobSpec(draft: draft, jobID: jobID)
        do {
            try state.begin(jobID: job.jobID, operationKind: .conversion)
            liveObservabilityStatus = .empty
            switch mode {
            case .singleConversion, .titleQueueConversion:
                lastConversionDraft = draft
            case .singleInspection, .batchInspection, .batchConversion:
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
        guard state.sourceURL == draft.source.url, state.result != nil else {
            return false
        }
        switch mode {
        case let .batchConversion(itemID):
            return batchQueue?.activeItemID == itemID
                && batchQueue?.activeItem?.source == draft.source
        case .singleConversion, .titleQueueConversion:
            return source == draft.source
        case .singleInspection, .batchInspection:
            return false
        }
    }

    func startBatchConversion(
        profile: EncodingProfile,
        destinationURL: URL,
        options: ConversionOptions
    ) {
        guard !hasActiveWork,
              !isBatchRunning,
              let source,
              source.kind == .sourceFolder,
              var queue = batchQueue,
              queue.folderSource == source,
              !queue.items.isEmpty
        else {
            return
        }

        guard !durableQueueStore.writesBlocked else {
            failClosedSourceFolderQueuePersistence(ConversionQueueStoreError.recoveryRequired)
            return
        }

        queue.prepareForRun(
            profile: profile,
            destinationURL: destinationURL,
            options: options
        )
        batchQueue = queue
        sourceFolderQueueGroupID = nil
        sourceFolderStopRequested = false
        sourceFolderQueueCompletionPending = false
        sourceFolderRecoveryChoices.removeAll()
        recordDiagnosticWorkflow(name: "batch.started", message: "source_folder")
        enqueueSourceFolderQueueTransition { [weak self] in
            await self?.admitAndStartSourceFolderQueue()
        }
    }

    func stopActiveWorker() {
        guard hasStoppableWork else {
            return
        }
        if titleQueueGroupID != nil {
            titleQueueStopRequested = true
        }
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
            if hasActiveWorker, case .batchInspection = activeRunMode {
                state.requestStop()
                client?.cancel()
            } else if hasActiveWorker, case .batchConversion = activeRunMode {
                state.requestStop()
                client?.cancel()
            }
            enqueueSourceFolderQueueTransition { [weak self] in
                await self?.persistSourceFolderStopRequest()
            }
            return
        }
        if let activeQueueItemID {
            state.requestStop()
            recordDiagnosticWorkflow(name: "cancel.requested", mode: activeRunMode, jobID: state.jobID)
            client?.cancel()
            enqueueTitleQueueTransition { [weak self] in
                await self?.persistTitleQueueStop(itemID: activeQueueItemID)
            }
            return
        }
        if titleQueueGroupID != nil {
            enqueueTitleQueueTransition { [weak self] in
                await self?.persistWaitingTitleQueueStop()
            }
            return
        }
        if hasActiveWorker {
            state.requestStop()
            recordDiagnosticWorkflow(
                name: "cancel.requested",
                mode: activeRunMode,
                jobID: state.jobID
            )
            client?.cancel()
        }
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
              pendingSourceFolderQueueTransition == nil,
              !isBatchRunning,
              let decision = state.recoveryDecision,
              decision.supportedChoices.contains(choice)
        else {
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
            if let activeQueueItemID {
                enqueueTitleQueueTransition { [weak self] in
                    await self?.persistTitleQueueDecisionCancellation(itemID: activeQueueItemID)
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
            if let activeQueueItemID {
                enqueueTitleQueueTransition { [weak self] in
                    await self?.persistTitleQueueTerminalState(itemID: activeQueueItemID)
                }
            }
            return false
        }
        guard FileManager.default.fileExists(atPath: retryDraft.source.url.path) else {
            state.failTransport(
                message: "Conversion requires an inserted Blu-ray disc or existing Blu-ray folder, ISO, MKV, MTS, or M2TS source.",
                retryable: false
            )
            if let activeQueueItemID {
                let snapshot = TitleQueueTerminalSnapshot(
                    phase: .failed,
                    decision: nil,
                    failure: DurableQueueFailure(
                        code: state.failureCode,
                        message: state.failureMessage ?? "Conversion could not be restarted.",
                        details: state.failureDetails,
                        retryable: state.failureRetryable
                    ),
                    result: nil
                )
                enqueueTitleQueueTransition { [weak self] in
                    await self?.persistTitleQueueTerminalState(itemID: activeQueueItemID, snapshot: snapshot)
                }
            }
            return false
        }
        state.prepareForRetry()
        if let activeQueueItemID {
            enqueueTitleQueueTransition { [weak self] in
                await self?.retryDurableTitleQueueItem(itemID: activeQueueItemID, draft: retryDraft, choice: choice)
            }
        } else {
            _ = startConversion(draft: retryDraft, mode: .singleConversion)
        }
        return activeQueueItemID != nil || state.phase.isRunning
    }

    func clearSource() {
        if state.phase == .decisionRequired, let activeQueueItemID {
            state.cancelRecoveryDecision()
            let groupID = titleQueueGroupID
            self.activeQueueItemID = nil
            enqueueTitleQueueTransition { [weak self] in
                await self?.persistClearedTitleQueueDecisionCancellation(
                    itemID: activeQueueItemID,
                    groupID: groupID
                )
                self?.clearSourceNow()
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
        if let pendingTitleQueueTransition {
            await pendingTitleQueueTransition.value
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
            if let transition = pendingSourceFolderQueueTransition {
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
        let previousTransition = pendingSourceFolderQueueTransition
        let transitionID = UUID()
        pendingSourceFolderQueueTransitionID = transitionID
        pendingSourceFolderQueueTransition = Task { @MainActor [weak self] in
            await previousTransition?.value
            guard !Task.isCancelled else {
                return
            }
            await transition()
            if self?.pendingSourceFolderQueueTransitionID == transitionID {
                self?.pendingSourceFolderQueueTransition = nil
                self?.pendingSourceFolderQueueTransitionID = nil
                self?.runDeferredActionsIfIdle()
            }
        }
    }

    private func admitAndStartSourceFolderQueue() async {
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
                        intent: DurableQueueItemIntent(draft: draft),
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
            }
            sourceFolderQueueGroupID = groupID
            publishSourceFolderQueueProjection()
            await pumpSourceFolderQueue()
        } catch {
            failClosedSourceFolderQueuePersistence(error)
        }
    }

    private func pumpSourceFolderQueue() async {
        guard !hasActiveWorker,
              let groupID = sourceFolderQueueGroupID
        else {
            return
        }
        if sourceFolderStopRequested {
            await persistSourceFolderStopRequest()
            finishSourceFolderQueueIfNeeded()
            return
        }
        guard let item = currentSourceFolderQueueItems.first(where: { $0.state == .waiting }) else {
            finishSourceFolderQueueIfNeeded()
            return
        }
        do {
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == item.id }),
                      items[index].groupID == groupID,
                      items[index].state == .waiting
                else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[index].state = .inspecting
                items[index].attempts.append(DurableQueueAttempt(
                    startedAt: diagnosticClock(),
                    recoveryChoice: sourceFolderRecoveryChoices.removeValue(forKey: item.id)
                ))
            }
            publishSourceFolderQueueProjection()
            if sourceFolderStopRequested {
                await stopSourceFolderQueueBeforeSpawn(itemID: item.id)
                return
            }
            let source = try sourceFolderSource(for: item)
            state.clear()
            state.selectSource(source.url)
            startInspection(source: source, mode: .batchInspection(itemID: item.id))
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
        var draft = try conversionDraft(for: item, preserveStoredSourceRemoval: true)
        draft = draft.withSourceDetails(inspection)
        if draft.source.kind.isDiscWorkflow {
            guard let mainTitle = inspection.mainTitle else {
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
            var inspectedOptions = draft.options
            if inspection.titles.count > 1 {
                inspectedOptions.job.removeOriginalAfterSuccess = false
            }
            draft = ConversionDraft(
                source: draft.source,
                sourceDetails: inspection,
                profile: draft.profile,
                destinationURL: draft.destinationURL,
                options: inspectedOptions,
                selectedTitle: mainTitle
            )
        }
        try await durableQueueStore.mutateItems { items in
            guard let index = items.firstIndex(where: { $0.id == itemID }),
                  items[index].groupID == groupID,
                  items[index].state == .inspecting
            else {
                throw ConversionQueueStoreError.invalidDocument
            }
            let outputKey = draft.proposedOutputURL.standardizedFileURL.path.lowercased()
            let conflictingItem = items.first { candidate in
                candidate.id != itemID
                    && candidate.groupID == groupID
                    && candidate.origin == .sourceFolder
                    && candidate.inspection != nil
                    && candidate.state != .notStarted
                    && candidate.state != .stopped
                    && ((try? conversionDraft(for: candidate, preserveStoredSourceRemoval: true)
                        .proposedOutputURL.standardizedFileURL.path.lowercased()) == outputKey)
            }
            if let conflictingItem {
                items[index].state = .failed
                items[index].inspection = inspection
                items[index].failure = DurableQueueFailure(
                    code: "output_collision",
                    message: "Another queued source resolves to the same output file.",
                    details: "\(draft.proposedOutputURL.path) is already reserved by \(conflictingItem.intent.source.displayName).",
                    retryable: false
                )
            } else {
                if let inspectionAttempt = items[index].attempts.lastIndex(where: { $0.endedAt == nil }) {
                    items[index].attempts[inspectionAttempt].endedAt = diagnosticClock()
                }
                items[index].inspection = inspection
                items[index].intent = DurableQueueItemIntent(draft: draft)
                items[index].state = .processing
                items[index].decision = nil
                items[index].failure = nil
                items[index].attempts.append(DurableQueueAttempt(startedAt: diagnosticClock()))
            }
            if let attemptIndex = items[index].attempts.lastIndex(where: { $0.endedAt == nil }),
               items[index].state != .processing
            {
                items[index].attempts[attemptIndex].endedAt = diagnosticClock()
            }
        }
        publishSourceFolderQueueProjection()
        try await startPersistedSourceFolderConversion(itemID: itemID)
    }

    private func startPersistedSourceFolderConversion(itemID: UUID) async throws {
        let updated = sourceFolderQueueItem(id: itemID)
        guard let updated else {
            await pumpSourceFolderQueue()
            return
        }
        guard updated.state == .processing else {
            await pumpSourceFolderQueue()
            return
        }
        if sourceFolderStopRequested {
            await stopSourceFolderQueueBeforeSpawn(itemID: itemID)
            return
        }
        let conversionDraft = try conversionDraft(for: updated, preserveStoredSourceRemoval: true)
        guard !hasActiveWorker else {
            throw ConversionQueueStoreError.invalidDocument
        }
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
                items[index].state = .failed
                items[index].failure = snapshot.failure
                items[index].decision = DurableQueueDecision(decision: decision)
            } else {
                items[index].state = .failed
                items[index].failure = snapshot.failure
                items[index].decision = nil
            }
        }
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
            try await durableQueueStore.mutateItems { items in
                for index in items.indices where items[index].groupID == groupID && items[index].origin == .sourceFolder {
                    switch items[index].state {
                    case .waiting:
                        items[index].state = .notStarted
                    case .inspecting, .processing:
                        items[index].state = .stopping
                    default:
                        break
                    }
                }
            }
            publishSourceFolderQueueProjection()
        } catch {
            failClosedSourceFolderQueuePersistence(error)
        }
    }

    private func stopSourceFolderQueueBeforeSpawn(itemID: UUID) async {
        guard let groupID = sourceFolderQueueGroupID else {
            return
        }
        do {
            try await durableQueueStore.mutateItems { items in
                guard let activeIndex = items.firstIndex(where: { $0.id == itemID }),
                      items[activeIndex].groupID == groupID,
                      items[activeIndex].origin == .sourceFolder
                else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[activeIndex].state = .stopped
                items[activeIndex].decision = nil
                items[activeIndex].failure = nil
                if let attemptIndex = items[activeIndex].attempts.lastIndex(where: { $0.endedAt == nil }) {
                    items[activeIndex].attempts[attemptIndex].endedAt = diagnosticClock()
                }
                for index in items.indices where items[index].groupID == groupID && items[index].state == .waiting {
                    items[index].state = .notStarted
                }
            }
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
                    options: draft.options
                )
                items[index].intent = DurableQueueItemIntent(draft: draft)
                items[index].state = .waiting
                items[index].decision = nil
                items[index].failure = nil
                items[index].result = nil
            }
            if let recoveryChoice {
                sourceFolderRecoveryChoices[itemID] = recoveryChoice.rawValue
            }
            publishSourceFolderQueueProjection()
            await pumpSourceFolderQueue()
        } catch {
            failClosedSourceFolderQueuePersistence(error)
        }
    }

    private func finishSourceFolderQueueIfNeeded() {
        guard !sourceFolderQueueCompletionPending,
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
            workerSourcePath: item.intent.source.workerSourcePath
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
        sourceFolderStopRequested = false
        sourceFolderQueueCompletionPending = false
        state.failTransport(message: durableQueueRuntimeDiagnostic ?? "Queue changes are unavailable.", retryable: false)
        runDeferredActionsIfIdle()
    }

    private var currentTitleQueueItems: [DurableConversionQueueItem] {
        guard let titleQueueGroupID else {
            return []
        }
        return durableQueueStore.items
            .filter { $0.groupID == titleQueueGroupID && $0.origin == .multiTitle }
            .sorted { $0.ordinal < $1.ordinal }
    }

    private func enqueueTitleQueueTransition(_ transition: @escaping @MainActor () async -> Void) {
        let previousTransition = pendingTitleQueueTransition
        let transitionID = UUID()
        pendingTitleQueueTransitionID = transitionID
        pendingTitleQueueTransition = Task { @MainActor [weak self] in
            await previousTransition?.value
            guard !Task.isCancelled else {
                return
            }
            await transition()
            if self?.pendingTitleQueueTransitionID == transitionID {
                self?.pendingTitleQueueTransition = nil
                self?.pendingTitleQueueTransitionID = nil
                self?.runDeferredActionsIfIdle()
            }
        }
    }

    private func startNextDurableTitleQueueItem() async {
        guard !hasActiveWorker,
              activeQueueItemID == nil,
              let item = currentTitleQueueItems.first(where: { $0.state == .waiting })
        else {
            publishTitleQueueProjection()
            runDeferredActionsIfIdle()
            return
        }

        let isFinalWaitingItem = currentTitleQueueItems.filter { $0.state == .waiting }.count == 1
        let draft: ConversionDraft
        do {
            draft = try conversionDraft(for: item, removeOriginalAfterSuccess: isFinalWaitingItem)
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == item.id }),
                      items[index].state == .waiting
                else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[index].state = .processing
                items[index].attempts.append(DurableQueueAttempt(startedAt: diagnosticClock()))
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
        if !startConversion(draft: draft, mode: .titleQueueConversion(itemID: item.id)) {
            await persistTitleQueueTerminalState(itemID: item.id)
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
                if titleQueueStopRequested {
                    try await stopWaitingTitleQueueItems()
                    publishTitleQueueProjection()
                    publishCompletedTitleQueueResults()
                    runDeferredActionsIfIdle()
                } else if currentTitleQueueItems.contains(where: { $0.state == .waiting }) {
                    await startNextDurableTitleQueueItem()
                } else {
                    publishCompletedTitleQueueResults()
                    runDeferredActionsIfIdle()
                }
            case .decisionRequired:
                publishTitleQueueProjection()
            case .cancelled, .failed:
                try await stopWaitingTitleQueueItems()
                publishTitleQueueProjection()
                activeQueueItemID = nil
                publishCompletedTitleQueueResults()
                runDeferredActionsIfIdle()
            default:
                break
            }
        } catch {
            durableQueueRuntimeDiagnostic = "Queue state could not be saved after the conversion finished: \(error.localizedDescription)"
            activeQueueItemID = nil
            publishCompletedTitleQueueResults()
            titleQueueGroupID = nil
            titleQueueStopRequested = false
            state.failTransport(
                message: durableQueueRuntimeDiagnostic ?? "Queue state could not be saved.",
                retryable: false
            )
            runDeferredActionsIfIdle()
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

    private func persistTitleQueueDecisionCancellation(itemID: UUID) async {
        guard activeQueueItemID == itemID else {
            return
        }
        do {
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }) else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[index].state = .stopped
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
                }
            }
            activeQueueItemID = nil
            publishTitleQueueProjection()
            runDeferredActionsIfIdle()
        } catch {
            failClosedTitleQueuePersistence(error)
        }
    }

    private func persistClearedTitleQueueDecisionCancellation(itemID: UUID, groupID: UUID?) async {
        guard let groupID else {
            return
        }
        do {
            try await durableQueueStore.mutateItems { items in
                guard let index = items.firstIndex(where: { $0.id == itemID }) else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[index].state = .stopped
                items[index].decision = nil
                items[index].failure = nil
                if let attemptIndex = items[index].attempts.lastIndex(where: { $0.endedAt == nil }) {
                    items[index].attempts[attemptIndex].endedAt = diagnosticClock()
                }
                for pendingIndex in items.indices where items[pendingIndex].groupID == groupID && items[pendingIndex].state == .waiting {
                    items[pendingIndex].state = .stopped
                }
            }
        } catch {
            durableQueueRuntimeDiagnostic = "Queue decision cancellation could not be saved: \(error.localizedDescription)"
        }
    }

    private func persistWaitingTitleQueueStop() async {
        do {
            try await stopWaitingTitleQueueItems()
            publishTitleQueueProjection()
            runDeferredActionsIfIdle()
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
        try await durableQueueStore.mutateItems { items in
            for index in items.indices where items[index].groupID == titleQueueGroupID && items[index].state == .waiting {
                items[index].state = .stopped
            }
        }
    }

    private func stopDurableTitleQueueBeforeSpawn(itemID: UUID) async {
        do {
            try await durableQueueStore.mutateItems { items in
                guard let activeIndex = items.firstIndex(where: { $0.id == itemID }) else {
                    throw ConversionQueueStoreError.invalidDocument
                }
                items[activeIndex].state = .stopped
                if let attemptIndex = items[activeIndex].attempts.lastIndex(where: { $0.endedAt == nil }) {
                    items[activeIndex].attempts[attemptIndex].endedAt = diagnosticClock()
                }
                guard let titleQueueGroupID else {
                    return
                }
                for index in items.indices where items[index].groupID == titleQueueGroupID && items[index].state == .waiting {
                    items[index].state = .stopped
                }
            }
            activeQueueItemID = nil
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
        options.job.removeOriginalAfterSuccess = removeOriginalAfterSuccess && options.job.removeOriginalAfterSuccess
        return ConversionDraft(
            source: ConversionSource(
                kind: sourceKind,
                url: URL(fileURLWithPath: item.intent.source.path),
                displayName: item.intent.source.displayName,
                workerSourcePath: item.intent.source.workerSourcePath
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

    private func conversionDraft(
        for item: DurableConversionQueueItem,
        preserveStoredSourceRemoval: Bool
    ) throws -> ConversionDraft {
        try conversionDraft(
            for: item,
            removeOriginalAfterSuccess: preserveStoredSourceRemoval
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
        case .waiting, .notStarted, .interrupted:
            .waiting
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
        case .stopped:
            .cancelled
        }
    }

    private func failClosedTitleQueuePersistence(_ error: Error) {
        durableQueueRuntimeDiagnostic = "Queue changes are unavailable: \(error.localizedDescription)"
        activeQueueItemID = nil
        titleQueueGroupID = nil
        state.failTransport(message: durableQueueRuntimeDiagnostic ?? "Queue changes are unavailable.", retryable: false)
        runDeferredActionsIfIdle()
    }

    private func resetQueue() {
        queueItems.removeAll()
        activeQueueItemID = nil
        titleQueueGroupID = nil
        titleQueueStopRequested = false
        sourceFolderQueueGroupID = nil
        sourceFolderStopRequested = false
        sourceFolderQueueCompletionPending = false
        sourceFolderRecoveryChoices.removeAll()
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
        case .singleInspection, .singleConversion, .titleQueueConversion:
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

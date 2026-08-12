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

@MainActor
final class ConversionViewModel: ObservableObject, UpdateInstallPostponing {
    typealias ClientFactory = () throws -> any WorkerProcessRunning

    @Published private(set) var source: ConversionSource?
    @Published private(set) var state = WorkerLifecycleState()
    @Published private(set) var liveObservabilityStatus = LiveObservabilityStatus.empty
    @Published private(set) var batchQueue: SourceFolderQueueState?
    @Published private(set) var queueItems: [ConversionQueueItem] = []
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
    private var batchItemDiagnosticJobIDs: [UUID: UUID] = [:]

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
            || pendingBatchContinuation != nil
            || isBatchRunning
            || hasQueuedWork
            || state.phase == .decisionRequired
    }

    var hasStoppableWork: Bool {
        hasActiveWorker
            || pendingTitleQueueTransition != nil
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

    var durableQueueLoadErrorMessage: String? {
        durableQueueStore.loadErrorMessage
    }

    var durableQueueWritesBlocked: Bool {
        durableQueueStore.writesBlocked
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
        guard !hasActiveWork, let firstDraft = drafts.first
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
                try await self.durableQueueStore.mutateItems { items in
                    guard let sourceIndex = items.firstIndex(where: { $0.id == itemID }),
                          let targetIndex = items.firstIndex(where: { $0.id == targetID }),
                          items[sourceIndex].groupID == groupID,
                          items[targetIndex].groupID == groupID,
                          items[sourceIndex].state == .waiting,
                          items[targetIndex].state == .waiting
                    else {
                        throw ConversionQueueStoreError.invalidDocument
                    }
                    let item = items.remove(at: sourceIndex)
                    let insertionIndex = items.firstIndex(where: { $0.id == targetID }) ?? targetIndex
                    items.insert(item, at: insertionIndex)
                    let sourceRemovalRequested = items.contains { queueItem in
                        queueItem.groupID == groupID
                            && queueItem.intent.options.job.removeOriginalAfterSuccess
                    }
                    let finalWaitingItemID = items.last(where: { queueItem in
                        queueItem.groupID == groupID && queueItem.state == .waiting
                    })?.id
                    for index in items.indices {
                        items[index].ordinal = index
                        if items[index].groupID == groupID, items[index].state == .waiting {
                            items[index].intent.options.job.removeOriginalAfterSuccess = sourceRemovalRequested
                                && items[index].id == finalWaitingItemID
                        }
                    }
                }
                self.publishTitleQueueProjection()
            } catch {
                self.failClosedTitleQueuePersistence(error)
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

        queue.prepareForRun(
            profile: profile,
            destinationURL: destinationURL,
            options: options
        )
        batchQueue = queue
        recordDiagnosticWorkflow(name: "batch.started", message: "source_folder")
        startNextBatchItem()
    }

    func stopActiveWorker() {
        guard hasStoppableWork else {
            return
        }
        if titleQueueGroupID != nil {
            titleQueueStopRequested = true
        }
        if var queue = batchQueue, queue.isRunning {
            queue.stopRequested = true
            queue.markPendingItemsStopped()
            if let activeItemIndex = queue.activeItemIndex {
                queue.items[activeItemIndex].status = .stopping
            }
            batchQueue = queue
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
              pendingBatchContinuation == nil,
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
            clearSourceNow()
            enqueueTitleQueueTransition { [weak self] in
                await self?.persistClearedTitleQueueDecisionCancellation(
                    itemID: activeQueueItemID,
                    groupID: groupID
                )
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
        if let pendingBatchContinuation {
            await pendingBatchContinuation.value
        }
        runDeferredActionsIfIdle()
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
              var queue = batchQueue,
              let itemIndex = queue.items.firstIndex(where: { $0.id == itemID }),
              queue.items[itemIndex].canRetry,
              let originalDraft = queue.items[itemIndex].draft
        else {
            return
        }

        let configuredRetryDraft: ConversionDraft
        if let recoveryChoice,
           let decision = queue.items[itemIndex].recoveryDecision,
           let recoveredDraft = originalDraft.retrying(decision: decision, choice: recoveryChoice)
        {
            configuredRetryDraft = recoveredDraft
        } else if queue.items[itemIndex].recoveryDecision == nil {
            configuredRetryDraft = originalDraft
        } else {
            return
        }
        let retryDraft = ConversionDraft(
            source: configuredRetryDraft.source,
            sourceDetails: nil,
            profile: configuredRetryDraft.profile,
            destinationURL: configuredRetryDraft.destinationURL,
            options: configuredRetryDraft.options
        )

        queue.stopRequested = false
        queue.hasStarted = true
        queue.completionID = nil
        queue.items[itemIndex].draft = retryDraft
        queue.items[itemIndex].status = .pending
        queue.items[itemIndex].inspection = nil
        queue.items[itemIndex].conversionResult = nil
        queue.items[itemIndex].failureMessage = nil
        queue.items[itemIndex].failureDetails = nil
        queue.items[itemIndex].failureRetryable = false
        queue.items[itemIndex].recoveryDecision = nil
        batchQueue = queue
        recordDiagnosticWorkflow(
            name: "batch.retry_requested",
            message: recoveryChoice?.rawValue,
            jobID: batchItemDiagnosticJobIDs[itemID]
        )
        startNextBatchItem()
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
        guard var queue = batchQueue,
              let itemIndex = queue.items.firstIndex(where: { $0.id == itemID })
        else {
            return
        }

        if queue.stopRequested || state.phase == .cancelled {
            queue.items[itemIndex].status = .stopped
            queue.activeItemID = nil
            queue.markPendingItemsStopped()
            batchQueue = queue
            finishBatchIfNeeded()
            return
        }

        guard state.phase == .completed,
              let inspection = state.result,
              let draft = queue.items[itemIndex].draft
        else {
            recordBatchFailure(in: &queue, itemIndex: itemIndex)
            queue.activeItemID = nil
            batchQueue = queue
            startNextBatchItem()
            return
        }

        queue.items[itemIndex].inspection = inspection
        let inspectedDraft: ConversionDraft
        if draft.source.kind.isDiscWorkflow {
            guard let mainTitle = inspection.mainTitle else {
                queue.items[itemIndex].status = .failed
                queue.items[itemIndex].failureMessage = "No convertible 3D title was found in this source."
                queue.items[itemIndex].failureDetails = "Analyze the source again after confirming it contains an MVC Blu-ray title."
                queue.items[itemIndex].failureRetryable = true
                queue.activeItemID = nil
                batchQueue = queue
                startNextBatchItem()
                return
            }
            var inspectedOptions = draft.options
            if inspection.titles.count > 1 {
                inspectedOptions.job.removeOriginalAfterSuccess = false
            }
            inspectedDraft = ConversionDraft(
                source: draft.source,
                sourceDetails: inspection,
                profile: draft.profile,
                destinationURL: draft.destinationURL,
                options: inspectedOptions,
                selectedTitle: mainTitle
            )
        } else {
            inspectedDraft = draft.withSourceDetails(inspection)
        }
        queue.items[itemIndex].draft = inspectedDraft
        if let conflictingIndex = queue.items.indices.first(where: { index in
            guard index != itemIndex,
                  queue.items[index].inspection != nil,
                  let queuedDraft = queue.items[index].draft
            else {
                return false
            }
            return queuedDraft.proposedOutputURL.standardizedFileURL.path.lowercased()
                == inspectedDraft.proposedOutputURL.standardizedFileURL.path.lowercased()
        }) {
            queue.items[itemIndex].status = .failed
            queue.items[itemIndex].failureMessage = "Another queued source resolves to the same output file."
            queue.items[itemIndex].failureDetails = "\(inspectedDraft.proposedOutputURL.path) is already reserved by \(queue.items[conflictingIndex].source.displayName)."
            queue.activeItemID = nil
            batchQueue = queue
            startNextBatchItem()
            return
        }
        queue.items[itemIndex].status = .converting
        batchQueue = queue
        startConversion(
            draft: inspectedDraft,
            mode: .batchConversion(itemID: itemID)
        )
    }

    private func completeBatchConversion(itemID: UUID) {
        guard var queue = batchQueue,
              let itemIndex = queue.items.firstIndex(where: { $0.id == itemID })
        else {
            return
        }

        if state.phase == .completed, let conversionResult = state.conversionResult {
            queue.items[itemIndex].status = .completed
            queue.items[itemIndex].conversionResult = conversionResult
        } else if queue.stopRequested || state.phase == .cancelled {
            queue.items[itemIndex].status = .stopped
        } else {
            recordBatchFailure(in: &queue, itemIndex: itemIndex)
        }

        queue.activeItemID = nil
        if queue.stopRequested {
            queue.markPendingItemsStopped()
        }
        batchQueue = queue
        startNextBatchItem()
    }

    private func recordBatchFailure(
        in queue: inout SourceFolderQueueState,
        itemIndex: Int
    ) {
        queue.items[itemIndex].status = .failed
        queue.items[itemIndex].failureMessage = state.failureMessage
            ?? state.recoveryDecision?.prompt
            ?? "The queued source could not be processed."
        queue.items[itemIndex].failureDetails = state.failureDetails
            ?? state.recoveryDecision?.details
        queue.items[itemIndex].failureRetryable = state.failureRetryable
        queue.items[itemIndex].recoveryDecision = state.recoveryDecision
    }

    private func startNextBatchItem() {
        guard var queue = batchQueue else {
            return
        }
        if queue.stopRequested {
            queue.markPendingItemsStopped()
            queue.activeItemID = nil
            batchQueue = queue
            finishBatchIfNeeded()
            return
        }
        guard let nextIndex = queue.nextPendingIndex else {
            queue.activeItemID = nil
            batchQueue = queue
            finishBatchIfNeeded()
            return
        }

        let itemID = queue.items[nextIndex].id
        let itemSource = queue.items[nextIndex].source
        queue.activeItemID = itemID
        queue.items[nextIndex].status = .inspecting
        batchQueue = queue

        state.clear()
        state.selectSource(itemSource.url)
        startInspection(
            source: itemSource,
            mode: .batchInspection(itemID: itemID)
        )
    }

    private func finishBatchIfNeeded() {
        guard var queue = batchQueue,
              queue.hasStarted,
              queue.activeItemID == nil,
              queue.nextPendingIndex == nil
        else {
            return
        }
        queue.completionID = UUID()
        batchQueue = queue
        recordDiagnosticWorkflow(name: "batch.finished")
        state.clear()
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
            switch terminalSnapshot.phase {
            case .completed:
                publishTitleQueueProjection()
                activeQueueItemID = nil
                if titleQueueStopRequested {
                    try await stopWaitingTitleQueueItems()
                    publishTitleQueueProjection()
                    runDeferredActionsIfIdle()
                } else {
                    await startNextDurableTitleQueueItem()
                }
            case .decisionRequired:
                publishTitleQueueProjection()
            case .cancelled, .failed:
                try await stopWaitingTitleQueueItems()
                publishTitleQueueProjection()
                activeQueueItemID = nil
                runDeferredActionsIfIdle()
            default:
                break
            }
        } catch {
            durableQueueRuntimeDiagnostic = "Queue state could not be saved after the conversion finished: \(error.localizedDescription)"
            activeQueueItemID = nil
            titleQueueGroupID = nil
            titleQueueStopRequested = false
            publishTitleQueueProjection()
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

    private func publishTitleQueueProjection() {
        queueItems = currentTitleQueueItems.compactMap { item in
            guard let draft = try? conversionDraft(for: item, removeOriginalAfterSuccess: false) else {
                return nil
            }
            return ConversionQueueItem(id: item.id, draft: draft, status: queueStatus(for: item))
        }
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

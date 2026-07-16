import Foundation
import SwiftUI

private enum ActiveRunMode: Equatable {
    case singleInspection
    case singleConversion
    case titleQueueConversion(index: Int)
    case batchInspection(itemID: UUID)
    case batchConversion(itemID: UUID)
}

@MainActor
final class ConversionViewModel: ObservableObject, UpdateInstallPostponing {
    typealias ClientFactory = () throws -> any WorkerProcessRunning

    @Published private(set) var source: ConversionSource?
    @Published private(set) var state = WorkerLifecycleState()
    @Published private(set) var diagnosticLog = ""
    @Published private(set) var batchQueue: SourceFolderQueueState?
    @Published private(set) var queueItems: [ConversionQueueItem] = []
    @Published private(set) var completedBatchResults: [ConversionResult]?

    private let clientFactory: ClientFactory
    private var client: (any WorkerProcessRunning)?
    private var runTask: Task<Void, Never>?
    private var pendingTerminalEvent: WorkerEvent?
    private var lastConversionDraft: ConversionDraft?
    private var activeRunMode: ActiveRunMode?
    private var pendingBatchContinuation: Task<Void, Never>?
    private var actionsWaitingForIdle: [() -> Void] = []
    private var pendingQueueIndices: [Int] = []
    private var activeQueueIndex: Int?

    init(clientFactory: @escaping ClientFactory = {
        WorkerProcessClient(configuration: try WorkerLaunchConfiguration.automatic())
    }) {
        self.clientFactory = clientFactory
    }

    var isRunning: Bool {
        state.phase.isRunning
    }

    var hasActiveWorker: Bool {
        runTask != nil
    }

    var hasActiveWork: Bool {
        hasActiveWorker
            || pendingBatchContinuation != nil
            || isBatchRunning
            || hasQueuedWork
            || state.phase == .decisionRequired
    }

    var hasStoppableWork: Bool {
        hasActiveWorker
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
        activeQueueIndex != nil || !pendingQueueIndices.isEmpty
    }

    var hasPendingWork: Bool {
        hasActiveWork
    }

    var canRetry: Bool {
        !hasActiveWork
            && state.recoveryDecision == nil
            && (state.phase == .cancelled || state.failureRetryable)
    }

    func selectSource(_ sourceURL: URL) {
        guard !hasActiveWork else {
            return
        }
        resetQueue()
        lastConversionDraft = nil
        batchQueue = nil
        guard let source = ConversionSource.infer(from: sourceURL) else {
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
        self.source = source
        state.clear()
        diagnosticLog = ""
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
            pendingTerminalEvent = nil
            activeRunMode = mode
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
                        try await self.receive(event)
                    }
                    self.finish(runResult)
                } catch {
                    self.fail(error)
                }
            }
        } catch {
            state.failTransport(message: error.localizedDescription)
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
        let normalizedDrafts = drafts.enumerated().map { index, draft in
            var options = draft.options
            if index < drafts.index(before: drafts.endIndex) {
                options.job.removeOriginalAfterSuccess = false
            }
            return ConversionDraft(
                source: draft.source,
                sourceDetails: draft.sourceDetails,
                profile: draft.profile,
                destinationURL: draft.destinationURL,
                options: options,
                selectedTitle: draft.selectedTitle
            )
        }
        queueItems = normalizedDrafts.map { ConversionQueueItem(draft: $0) }
        pendingQueueIndices = Array(queueItems.indices)
        activeQueueIndex = nil
        completedBatchResults = nil
        startNextQueuedConversion()
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
            switch mode {
            case .singleConversion, .titleQueueConversion:
                lastConversionDraft = draft
            case .singleInspection, .batchInspection, .batchConversion:
                break
            }
            pendingTerminalEvent = nil
            diagnosticLog = ""
            activeRunMode = mode
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
                        try await self.receive(event)
                    }
                    self.finish(runResult)
                } catch {
                    self.fail(error)
                }
            }
            return true
        } catch {
            state.failTransport(message: error.localizedDescription)
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
        startNextBatchItem()
    }

    func stopActiveWorker() {
        guard hasStoppableWork else {
            return
        }
        if var queue = batchQueue, queue.isRunning {
            queue.stopRequested = true
            queue.markPendingItemsStopped()
            if let activeItemIndex = queue.activeItemIndex {
                queue.items[activeItemIndex].status = .stopping
            }
            batchQueue = queue
        }
        if hasQueuedWork {
            cancelPendingQueueItems()
        }
        if hasActiveWorker {
            state.requestStop()
            client?.cancel()
        }
    }

    func prepareForRetry() {
        guard !hasActiveWork else {
            return
        }
        state.prepareForRetry()
        diagnosticLog = ""
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
        if choice == .cancel {
            state.cancelRecoveryDecision()
            if let activeQueueIndex {
                queueItems[activeQueueIndex].status = .cancelled
                self.activeQueueIndex = nil
                cancelPendingQueueItems()
                publishCompletedQueueResults()
            }
            runDeferredActionsIfIdle()
            return true
        }
        guard let lastConversionDraft,
              let retryDraft = lastConversionDraft.retrying(decision: decision, choice: choice)
        else {
            state.failTransport(
                message: "This recovery option is not available for the current conversion.",
                retryable: false
            )
            if let activeQueueIndex {
                queueItems[activeQueueIndex].status = .failed(
                    state.failureMessage ?? "The conversion could not be restarted."
                )
                self.activeQueueIndex = nil
                cancelPendingQueueItems()
                publishCompletedQueueResults()
            }
            runDeferredActionsIfIdle()
            return false
        }
        state.prepareForRetry()
        if let activeQueueIndex {
            queueItems[activeQueueIndex].status = .processing
            _ = startConversion(
                draft: retryDraft,
                mode: .titleQueueConversion(index: activeQueueIndex)
            )
        } else {
            _ = startConversion(draft: retryDraft, mode: .singleConversion)
        }
        return state.phase.isRunning
    }

    func clearSource() {
        guard !hasStoppableWork else {
            return
        }
        source = nil
        lastConversionDraft = nil
        batchQueue = nil
        resetQueue()
        state.clear()
        diagnosticLog = ""
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
        queue.items[itemIndex].diagnosticLog = ""
        batchQueue = queue
        startNextBatchItem()
    }

    func restartInspection() {
        guard canRetry else {
            return
        }
        state.prepareForRetry()
        diagnosticLog = ""
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
        if event.type.isTerminal {
            pendingTerminalEvent = event
            return
        }
        if event.type == .log || event.type == .warning, let message = event.payload.message {
            appendDiagnostic(message)
        }
        var nextState = state
        try nextState.receive(event)
        state = nextState
    }

    private func finish(_ result: WorkerRunResult) {
        appendDiagnostic(result.diagnostics)
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
        let completedMode = activeRunMode
        pendingTerminalEvent = nil
        activeRunMode = nil
        clearActiveWorker(runDeferredActions: false)
        if let completedMode {
            handleCompletedRun(completedMode)
        }
        runDeferredActionsIfIdle()
    }

    private func fail(_ error: Error) {
        let clientError = error as? WorkerClientError
        appendDiagnostic(clientError?.technicalDetails ?? "")
        if state.phase == .stopping {
            state.completeStop()
        } else {
            state.failTransport(
                message: error.localizedDescription,
                details: clientError?.technicalDetails
            )
        }
        let completedMode = activeRunMode
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
        case let .titleQueueConversion(index):
            guard activeQueueIndex == index else {
                return
            }
            if updateQueueAfterTerminalState() {
                startNextQueuedConversion()
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
        case let .titleQueueConversion(index):
            if queueItems.indices.contains(index) {
                queueItems[index].status = .failed(
                    state.failureMessage ?? "Conversion could not start."
                )
            }
            activeQueueIndex = nil
            cancelPendingQueueItems()
            publishCompletedQueueResults()
            runDeferredActionsIfIdle()
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

        queue.items[itemIndex].diagnosticLog = diagnosticLog
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
        diagnosticLog = ""
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

        queue.items[itemIndex].diagnosticLog = [
            queue.items[itemIndex].diagnosticLog,
            diagnosticLog,
        ]
        .filter { !$0.isEmpty }
        .joined(separator: "\n")

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
        diagnosticLog = ""
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
        state.clear()
        diagnosticLog = ""
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

    private func startNextQueuedConversion() {
        guard let queueIndex = pendingQueueIndices.first else {
            publishCompletedQueueResults()
            runDeferredActionsIfIdle()
            return
        }
        pendingQueueIndices.removeFirst()
        activeQueueIndex = queueIndex
        queueItems[queueIndex].status = .processing
        _ = startConversion(
            draft: queueItems[queueIndex].draft,
            mode: .titleQueueConversion(index: queueIndex)
        )
    }

    private func updateQueueAfterTerminalState() -> Bool {
        guard let activeQueueIndex else {
            return false
        }
        switch state.phase {
        case .completed:
            guard let result = state.conversionResult else {
                queueItems[activeQueueIndex].status = .failed("The conversion completed without an output result.")
                self.activeQueueIndex = nil
                cancelPendingQueueItems()
                publishCompletedQueueResults()
                return false
            }
            queueItems[activeQueueIndex].status = .completed(result)
            self.activeQueueIndex = nil
            if pendingQueueIndices.isEmpty {
                publishCompletedQueueResults()
                return false
            }
            return true
        case .decisionRequired:
            queueItems[activeQueueIndex].status = .attention(state.failureMessage ?? "Choose how to continue.")
            return false
        case .cancelled:
            queueItems[activeQueueIndex].status = .cancelled
            self.activeQueueIndex = nil
            cancelPendingQueueItems()
            publishCompletedQueueResults()
            return false
        case .failed:
            queueItems[activeQueueIndex].status = .failed(state.failureMessage ?? "Conversion failed.")
            self.activeQueueIndex = nil
            cancelPendingQueueItems()
            publishCompletedQueueResults()
            return false
        default:
            return false
        }
    }

    private func cancelPendingQueueItems() {
        for index in pendingQueueIndices {
            queueItems[index].status = .cancelled
        }
        pendingQueueIndices.removeAll()
    }

    private func publishCompletedQueueResults() {
        let results = queueItems.compactMap { item in
            if case .completed(let result) = item.status {
                return result
            }
            return nil
        }
        completedBatchResults = results.isEmpty ? nil : results
    }

    private func resetQueue() {
        queueItems.removeAll()
        pendingQueueIndices.removeAll()
        activeQueueIndex = nil
        completedBatchResults = nil
    }

    private func appendDiagnostic(_ message: String) {
        let trimmed = message.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return
        }
        if diagnosticLog.isEmpty {
            diagnosticLog = trimmed
        } else {
            diagnosticLog += "\n\(trimmed)"
        }
    }
}

import Foundation
import SwiftUI

private enum ActiveRunMode: Equatable {
    case singleInspection
    case singleConversion
    case batchInspection(itemID: UUID)
    case batchConversion(itemID: UUID)
}

@MainActor
final class ConversionViewModel: ObservableObject, UpdateInstallPostponing {
    typealias ClientFactory = () throws -> any WorkerProcessRunning

    @Published private(set) var source: ConversionSource?
    @Published private(set) var state = WorkerLifecycleState()
    @Published private(set) var diagnosticLog = ""
    @Published private(set) var batchQueue: ConversionQueueState?

    private let clientFactory: ClientFactory
    private var client: (any WorkerProcessRunning)?
    private var runTask: Task<Void, Never>?
    private var pendingTerminalEvent: WorkerEvent?
    private var lastConversionDraft: ConversionDraft?
    private var activeRunMode: ActiveRunMode?
    private var pendingBatchContinuation: Task<Void, Never>?
    private var actionsWaitingForIdle: [() -> Void] = []

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
        hasActiveWorker || pendingBatchContinuation != nil || isBatchRunning
    }

    var isBatchRunning: Bool {
        batchQueue?.isRunning == true
    }

    var activeBatchItem: ConversionQueueItem? {
        batchQueue?.activeItem
    }

    var canSelectSource: Bool {
        !hasActiveWork && state.phase != .decisionRequired
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
        lastConversionDraft = nil
        self.source = source
        state.clear()
        diagnosticLog = ""
        if source.kind == .sourceFolder {
            batchQueue = ConversionQueueState(
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
            clearActiveWorker()
            handleSynchronousRunFailure(mode)
        }
    }

    func startConversion(draft: ConversionDraft, jobID: UUID = UUID()) {
        guard !hasActiveWork else {
            return
        }
        guard let selectedSource = source,
              selectedSource == draft.source,
              state.sourceURL == selectedSource.url,
              state.result != nil
        else {
            state.failTransport(
                message: "Analyze the selected source before starting conversion.",
                retryable: false
            )
            return
        }
        startConversion(draft: draft, jobID: jobID, mode: .singleConversion)
    }

    private func startConversion(
        draft: ConversionDraft,
        jobID: UUID = UUID(),
        mode: ActiveRunMode
    ) {
        guard draft.source.kind.supportsConversion,
              FileManager.default.fileExists(atPath: draft.source.url.path)
        else {
            state.failTransport(
                message: "Conversion requires an inserted Blu-ray disc or existing Blu-ray folder, ISO, MKV, MTS, or M2TS source.",
                retryable: false
            )
            handleSynchronousRunFailure(mode)
            return
        }
        if draft.source.kind == .physicalDisc,
           Self.isInsideSourceVolume(draft.destinationURL, sourceURL: draft.source.url)
        {
            state.failTransport(
                message: "Choose a destination outside the Blu-ray disc.",
                retryable: false
            )
            handleSynchronousRunFailure(mode)
            return
        }
        let job = WorkerJobSpec(draft: draft, jobID: jobID)
        do {
            try state.begin(jobID: job.jobID, operationKind: .conversion)
            if mode == .singleConversion {
                lastConversionDraft = draft
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
        } catch {
            state.failTransport(message: error.localizedDescription)
            activeRunMode = nil
            clearActiveWorker()
            handleSynchronousRunFailure(mode)
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
        guard hasActiveWork else {
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
        guard !hasActiveWork,
              let decision = state.recoveryDecision,
              decision.supportedChoices.contains(choice)
        else {
            return false
        }
        if choice == .cancel {
            state.cancelRecoveryDecision()
            return true
        }
        guard let lastConversionDraft,
              let retryDraft = lastConversionDraft.retrying(decision: decision, choice: choice)
        else {
            state.failTransport(
                message: "This recovery option is not available for the current conversion.",
                retryable: false
            )
            return false
        }
        state.prepareForRetry()
        startConversion(draft: retryDraft)
        return state.phase.isRunning
    }

    func clearSource() {
        guard !hasActiveWork else {
            return
        }
        source = nil
        lastConversionDraft = nil
        batchQueue = nil
        state.clear()
        diagnosticLog = ""
    }

    func sourceVolumeDidUnmount(_ volumeURL: URL) {
        guard let source,
              source.kind == .physicalDisc,
              source.url == volumeURL.standardizedFileURL
        else {
            return
        }
        if hasActiveWork {
            stopActiveWorker()
        } else {
            clearSource()
        }
    }

    func stopForQuit() async {
        guard hasActiveWork else {
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
        if let task = runTask {
            state.requestStop()
            client?.cancel()
            await task.value
        }
        if let pendingBatchContinuation {
            await pendingBatchContinuation.value
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
              var queue = batchQueue,
              let itemIndex = queue.items.firstIndex(where: { $0.id == itemID }),
              queue.items[itemIndex].canRetry,
              let originalDraft = queue.items[itemIndex].draft
        else {
            return
        }

        let retryDraft: ConversionDraft
        if let recoveryChoice,
           let decision = queue.items[itemIndex].recoveryDecision,
           let recoveredDraft = originalDraft.retrying(decision: decision, choice: recoveryChoice)
        {
            retryDraft = recoveredDraft
        } else if queue.items[itemIndex].recoveryDecision == nil {
            retryDraft = originalDraft
        } else {
            return
        }

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
        clearActiveWorker()
        if let completedMode {
            handleCompletedRun(completedMode)
        }
        runActionsWaitingForIdleIfNeeded()
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
        clearActiveWorker()
        if let completedMode {
            handleCompletedRun(completedMode)
        }
        runActionsWaitingForIdleIfNeeded()
    }

    private func clearActiveWorker() {
        client = nil
        runTask = nil
    }

    private func handleCompletedRun(_ mode: ActiveRunMode) {
        switch mode {
        case .singleInspection, .singleConversion:
            return
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
            runActionsWaitingForIdleIfNeeded()
        case .batchInspection, .batchConversion:
            pendingBatchContinuation = Task { @MainActor [weak self] in
                await Task.yield()
                guard let self else {
                    return
                }
                self.pendingBatchContinuation = nil
                self.handleCompletedRun(mode)
                self.runActionsWaitingForIdleIfNeeded()
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

        let inspectedDraft = draft.withSourceDetails(inspection)
        queue.items[itemIndex].inspection = inspection
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
        in queue: inout ConversionQueueState,
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

    private func runActionsWaitingForIdleIfNeeded() {
        guard !hasActiveWork else {
            return
        }
        let actions = actionsWaitingForIdle
        actionsWaitingForIdle.removeAll()
        for action in actions {
            action()
        }
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

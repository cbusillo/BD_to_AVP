import Foundation
import SwiftUI

@MainActor
final class ConversionViewModel: ObservableObject, UpdateInstallPostponing {
    typealias ClientFactory = () throws -> any WorkerProcessRunning

    @Published private(set) var source: ConversionSource?
    @Published private(set) var state = WorkerLifecycleState()
    @Published private(set) var diagnosticLog = ""
    @Published private(set) var queueItems: [ConversionQueueItem] = []
    @Published private(set) var completedBatchResults: [ConversionResult]?

    private let clientFactory: ClientFactory
    private var client: (any WorkerProcessRunning)?
    private var runTask: Task<Void, Never>?
    private var pendingTerminalEvent: WorkerEvent?
    private var lastConversionDraft: ConversionDraft?
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

    var canSelectSource: Bool {
        !hasActiveWorker && !hasQueuedWork && state.phase != .decisionRequired
    }

    var hasQueuedWork: Bool {
        activeQueueIndex != nil || !pendingQueueIndices.isEmpty
    }

    var hasPendingWork: Bool {
        hasActiveWorker || hasQueuedWork || state.phase == .decisionRequired
    }

    var canRetry: Bool {
        !hasActiveWorker
            && state.recoveryDecision == nil
            && (state.phase == .cancelled || state.failureRetryable)
    }

    func selectSource(_ sourceURL: URL) {
        guard !hasActiveWorker else {
            return
        }
        resetQueue()
        lastConversionDraft = nil
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
        guard !hasActiveWorker else {
            return
        }
        resetQueue()
        lastConversionDraft = nil
        self.source = source
        state.clear()
        diagnosticLog = ""
        guard source.kind.supportsMetadataInspection else {
            return
        }
        state.selectSource(source.url)
        validateSelectedSourceAndStart()
    }

    func startInspection() {
        guard !hasActiveWorker, !hasQueuedWork, let source, state.sourceURL == source.url else {
            return
        }

        let job = WorkerJobSpec(source: source)
        do {
            try state.begin(jobID: job.jobID)
            pendingTerminalEvent = nil
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
            clearActiveWorker()
        }
    }

    func startConversion(draft: ConversionDraft, jobID: UUID = UUID()) {
        guard !hasActiveWorker, !hasQueuedWork else {
            return
        }
        resetQueue()
        _ = startConversion(draft: draft, jobID: jobID, queueIndex: nil)
    }

    func startConversionQueue(drafts: [ConversionDraft]) {
        guard !hasActiveWorker,
              !hasQueuedWork,
              state.phase != .decisionRequired,
              let firstDraft = drafts.first
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
        queueIndex: Int?
    ) -> Bool {
        guard !hasActiveWorker else {
            return false
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
            return false
        }
        guard draft.source.kind.supportsConversion,
              FileManager.default.fileExists(atPath: draft.source.url.path)
        else {
            state.failTransport(
                message: "Conversion requires an inserted Blu-ray disc or existing Blu-ray folder, ISO, MKV, MTS, or M2TS source.",
                retryable: false
            )
            return false
        }
        if draft.source.kind == .physicalDisc,
           Self.isInsideSourceVolume(draft.destinationURL, sourceURL: draft.source.url)
        {
            state.failTransport(
                message: "Choose a destination outside the Blu-ray disc.",
                retryable: false
            )
            return false
        }
        let job = WorkerJobSpec(draft: draft, jobID: jobID)
        do {
            try state.begin(jobID: job.jobID, operationKind: .conversion)
            lastConversionDraft = draft
            pendingTerminalEvent = nil
            diagnosticLog = ""
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
            if let queueIndex {
                queueItems[queueIndex].status = .failed(error.localizedDescription)
                activeQueueIndex = nil
                cancelPendingQueueItems()
                publishCompletedQueueResults()
            }
            clearActiveWorker(runDeferredActions: !hasQueuedWork)
            return false
        }
    }

    func stopActiveWorker() {
        guard hasActiveWorker else {
            return
        }
        cancelPendingQueueItems()
        state.requestStop()
        client?.cancel()
    }

    func prepareForRetry() {
        guard !hasActiveWorker else {
            return
        }
        state.prepareForRetry()
        diagnosticLog = ""
    }

    @discardableResult
    func resolveRecoveryChoice(_ choice: WorkerRecoveryChoice) -> Bool {
        guard !hasActiveWorker,
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
            if !startConversion(draft: retryDraft, queueIndex: activeQueueIndex) {
                queueItems[activeQueueIndex].status = .failed(
                    state.failureMessage ?? "The conversion could not be restarted."
                )
                self.activeQueueIndex = nil
                cancelPendingQueueItems()
                publishCompletedQueueResults()
                runDeferredActionsIfIdle()
            }
        } else {
            if !startConversion(draft: retryDraft, queueIndex: nil) {
                runDeferredActionsIfIdle()
            }
        }
        return state.phase.isRunning
    }

    func clearSource() {
        guard !hasActiveWorker else {
            return
        }
        source = nil
        lastConversionDraft = nil
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
        if hasActiveWorker {
            stopActiveWorker()
        } else {
            clearSource()
        }
    }

    func stopForQuit() async {
        guard let task = runTask else {
            return
        }
        state.requestStop()
        client?.cancel()
        await task.value
    }

    func postponeInstallUntilIdle(_ installHandler: @escaping () -> Void) -> Bool {
        guard hasPendingWork else {
            return false
        }
        actionsWaitingForIdle.append(installHandler)
        return true
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
        let continueQueue = updateQueueAfterTerminalState()
        pendingTerminalEvent = nil
        clearActiveWorker(runDeferredActions: !hasQueuedWork)
        if continueQueue {
            startNextQueuedConversion()
        }
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
        _ = updateQueueAfterTerminalState()
        pendingTerminalEvent = nil
        clearActiveWorker(runDeferredActions: !hasQueuedWork)
    }

    private func clearActiveWorker(runDeferredActions: Bool = true) {
        client = nil
        runTask = nil
        guard runDeferredActions else {
            return
        }
        runDeferredActionsIfIdle()
    }

    private func runDeferredActionsIfIdle() {
        guard !hasActiveWorker, !hasQueuedWork, state.phase != .decisionRequired else {
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
        if !startConversion(draft: queueItems[queueIndex].draft, queueIndex: queueIndex) {
            queueItems[queueIndex].status = .failed(state.failureMessage ?? "Conversion could not start.")
            activeQueueIndex = nil
            cancelPendingQueueItems()
            publishCompletedQueueResults()
            runDeferredActionsIfIdle()
        }
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

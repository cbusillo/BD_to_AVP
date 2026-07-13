import Foundation
import SwiftUI

@MainActor
final class ConversionViewModel: ObservableObject {
    typealias ClientFactory = () throws -> any WorkerProcessRunning

    @Published private(set) var source: ConversionSource?
    @Published private(set) var state = WorkerLifecycleState()
    @Published private(set) var diagnosticLog = ""

    private let clientFactory: ClientFactory
    private var client: (any WorkerProcessRunning)?
    private var runTask: Task<Void, Never>?
    private var pendingTerminalEvent: WorkerEvent?
    private var lastConversionDraft: ConversionDraft?

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
        !hasActiveWorker && state.phase != .decisionRequired
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
        guard !hasActiveWorker, let source, state.sourceURL == source.url else {
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
            client = nil
            runTask = nil
        }
    }

    func startConversion(draft: ConversionDraft) {
        guard !hasActiveWorker else {
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
        guard draft.source.kind.supportsConversion,
              FileManager.default.fileExists(atPath: draft.source.url.path)
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
        guard draft.outputLength == .fullMovie else {
            state.failTransport(
                message: "Short sample conversion is not available yet. Choose Full Movie.",
                retryable: false
            )
            return
        }

        let job = WorkerJobSpec(draft: draft)
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
        } catch {
            state.failTransport(message: error.localizedDescription)
            client = nil
            runTask = nil
        }
    }

    func stopActiveWorker() {
        guard hasActiveWorker else {
            return
        }
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
        guard !hasActiveWorker else {
            return
        }
        source = nil
        lastConversionDraft = nil
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
        pendingTerminalEvent = nil
        client = nil
        runTask = nil
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
        pendingTerminalEvent = nil
        client = nil
        runTask = nil
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

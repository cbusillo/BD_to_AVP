import Foundation
import SwiftUI

@MainActor
final class ConversionViewModel: ObservableObject {
    typealias ClientFactory = () throws -> any WorkerProcessRunning

    static let supportedExtensions = Set(["mkv", "mts", "m2ts"])

    @Published private(set) var source: ConversionSource?
    @Published private(set) var state = WorkerLifecycleState()
    @Published private(set) var diagnosticLog = ""

    private let clientFactory: ClientFactory
    private var client: (any WorkerProcessRunning)?
    private var runTask: Task<Void, Never>?
    private var pendingTerminalEvent: WorkerEvent?

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
        !hasActiveWorker
    }

    var canRetry: Bool {
        !hasActiveWorker && (state.phase == .cancelled || state.failureRetryable)
    }

    func selectSource(_ sourceURL: URL) {
        guard !hasActiveWorker else {
            return
        }
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
        guard !hasActiveWorker, let sourceURL = state.sourceURL else {
            return
        }

        let job = WorkerJobSpec(sourceURL: sourceURL)
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
        guard draft.source.kind.supportsConversion else {
            return
        }

        let job = WorkerJobSpec(draft: draft)
        do {
            try state.begin(jobID: job.jobID, operationKind: .conversion)
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

    func stopActiveWorker() {
        guard hasActiveWorker else {
            return
        }
        state.requestStop()
        client?.cancel()
    }

    func stopInspection() {
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

    func clearSource() {
        guard !hasActiveWorker else {
            return
        }
        source = nil
        state.clear()
        diagnosticLog = ""
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
        guard let sourceURL = state.sourceURL else {
            state.failTransport(message: "Choose a source before continuing.", retryable: false)
            return
        }
        guard Self.supportedExtensions.contains(sourceURL.pathExtension.lowercased()) else {
            state.failTransport(message: "Choose an MKV, MTS, or M2TS source file.", retryable: false)
            return
        }
        guard FileManager.default.fileExists(atPath: sourceURL.path) else {
            state.failTransport(message: "The selected source no longer exists.", retryable: false)
            return
        }
        startInspection()
    }

    private func receive(_ event: WorkerEvent) throws {
        if event.type.isTerminal {
            pendingTerminalEvent = event
            return
        }
        var nextState = state
        try nextState.receive(event)
        state = nextState
    }

    private func finish(_ result: WorkerRunResult) {
        diagnosticLog = result.diagnostics
        if state.phase == .stopping {
            state.completeStop()
        } else if let pendingTerminalEvent {
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
        } else {
            state.failTransport(
                message: "The source analysis ended before results were available.",
                details: result.diagnostics.isEmpty ? nil : result.diagnostics
            )
        }
        pendingTerminalEvent = nil
        client = nil
        runTask = nil
    }

    private func fail(_ error: Error) {
        let clientError = error as? WorkerClientError
        diagnosticLog = clientError?.technicalDetails ?? ""
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
}

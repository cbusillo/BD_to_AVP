import Foundation

private enum PreviewViewModelError: Error, LocalizedError {
    case unsupportedSource
    case invalidArtifact
    case missingArtifact

    var errorDescription: String? {
        switch self {
        case .unsupportedSource:
            "Preview currently supports MKV, MTS, M2TS, and ISO sources."
        case .invalidArtifact:
            "The preview engine returned an artifact outside its owned cache."
        case .missingArtifact:
            "The preview ended before a playable artifact was available."
        }
    }
}

@MainActor
final class PreviewViewModel: ObservableObject, UpdateInstallPostponing {
    typealias ClientFactory = () throws -> any WorkerProcessRunning

    @Published private(set) var phase: PreviewPhase = .idle
    @Published private(set) var stageMessage = "Choose a preview range."
    @Published private(set) var activityMessage: String?
    @Published private(set) var failureMessage: String?
    @Published private(set) var diagnosticLog = ""
    @Published private(set) var artifactLease: PreviewArtifactLease?
    @Published private(set) var reviewedDraft: PreviewDraft?
    @Published private(set) var elapsedSeconds = 0
    @Published private(set) var progress: WorkerProgress?

    private let clientFactory: ClientFactory
    private let cache: PreviewCache
    private var client: (any WorkerProcessRunning)?
    private var runTask: Task<Void, Never>?
    private var activeDraft: PreviewDraft?
    private var activeDirectoryURL: URL?
    private var pendingTerminalEvent: WorkerEvent?
    private var actionsWaitingForIdle: [() -> Void] = []

    init(
        clientFactory: @escaping ClientFactory = {
            WorkerProcessClient(configuration: try WorkerLaunchConfiguration.automatic())
        },
        cache: PreviewCache = .automatic()
    ) {
        self.clientFactory = clientFactory
        self.cache = cache
        cache.removeExpired()
    }

    var hasActiveWorker: Bool {
        runTask != nil
    }

    var hasActiveWork: Bool {
        hasActiveWorker
    }

    var canStart: Bool {
        !hasActiveWorker
    }

    var elapsedText: String? {
        ElapsedTimeText.format(seconds: elapsedSeconds)
    }

    func startPreview(_ draft: PreviewDraft) {
        guard !hasActiveWorker else {
            return
        }
        guard draft.conversion.source.kind == .matroska
                || draft.conversion.source.kind == .transportStream
                || draft.conversion.source.kind == .discImage
        else {
            fail(PreviewViewModelError.unsupportedSource)
            return
        }
        guard FileManager.default.fileExists(atPath: draft.conversion.source.url.path) else {
            failTransport("The selected source no longer exists.")
            return
        }

        releaseArtifact()
        diagnosticLog = ""
        failureMessage = nil
        activityMessage = nil
        reviewedDraft = nil
        pendingTerminalEvent = nil
        elapsedSeconds = 0
        progress = nil

        let jobID = UUID()
        do {
            let directoryURL = try cache.prepareDirectory(jobID: jobID)
            activeDraft = draft
            activeDirectoryURL = directoryURL
            let job = WorkerJobSpec(
                previewDraft: draft,
                destinationURL: directoryURL,
                jobID: jobID
            )
            let client = try clientFactory()
            self.client = client
            phase = .preparing
            stageMessage = "Preparing Preview"
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
            fail(error)
        }
    }

    func stopActiveWorker() {
        guard hasActiveWorker else {
            return
        }
        phase = .stopping
        stageMessage = "Stopping Preview"
        progress = nil
        client?.cancel()
    }

    func stopForQuit() async {
        guard let task = runTask else {
            return
        }
        stopActiveWorker()
        await task.value
    }

    func discardPreview() {
        guard !hasActiveWorker else {
            return
        }
        releaseArtifact()
        reviewedDraft = nil
        phase = .expired
        stageMessage = "Preview Expired"
        activityMessage = "The cached preview was removed."
    }

    func validateArtifact() {
        guard let artifactLease else {
            return
        }
        guard cache.fileManager.fileExists(atPath: artifactLease.artifact.outputURL.path) else {
            self.artifactLease = nil
            reviewedDraft = nil
            phase = .expired
            stageMessage = "Preview Expired"
            activityMessage = "The cached preview is no longer available."
            return
        }
    }

    func postponeInstallUntilIdle(_ installHandler: @escaping () -> Void) -> Bool {
        guard hasActiveWorker else {
            return false
        }
        actionsWaitingForIdle.append(installHandler)
        return true
    }

    private func receive(_ event: WorkerEvent) throws {
        if event.type.isTerminal {
            pendingTerminalEvent = event
            return
        }
        guard phase != .stopping else {
            return
        }

        switch event.type {
        case .workerReady, .jobStarted:
            phase = .preparing
            stageMessage = "Preparing Preview"
        case .stageStarted:
            let stage = event.payload.stage ?? ""
            progress = event.payload.progress?.normalized
            if Self.encodingStages.contains(stage) {
                phase = .encoding
                stageMessage = event.payload.message ?? "Encoding Preview"
            } else {
                phase = .preparing
                stageMessage = event.payload.message ?? "Preparing Preview"
            }
        case .heartbeat:
            elapsedSeconds = event.payload.elapsedSeconds ?? elapsedSeconds
            activityMessage = event.payload.message
            if let incomingProgress = event.payload.progress {
                progress = incomingProgress.normalized
            }
        case .log, .warning:
            if let message = event.payload.message {
                activityMessage = message
                appendDiagnostic(message)
            }
        case .artifactReady:
            guard let artifact = event.payload.artifact else {
                throw WorkerLifecycleError.missingPayload(event: event.type)
            }
            try accept(artifact)
        case .jobCompleted, .jobFailed, .jobCancelled, .jobDecisionRequired:
            break
        }
    }

    private func accept(_ artifact: PreviewArtifact) throws {
        guard let activeDirectoryURL,
              let activeDraft,
              artifact.parentJobID == activeDraft.parentJobID,
              cache.contains(artifact.outputURL, in: activeDirectoryURL),
              cache.fileManager.fileExists(atPath: artifact.outputURL.path)
        else {
            throw PreviewViewModelError.invalidArtifact
        }
        artifactLease = PreviewArtifactLease(
            artifact: artifact,
            directoryURL: activeDirectoryURL,
            cache: cache
        )
        reviewedDraft = activeDraft
        phase = .ready
        stageMessage = "Ready to Play"
        activityMessage = nil
        progress = nil
    }

    private func finish(_ result: WorkerRunResult) {
        appendDiagnostic(result.diagnostics)
        guard let terminalEvent = pendingTerminalEvent else {
            failTransport("The preview ended before a playable artifact was available.")
            return
        }

        switch terminalEvent.type {
        case .jobCompleted:
            guard let completedArtifact = terminalEvent.payload.previewResult,
                  artifactLease?.artifact == completedArtifact else {
                failTransport(PreviewViewModelError.missingArtifact.localizedDescription)
                return
            }
            phase = .ready
            stageMessage = "Ready to Play"
            clearActiveWorker(preserveDirectory: true)
        case .jobFailed:
            let failure = terminalEvent.payload.error
            failureMessage = failure?.message ?? "The preview could not be created."
            if let details = failure?.details {
                appendDiagnostic(details)
            }
            phase = .failed
            stageMessage = "Preview Failed"
            clearActiveWorker(preserveDirectory: false)
        case .jobCancelled:
            phase = .idle
            stageMessage = "Preview Stopped"
            activityMessage = terminalEvent.payload.message
            clearActiveWorker(preserveDirectory: false)
        case .jobDecisionRequired:
            failureMessage = "Preview cannot continue from partial output."
            phase = .failed
            stageMessage = "Preview Failed"
            clearActiveWorker(preserveDirectory: false)
        case .workerReady, .jobStarted, .stageStarted, .heartbeat, .log, .warning, .artifactReady:
            failTransport("The preview engine returned an invalid terminal event.")
        }
    }

    private func fail(_ error: Error) {
        let clientError = error as? WorkerClientError
        appendDiagnostic(clientError?.technicalDetails ?? "")
        if phase == .stopping {
            phase = .idle
            stageMessage = "Preview Stopped"
            activityMessage = "Preview generation was cancelled."
        } else {
            failureMessage = error.localizedDescription
            phase = .failed
            stageMessage = "Preview Failed"
        }
        clearActiveWorker(preserveDirectory: false)
    }

    private func failTransport(_ message: String) {
        failureMessage = message
        phase = .failed
        stageMessage = "Preview Failed"
        clearActiveWorker(preserveDirectory: false)
    }

    private func clearActiveWorker(preserveDirectory: Bool) {
        let artifactOwnedDirectory = artifactLease != nil
        if !preserveDirectory {
            releaseArtifact()
            reviewedDraft = nil
        }
        if !preserveDirectory, !artifactOwnedDirectory, let activeDirectoryURL {
            try? cache.remove(activeDirectoryURL)
        }
        client = nil
        runTask = nil
        activeDraft = nil
        activeDirectoryURL = nil
        pendingTerminalEvent = nil
        progress = nil

        let actions = actionsWaitingForIdle
        actionsWaitingForIdle.removeAll()
        for action in actions {
            action()
        }
    }

    private func releaseArtifact() {
        artifactLease = nil
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

    private static let encodingStages: Set<String> = [
        "create_left_right_files",
        "combine_to_mv_hevc",
        "encode_av1_stereo",
        "finalize_av1_stereo",
        "upscale_video",
        "transcode_audio",
        "create_final_file",
        "move_files",
    ]
}

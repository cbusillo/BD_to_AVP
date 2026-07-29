import Foundation

private enum PreviewViewModelError: Error, LocalizedError {
    case unsupportedSource
    case workspacePreparationFailed(volumeName: String)
    case insufficientCapacity(volumeName: String, requiredBytes: Int64, availableBytes: Int64)
    case invalidArtifact
    case missingArtifact

    var errorDescription: String? {
        switch self {
        case .unsupportedSource:
            "Preview currently supports MKV, MTS, M2TS, ISO, and Blu-ray-folder sources."
        case .workspacePreparationFailed(let volumeName):
            "The temporary preview workspace could not be prepared on \(volumeName). Make sure the destination is mounted and writable, and that no file is using the reserved .BluRayToVisionProPreviews name, then try again."
        case .insufficientCapacity(let volumeName, _, _):
            "The temporary preview workspace on \(volumeName) does not have enough confirmed free space. Free space there or choose another destination, then try again."
        case .invalidArtifact:
            "The preview engine returned an artifact outside its owned temporary workspace."
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
    @Published private(set) var artifactLease: PreviewArtifactLease?
    @Published private(set) var reviewedDraft: PreviewDraft?
    @Published private(set) var elapsedSeconds = 0
    @Published private(set) var progress: WorkerProgress?
    @Published private(set) var videoRoute: VideoRouteReport?
    @Published private(set) var capacityWarning: String?
    @Published private(set) var cleanupWarning: String?
    @Published private(set) var isCleaningUp = false

    private let clientFactory: ClientFactory
    private let cache: PreviewCache
    private let capacityProvider: any PreviewCapacityProviding
    private let diagnosticClock: () -> Date
    private let diagnosticStorageProbe: any DiagnosticStorageProbing
    private let diagnosticBundleBuilder: DiagnosticBundleBuilder
    private let observabilityEventStore: any ObservabilityEventPersisting
    private let diagnosticRecorder = DiagnosticSessionRecorder()
    private var client: (any WorkerProcessRunning)?
    private var runTask: Task<Void, Never>?
    private var workspacePreparationTask: Task<PreviewWorkspacePreparation, Error>?
    private var cleanupTask: Task<Void, Never>?
    private var activeDraft: PreviewDraft?
    private var activeDirectoryURL: URL?
    private var activeCache: PreviewCache?
    private var pendingTerminalEvent: WorkerEvent?
    private var actionsWaitingForIdle: [() -> Void] = []
    private var diagnosticState = WorkerLifecycleState()

    init(
        clientFactory: @escaping ClientFactory = {
            WorkerProcessClient(configuration: try WorkerLaunchConfiguration.automatic())
        },
        cache: PreviewCache = .automatic(),
        capacityProvider: (any PreviewCapacityProviding)? = nil,
        diagnosticClock: @escaping () -> Date = Date.init,
        diagnosticStorageProbe: any DiagnosticStorageProbing = FileSystemDiagnosticStorageProbe(),
        diagnosticBundleBuilder: DiagnosticBundleBuilder? = nil,
        observabilityEventStore: any ObservabilityEventPersisting = NullObservabilityEventStore.shared
    ) {
        self.clientFactory = clientFactory
        self.cache = cache
        self.capacityProvider = capacityProvider ?? SystemPreviewCapacityProvider(fileManager: cache.fileManager)
        self.diagnosticClock = diagnosticClock
        self.diagnosticStorageProbe = diagnosticStorageProbe
        self.diagnosticBundleBuilder = diagnosticBundleBuilder
            ?? DiagnosticBundleBuilder(storageProbe: diagnosticStorageProbe)
        self.observabilityEventStore = observabilityEventStore
        cache.removeExpired()
    }

    var hasActiveWorker: Bool {
        runTask != nil
    }

    var hasActiveWork: Bool {
        hasActiveWorker || isCleaningUp
    }

    var canStart: Bool {
        !hasActiveWorker && !isCleaningUp
    }

    var hasDiagnosticEvidence: Bool {
        diagnosticRecorder.currentJobContext != nil
    }

    var elapsedText: String? {
        ElapsedTimeText.format(seconds: elapsedSeconds)
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
            lifecycle: diagnosticState,
            activeMode: "preview",
            batchSummary: nil,
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

    func startPreview(_ draft: PreviewDraft) {
        guard canStart else {
            return
        }
        guard draft.conversion.source.kind == .matroska
                || draft.conversion.source.kind == .transportStream
                || draft.conversion.source.kind == .discImage
                || draft.conversion.source.kind == .bluRayFolder
        else {
            fail(PreviewViewModelError.unsupportedSource)
            return
        }
        guard FileManager.default.fileExists(atPath: draft.conversion.source.url.path) else {
            failTransport("The selected source no longer exists.")
            return
        }

        releaseArtifact()
        failureMessage = nil
        activityMessage = nil
        reviewedDraft = nil
        pendingTerminalEvent = nil
        elapsedSeconds = 0
        progress = nil
        videoRoute = nil
        capacityWarning = nil
        cleanupWarning = nil

        let jobID = UUID()
        let workspacePlan = PreviewWorkspacePlan.resolve(draft: draft, localCache: cache)
        let plannedWorkspaceURL = workspacePlan.cache.rootURL
            .appendingPathComponent(jobID.uuidString, isDirectory: true)
        let sourceKind = draft.conversion.source.kind
        let sourceURL = draft.conversion.source.url
        let inspectedSizeBytes = draft.conversion.sourceDetails?.sizeBytes
        let capacityProvider = capacityProvider
        activeDraft = draft
        phase = .preparing
        stageMessage = "Preparing Preview Workspace"
        prepareDiagnosticSession(
            jobID: jobID,
            draft: draft,
            workspaceURL: plannedWorkspaceURL
        )

        let preparationTask = Task.detached(priority: .userInitiated) {
            try PreviewWorkspacePreparer.prepare(
                jobID: jobID,
                plan: workspacePlan,
                sourceKind: sourceKind,
                sourceURL: sourceURL,
                inspectedSizeBytes: inspectedSizeBytes,
                capacityProvider: capacityProvider
            )
        }
        workspacePreparationTask = preparationTask
        runTask = Task { [weak self] in
            guard let self else {
                preparationTask.cancel()
                return
            }
            do {
                let preparation = try await preparationTask.value
                self.workspacePreparationTask = nil
                self.activeDirectoryURL = preparation.directoryURL
                self.activeCache = preparation.cache
                self.applyCapacityAssessment(
                    preparation.capacityAssessment,
                    volumeName: preparation.volumeName
                )
                self.recordCapacityResult(
                    preparation.capacityResult,
                    role: .previewWorkspace,
                    url: preparation.directoryURL,
                    jobID: jobID
                )
                self.scheduleDiagnosticStorageSample(recordedAt: self.diagnosticClock(), force: true)
                try Task.checkCancellation()
                let directoryURL = preparation.directoryURL
                let job = WorkerJobSpec(
                    previewDraft: draft,
                    destinationURL: directoryURL,
                    jobID: jobID
                )
                let client = try clientFactory()
                self.client = client
                phase = .preparing
                stageMessage = "Preparing Preview"
                let runResult = try await client.run(job: job) { [weak self] event in
                    guard let self else {
                        return
                    }
                    try await self.receive(event)
                }
                self.finish(runResult)
            } catch let error as PreviewWorkspacePreparationError {
                self.failWorkspacePreparation(error)
            } catch {
                self.fail(error)
            }
        }
    }

    func stopActiveWorker() {
        guard hasActiveWorker else {
            return
        }
        phase = .stopping
        stageMessage = "Stopping Preview"
        progress = nil
        diagnosticState.requestStop()
        diagnosticRecorder.recordWorkflow(
            name: "job.stop_requested",
            lifecycle: diagnosticState,
            activeMode: "preview",
            recordedAt: diagnosticClock(),
            jobID: diagnosticState.jobID
        )
        if let client {
            client.cancel()
        } else {
            workspacePreparationTask?.cancel()
            runTask?.cancel()
        }
    }

    func stopForQuit() async {
        if let task = runTask {
            stopActiveWorker()
            await task.value
        }
        if let cleanupTask {
            await cleanupTask.value
        }
    }

    func discardPreview() {
        guard !hasActiveWorker else {
            return
        }
        releaseArtifact()
        reviewedDraft = nil
        videoRoute = nil
        phase = .expired
        stageMessage = "Preview Expired"
        activityMessage = "The temporary preview was removed."
    }

    func validateArtifact() {
        guard let artifactLease else {
            return
        }
        guard artifactLease.fileManager.fileExists(atPath: artifactLease.artifact.outputURL.path) else {
            self.artifactLease = nil
            reviewedDraft = nil
            videoRoute = nil
            phase = .expired
            stageMessage = "Preview Expired"
            activityMessage = "The temporary preview is no longer available."
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

    private func receive(_ event: WorkerEvent) throws {
        let recordedAt = diagnosticClock()
        if let observabilityEvent = event.payload.observabilityEvent {
            observabilityEventStore.append(observabilityEvent)
        }
        var nextDiagnosticState = diagnosticState
        try nextDiagnosticState.receive(event)
        diagnosticState = nextDiagnosticState
        diagnosticRecorder.record(
            event: event,
            lifecycle: diagnosticState,
            activeMode: "preview",
            recordedAt: recordedAt
        )
        if event.type.isTerminal
            || event.type == .heartbeat
            || event.type == .stageStarted
            || event.type == .artifactReady {
            scheduleDiagnosticStorageSample(
                recordedAt: recordedAt,
                force: event.type.isTerminal || event.type == .artifactReady
            )
        }
        if let reportedVideoRoute = event.payload.videoRoute {
            videoRoute = reportedVideoRoute
        }
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
            }
        case .artifactReady:
            guard let artifact = event.payload.artifact else {
                throw WorkerLifecycleError.missingPayload(event: event.type)
            }
            try accept(artifact)
        case .observability:
            break
        case .jobCompleted, .jobFailed, .jobCancelled, .jobDecisionRequired:
            break
        }
    }

    private func accept(_ artifact: PreviewArtifact) throws {
        guard let activeDirectoryURL,
              let activeCache,
              let activeDraft,
              artifact.parentJobID == activeDraft.parentJobID,
              activeCache.contains(artifact.outputURL, in: activeDirectoryURL),
              activeCache.fileManager.fileExists(atPath: artifact.outputURL.path)
        else {
            throw PreviewViewModelError.invalidArtifact
        }
        artifactLease = PreviewArtifactLease(
            artifact: artifact,
            directoryURL: activeDirectoryURL,
            cache: activeCache,
            cleanupFailureHandler: { [weak self] in
                Task { @MainActor [weak self] in
                    self?.cleanupWarning = Self.cleanupFailureMessage
                }
            }
        )
        videoRoute = artifact.videoRoute ?? videoRoute
        reviewedDraft = activeDraft
        phase = .ready
        stageMessage = "Ready to Play"
        activityMessage = nil
        progress = nil
    }

    private func finish(_ result: WorkerRunResult) {
        let processSnapshot = result.diagnosticSnapshot == .empty
            ? client?.diagnosticSnapshot() ?? .empty
            : result.diagnosticSnapshot
        diagnosticRecorder.updateProcessSnapshot(processSnapshot)
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
            videoRoute = completedArtifact.videoRoute ?? videoRoute
            phase = .ready
            stageMessage = "Ready to Play"
            clearActiveWorker(preserveDirectory: true)
        case .jobFailed:
            let failure = terminalEvent.payload.error
            failureMessage = failure?.message ?? "The preview could not be created."
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
        case .workerReady, .jobStarted, .stageStarted, .heartbeat, .log, .warning, .artifactReady, .observability:
            failTransport("The preview engine returned an invalid terminal event.")
        }
        diagnosticRecorder.recordWorkflow(
            name: "process.exited",
            lifecycle: diagnosticState,
            activeMode: "preview",
            recordedAt: diagnosticClock(),
            jobID: terminalEvent.jobID,
            exitStatus: result.exitStatus
        )
        scheduleDiagnosticStorageSample(recordedAt: diagnosticClock(), force: true)
    }

    private func fail(_ error: Error) {
        recordDiagnosticFailure(error)
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
        diagnosticState.failTransport(message: message)
        diagnosticRecorder.recordWorkflow(
            name: "process.failed",
            lifecycle: diagnosticState,
            activeMode: "preview",
            recordedAt: diagnosticClock(),
            message: message,
            jobID: diagnosticState.jobID
        )
        scheduleDiagnosticStorageSample(recordedAt: diagnosticClock(), force: true)
        failureMessage = message
        phase = .failed
        stageMessage = "Preview Failed"
        clearActiveWorker(preserveDirectory: false)
    }

    private func applyCapacityAssessment(
        _ assessment: PreviewCapacityAssessment?,
        volumeName: String
    ) {
        switch assessment {
        case .unknown:
            capacityWarning = "Available space for the temporary preview workspace on \(volumeName) could not be confirmed. The preview will continue; if it stops, free space there or choose another destination and try again."
        case .conflicting:
            capacityWarning = "The destination volume \(volumeName) reported conflicting free-space values for the temporary preview workspace. The preview will continue; if it stops, free space there or choose another destination and try again."
        case .sufficient, .insufficient, .none:
            break
        }
    }

    private func failWorkspacePreparation(_ error: PreviewWorkspacePreparationError) {
        switch error {
        case .insufficientCapacity(let volumeName, let requiredBytes, let availableBytes):
            fail(
                PreviewViewModelError.insufficientCapacity(
                    volumeName: volumeName,
                    requiredBytes: requiredBytes,
                    availableBytes: availableBytes
                )
            )
        case .unavailable(let volumeName):
            fail(PreviewViewModelError.workspacePreparationFailed(volumeName: volumeName))
        }
    }

    private func clearActiveWorker(preserveDirectory: Bool) {
        let artifactOwnedDirectory = artifactLease != nil
        let directoryCleanup: (PreviewCache, URL)?
        if !preserveDirectory {
            releaseArtifact()
            reviewedDraft = nil
        }
        if !preserveDirectory,
           !artifactOwnedDirectory,
           let activeDirectoryURL,
           let activeCache {
            directoryCleanup = (activeCache, activeDirectoryURL)
        } else {
            directoryCleanup = nil
        }
        client = nil
        runTask = nil
        workspacePreparationTask = nil
        activeDraft = nil
        activeDirectoryURL = nil
        activeCache = nil
        pendingTerminalEvent = nil
        progress = nil

        if let directoryCleanup {
            startCleanup(cache: directoryCleanup.0, directoryURL: directoryCleanup.1)
        } else {
            finishWaitingActions()
        }
    }

    private func startCleanup(cache: PreviewCache, directoryURL: URL) {
        isCleaningUp = true
        let backgroundTask = Task.detached(priority: .utility) { () -> Bool in
            await PreviewCleanup.remove(cache: cache, directoryURL: directoryURL)
        }
        cleanupTask = Task { [weak self] in
            let succeeded = await backgroundTask.value
            guard let self else {
                return
            }
            if !succeeded {
                self.cleanupWarning = Self.cleanupFailureMessage
            }
            self.isCleaningUp = false
            self.cleanupTask = nil
            self.finishWaitingActions()
        }
    }

    private func finishWaitingActions() {
        let actions = actionsWaitingForIdle
        actionsWaitingForIdle.removeAll()
        for action in actions {
            action()
        }
    }

    private func releaseArtifact() {
        artifactLease = nil
    }

    private func prepareDiagnosticSession(
        jobID: UUID,
        draft: PreviewDraft,
        workspaceURL: URL
    ) {
        diagnosticRecorder.reset()
        diagnosticState.clear()
        diagnosticState.selectSource(draft.conversion.source.url)
        do {
            try diagnosticState.begin(jobID: jobID, operationKind: .preview)
        } catch {
            diagnosticState.failTransport(message: "The preview diagnostic session could not start.")
        }
        diagnosticRecorder.beginJob(
            context: DiagnosticJobContext(jobID: jobID, draft: draft, workspaceURL: workspaceURL),
            lifecycle: diagnosticState,
            activeMode: "preview",
            recordedAt: diagnosticClock()
        )
        scheduleDiagnosticStorageSample(recordedAt: diagnosticClock(), force: true)
    }

    private func recordCapacityResult(
        _ capacity: StorageCapacityResult?,
        role: DiagnosticStorageRole,
        url: URL,
        jobID: UUID
    ) {
        guard let capacity else {
            return
        }
        let capturedAt = diagnosticClock()
        let probe = RawDiagnosticStorageProbe(
            capturedAt: capturedAt,
            role: role,
            url: url.resolvingSymlinksInPath().standardizedFileURL,
            status: .available,
            isDirectory: true,
            isReadable: nil,
            isWritable: nil,
            fileSizeBytes: nil,
            modificationAgeSeconds: nil,
            volumeAvailableBytes: capacity.availableBytes,
            volumeTotalBytes: nil,
            volumeReadOnly: nil,
            errorKind: nil,
            capacity: capacity
        )
        diagnosticRecorder.recordStorageSamples(
            [RawDiagnosticStorageSample(probe: probe)],
            for: jobID
        )
    }

    private func recordDiagnosticFailure(_ error: Error) {
        if let processSnapshot = client?.diagnosticSnapshot() {
            diagnosticRecorder.updateProcessSnapshot(processSnapshot)
        }
        let message: String
        if phase == .stopping || error is CancellationError {
            diagnosticState.completeStop()
            message = "The preview was cancelled."
        } else if let previewError = error as? PreviewViewModelError {
            switch previewError {
            case .insufficientCapacity:
                message = "The preview workspace capacity preflight reported known insufficient space."
            case .workspacePreparationFailed:
                message = "The preview workspace could not be prepared."
            case .unsupportedSource:
                message = "The selected source is not supported for preview."
            case .invalidArtifact, .missingArtifact:
                message = "The preview did not produce a valid owned artifact."
            }
            diagnosticState.failTransport(message: message)
        } else {
            message = "The preview process failed."
            diagnosticState.failTransport(
                message: message,
                details: (error as? WorkerClientError)?.technicalDetails
            )
        }
        diagnosticRecorder.recordWorkflow(
            name: phase == .stopping ? "process.cancelled" : "process.failed",
            lifecycle: diagnosticState,
            activeMode: "preview",
            recordedAt: diagnosticClock(),
            message: message,
            details: (error as? WorkerClientError)?.technicalDetails,
            jobID: diagnosticState.jobID,
            exitStatus: (error as? WorkerClientError)?.processExitStatus
        )
        scheduleDiagnosticStorageSample(recordedAt: diagnosticClock(), force: true)
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

    private static let cleanupFailureMessage = "The temporary preview workspace could not be removed completely. After other previews finish, remove the hidden .BluRayToVisionProPreviews folder from the selected destination."
}

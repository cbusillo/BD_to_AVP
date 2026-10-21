import Darwin
import Foundation

/// Bounds and timings for one live relay stream. The defaults are the Mac's production values.
struct LiveRelayStreamConfiguration: Sendable {
    /// The worker's published window: about 107 s of media at 20 Mbps, so a 30 s lead leaves about
    /// 75 s behind the viewer for rewind.
    var retentionSeconds = 120
    var retentionMaxBytes = 256 * 1_024 * 1_024
    var viewerLeadSeconds = 30
    var maximumPairs: Int?
    var maximumMediaBytes = 64 * 1_024 * 1_024
    var pollInterval: TimeInterval = 0.25
    var idleTimeout: TimeInterval = 60
    var initialLeaseTimeout: TimeInterval = 120
    var sourceRecheckInterval: TimeInterval = 5
    /// How long a stop waits for the worker after cancelling it before killing its process group.
    var workerExitGrace: TimeInterval = 10
    /// After the grace, how long an unconfirmed exit may take before the drain reports it is stuck.
    var workerExitOverdueAfter: TimeInterval = 5
    var pacing = LiveViewerPositionPacer.Settings()
}

/// Decides when the viewer's position reaches the worker.
///
/// The player reports its playhead. When no report has arrived for `reportTimeout` (a client that
/// never reports, such as the developer probe), the start of the most recently requested segment
/// stands in for it. A position is forwarded only when it moved at least `minimumChange` since the
/// last one sent, and at most once per `minimumInterval`.
struct LiveViewerPositionPacer: Sendable {
    struct Settings: Sendable {
        var reportTimeout: TimeInterval = 10
        var minimumChangeMilliseconds: Int64 = 500
        var minimumInterval: TimeInterval = 1
    }

    let settings: Settings
    private let startedAt: Date
    private var lastReport: (milliseconds: Int64, at: Date)?
    private var lastRequestedSegmentStartMilliseconds: Int64?
    private var lastSent: (milliseconds: Int64, at: Date)?

    init(settings: Settings = Settings(), startedAt: Date) {
        self.settings = settings
        self.startedAt = startedAt
    }

    var lastSentMilliseconds: Int64? { lastSent?.milliseconds }

    mutating func report(_ milliseconds: Int64, at date: Date) {
        lastReport = (milliseconds, date)
    }

    mutating func requestedSegment(startingAt milliseconds: Int64) {
        lastRequestedSegmentStartMilliseconds = milliseconds
    }

    func effectivePosition(at date: Date) -> Int64? {
        if let lastReport, date.timeIntervalSince(lastReport.at) < settings.reportTimeout {
            return lastReport.milliseconds
        }
        let reportedAt = lastReport?.at ?? startedAt
        guard date.timeIntervalSince(reportedAt) >= settings.reportTimeout else { return nil }
        return lastRequestedSegmentStartMilliseconds ?? lastReport?.milliseconds
    }

    /// The clamped position to send now, if one is due. The caller records it with `markSent`.
    func due(at date: Date, window: ClosedRange<Int64>?) -> Int64? {
        guard var position = effectivePosition(at: date) else { return nil }
        if let window {
            position = min(max(position, window.lowerBound), window.upperBound)
        }
        guard let lastSent else { return position }
        guard abs(position - lastSent.milliseconds) >= settings.minimumChangeMilliseconds,
              date.timeIntervalSince(lastSent.at) >= settings.minimumInterval
        else { return nil }
        return position
    }

    mutating func markSent(_ milliseconds: Int64, at date: Date) {
        lastSent = (milliseconds, date)
    }
}

/// Keeps the Mac from idle sleep while a stream can serve a viewer.
struct LiveRelaySleepAssertion: Sendable {
    var begin: @Sendable (_ reason: String) -> NSObjectProtocol
    var end: @Sendable (NSObjectProtocol) -> Void

    /// `idleSystemSleepDisabled` is the PreventUserIdleSystemSleep power assertion.
    static let system = LiveRelaySleepAssertion(
        begin: { reason in
            ProcessInfo.processInfo.beginActivity(options: [.userInitiated, .idleSystemSleepDisabled], reason: reason)
        },
        end: { ProcessInfo.processInfo.endActivity($0) }
    )
}

/// One live stream: a `start_live_hls` worker writing a private workspace, which this actor
/// follows and serves through the relay.
///
/// Lifecycle: `starting` until the first valid manifest, then `live`, then `complete` when the
/// worker finishes. A worker failure or a manifest violation makes it `failed` with the reason,
/// served as a 503 `stream_failed`, and starts cleanup at once. `stop` (including idle expiry and a
/// changed source) makes it `stopped`, served as a 410 `stream_ended`. One tracked cleanup cancels
/// the worker, waits for its confirmed exit, removes the workspace, and releases the source lease,
/// and every caller awaits that same cleanup.
actor LiveRelayStream: LiveRelayStreaming {
    nonisolated let id: UUID
    let workspaceURL: URL

    private let job: WorkerJobSpec
    private let runner: any WorkerProcessRunning & Sendable
    private let configuration: LiveRelayStreamConfiguration
    private let now: @Sendable () -> Date
    private let sleepAssertion: LiveRelaySleepAssertion
    private let sourcePath: String
    private let sourceIdentity: LiveSourceIdentity
    private var lease: LiveSourceLease?
    private var workspace: LiveHLSWorkspace
    private var pacer: LiveViewerPositionPacer
    private var currentState: LiveRelayStreamState = .starting
    private var startedAt: Date
    private var lastRenewal: Date?
    private var lastSourceCheck: Date
    private var workerFinished = false
    private var sendingPosition = false
    private var sleepToken: NSObjectProtocol?
    private var runTask: Task<Void, Never>?
    private var monitorTask: Task<Void, Never>?
    private var cleanupTask: Task<Void, Never>?
    private(set) var sentPositions: [Int64] = []
    /// The worker has not exited although its process group was killed; the stream still drains.
    private(set) var isWorkerExitOverdue = false
    /// The worker has exited, the workspace is gone, and the source lease is released.
    private(set) var isCleanupFinished = false

    init(
        id: UUID,
        request: LiveStreamRequest,
        workspaceURL: URL,
        runner: any WorkerProcessRunning & Sendable,
        configuration: LiveRelayStreamConfiguration = LiveRelayStreamConfiguration(),
        now: @escaping @Sendable () -> Date = { Date() },
        sleepAssertion: LiveRelaySleepAssertion = .system
    ) {
        self.id = id
        self.workspaceURL = workspaceURL.standardizedFileURL
        self.runner = runner
        self.configuration = configuration
        self.now = now
        self.sleepAssertion = sleepAssertion
        lease = request.source
        sourcePath = request.source.canonicalPath
        sourceIdentity = request.source.identity
        workspace = LiveHLSWorkspace(rootURL: workspaceURL, maximumMediaBytes: configuration.maximumMediaBytes)
        let started = now()
        startedAt = started
        lastSourceCheck = started
        pacer = LiveViewerPositionPacer(settings: configuration.pacing, startedAt: started)
        var isDirectory = false
        var info = stat()
        if lstat(request.source.canonicalPath, &info) == 0 {
            isDirectory = info.st_mode & S_IFMT == S_IFDIR
        }
        job = WorkerJobSpec(
            liveHLSSourcePath: request.source.canonicalPath,
            isBluRayFolder: isDirectory,
            identity: request.source.identity,
            workspaceURL: workspaceURL,
            playlist: request.playlist,
            audioPID: request.audioPID,
            retentionSeconds: configuration.retentionSeconds,
            retentionMaxBytes: configuration.retentionMaxBytes,
            viewerLeadSeconds: configuration.viewerLeadSeconds,
            maximumPairs: configuration.maximumPairs
        )
    }

    var jobID: UUID { job.jobID }

    /// Starts the worker and the follower. Called once by the starter.
    func launch() {
        guard runTask == nil, cleanupTask == nil else { return }
        startedAt = now()
        lastSourceCheck = startedAt
        sleepToken = sleepAssertion.begin("Serving a live Blu-ray stream to Vision Pro")
        let job = job
        let runner = runner
        runTask = Task { [weak self] in
            let outcome: Result<WorkerRunResult, any Error>
            do {
                outcome = .success(try await runner.run(job: job) { event in
                    await self?.receive(event)
                })
            } catch {
                outcome = .failure(error)
            }
            await self?.workerExited(outcome)
        }
        monitorTask = Task { [weak self, configuration] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(configuration.pollInterval))
                guard let self, await self.tick() else { return }
            }
        }
    }

    func state() -> LiveRelayStreamState {
        currentState
    }

    /// The manifest's latest producer stats, for logging.
    func producerStats() -> LiveHLSManifest.Producer? {
        workspace.window.producer
    }

    func renewLease() {
        lastRenewal = now()
    }

    func relayResponse(for request: RelayHTTPRequest) async -> RelayHTTPResponse {
        switch currentState {
        case let .stopped(reason):
            return .liveStreamEnded(RelayLiveStreamEnded(error: .streamEnded, code: reason.rawValue, message: nil))
        case let .failed(code, message):
            return .liveStreamEnded(RelayLiveStreamEnded(error: .streamFailed, code: code, message: message))
        case .starting, .live, .complete:
            break
        }
        switch request.requestTarget {
        case RelayWireContract.playlistPath:
            guard request.method == "GET" else { return .empty(statusCode: 405) }
            guard let rendered = workspace.window.rendered else { return Self.notYetAvailable }
            return .text(rendered.playlist, contentType: "application/vnd.apple.mpegurl")
        case RelayWireContract.playlistSnapshotPath:
            guard request.method == "GET" else { return .empty(statusCode: 405) }
            guard let rendered = workspace.window.rendered else { return Self.notYetAvailable }
            return .json(rendered.snapshot)
        case RelayWireContract.positionPath:
            guard request.method == "POST" else { return .empty(statusCode: 405) }
            guard request.body.count <= 256,
                  let position = try? JSONDecoder().decode(RelayViewerPosition.self, from: request.body),
                  position.positionMilliseconds >= 0
            else { return .empty(statusCode: 400) }
            pacer.report(position.positionMilliseconds, at: now())
            await forwardPositionIfDue()
            return .empty(statusCode: 204)
        case RelayWireContract.cancelPath:
            guard request.method == "POST", request.body.isEmpty else { return .empty(statusCode: 405) }
            beginStop(.viewerStopped)
            return .empty(statusCode: 204)
        default:
            guard request.requestTarget.hasPrefix(RelayWireContract.mediaPathPrefix) else {
                return .empty(statusCode: 404)
            }
            guard request.method == "GET" else { return .empty(statusCode: 405) }
            return await media(named: String(request.requestTarget.dropFirst(RelayWireContract.mediaPathPrefix.count)))
        }
    }

    func stop(_ reason: LiveRelayStopReason) async {
        beginStop(reason)
        await cleanupTask?.value
    }

    // MARK: - Serving

    private static let notYetAvailable = RelayHTTPResponse(statusCode: 503, headers: ["retry-after": "1"])

    private func media(named name: String) async -> RelayHTTPResponse {
        let isInitialization = name == LiveHLSManifest.initialization
        guard isInitialization || LiveHLSManifest.isSegmentName(name) else { return .empty(statusCode: 404) }
        let data: Data
        do {
            data = try workspace.media(named: name)
        } catch LiveHLSWorkspace.MediaError.tooLarge {
            return .empty(statusCode: 413)
        } catch {
            return .empty(statusCode: 404)
        }
        if let timing = workspace.window.timing(for: name) {
            pacer.requestedSegment(startingAt: LiveHLSPlaylistRenderer.milliseconds(
                boundaryMicroseconds: timing.startMicroseconds
            ))
            await forwardPositionIfDue()
        }
        return RelayHTTPResponse(
            statusCode: 200,
            headers: ["content-type": isInitialization ? "video/mp4" : "video/iso.segment"],
            body: data
        )
    }

    // MARK: - Worker

    private func receive(_ event: WorkerEvent) {
        guard cleanupTask == nil else { return }
        switch event.type {
        case .artifactReady:
            guard let artifact = event.payload.liveHLS else { return }
            guard URL(fileURLWithPath: artifact.manifestPath).standardizedFileURL == workspace.manifestURL else {
                fail(code: "live_hls_artifact_invalid", message: "The worker published outside its workspace.")
                return
            }
            follow()
        case .jobCompleted:
            workerFinished = true
            follow()
        case .jobFailed:
            let failure = event.payload.error
            fail(
                code: failure?.code ?? "live_hls_failed",
                message: failure?.message ?? "The live stream stopped on the Mac."
            )
        default:
            break
        }
    }

    private func workerExited(_ outcome: Result<WorkerRunResult, any Error>) {
        guard cleanupTask == nil else { return }
        switch outcome {
        case let .success(result):
            switch result.terminalEvent.type {
            case .jobCompleted:
                workerFinished = true
                follow()
                if case .complete = currentState { return }
                fail(code: "live_hls_incomplete", message: "The live stream ended without a complete playlist.")
            case .jobFailed:
                let failure = result.terminalEvent.payload.error
                fail(
                    code: failure?.code ?? "live_hls_failed",
                    message: failure?.message ?? "The live stream stopped on the Mac."
                )
            default:
                fail(code: "live_hls_worker_stopped", message: "The live stream's worker stopped unexpectedly.")
            }
        case let .failure(error):
            fail(code: "live_hls_worker_failed", message: error.localizedDescription)
        }
    }

    private func follow() {
        guard cleanupTask == nil else { return }
        switch currentState {
        case .failed, .stopped:
            return
        case .starting, .live, .complete:
            break
        }
        do {
            guard try workspace.poll() else { return }
        } catch let error as LiveHLSManifestError {
            fail(code: LiveHLSManifestError.code, message: "The live stream's manifest was invalid (\(error.reason)).")
            return
        } catch {
            fail(code: LiveHLSManifestError.code, message: "The live stream's manifest could not be read.")
            return
        }
        let window = workspace.window
        guard let earliest = window.earliestMicroseconds, let latest = window.latestAvailableMicroseconds else { return }
        let earliestMilliseconds = LiveHLSPlaylistRenderer.milliseconds(boundaryMicroseconds: earliest)
        let latestMilliseconds = LiveHLSPlaylistRenderer.milliseconds(boundaryMicroseconds: latest)
        currentState = window.complete
            ? .complete(earliestMs: earliestMilliseconds, latestMs: latestMilliseconds)
            : .live(earliestMs: earliestMilliseconds, latestMs: latestMilliseconds)
    }

    /// Fails the stream and starts the tracked cleanup at once. The failed state and its signed
    /// 503 body remain until a stop replaces them with `stopped`.
    private func fail(code: String, message: String) {
        guard cleanupTask == nil else { return }
        switch currentState {
        case .failed, .stopped:
            return
        case .starting, .live, .complete:
            break
        }
        currentState = .failed(code: code, message: message)
        endSleepAssertion()
        startCleanup()
    }

    /// One monitor step: follow the workspace, forward the position, and enforce the idle lease
    /// and the source recheck. Returns false once the stream has stopped.
    private func tick() async -> Bool {
        guard cleanupTask == nil else { return false }
        let date = now()
        let leaseDeadline = lastRenewal.map { $0.addingTimeInterval(configuration.idleTimeout) }
            ?? startedAt.addingTimeInterval(configuration.initialLeaseTimeout)
        if date > leaseDeadline {
            beginStop(.idle)
            return false
        }
        if date.timeIntervalSince(lastSourceCheck) >= configuration.sourceRecheckInterval {
            lastSourceCheck = date
            if !sourceStillLeased() {
                beginStop(.sourceChanged)
                return false
            }
        }
        if !workerFinished {
            follow()
        }
        await forwardPositionIfDue()
        return cleanupTask == nil
    }

    private func sourceStillLeased() -> Bool {
        var info = stat()
        guard lstat(sourcePath, &info) == 0 else { return false }
        let observed = LiveSourceIdentity(info)
        return observed.device == sourceIdentity.device && observed.inode == sourceIdentity.inode
    }

    private func forwardPositionIfDue() async {
        guard !sendingPosition, !workerFinished, cleanupTask == nil else { return }
        switch currentState {
        case .live, .complete:
            break
        case .starting, .failed, .stopped:
            return
        }
        let window = workspace.window
        let bounds = window.earliestMicroseconds.flatMap { earliest in
            window.latestAvailableMicroseconds.map {
                LiveHLSPlaylistRenderer.milliseconds(boundaryMicroseconds: earliest)
                    ... LiveHLSPlaylistRenderer.milliseconds(boundaryMicroseconds: $0)
            }
        }
        let date = now()
        guard let position = pacer.due(at: date, window: bounds) else { return }
        pacer.markSent(position, at: date)
        sentPositions.append(position)
        sendingPosition = true
        defer { sendingPosition = false }
        // A lost position only delays pacing; the next change sends a fresh one.
        try? await runner.sendViewerPosition(WorkerViewerPositionCommand(
            jobID: job.jobID,
            positionMicroseconds: position * 1_000
        ))
    }

    // MARK: - Stop

    /// Invalidates synchronously, then joins the one tracked cleanup. A failed stream becomes
    /// stopped with this reason; its cleanup may already be running.
    private func beginStop(_ reason: LiveRelayStopReason) {
        if case .stopped = currentState {} else {
            currentState = .stopped(reason)
        }
        startCleanup()
    }

    /// The one cleanup every stop and failure shares: cancel the worker, wait for its run to
    /// return, then remove the workspace and release the source lease.
    ///
    /// The run returns only once the worker process has exited; its client escalates to killing
    /// the process group `workerExitGrace` after the cancel. There is no timeout: until the exit
    /// is confirmed nothing is released, so an owner draining this stream never admits a
    /// replacement while the old worker may still read the source or write the workspace. If the
    /// exit is still unconfirmed `workerExitOverdueAfter` past the grace, `isWorkerExitOverdue`
    /// reports the stuck drain.
    private func startCleanup() {
        guard cleanupTask == nil else { return }
        monitorTask?.cancel()
        let runTask = runTask
        let runner = runner
        let overdueAfter = configuration.workerExitGrace + configuration.workerExitOverdueAfter
        let workspaceURL = workspaceURL
        cleanupTask = Task { [weak self] in
            if let runTask {
                runner.cancel()
                let watchdog = Task { [weak self] in
                    try? await Task.sleep(for: .seconds(overdueAfter))
                    guard !Task.isCancelled else { return }
                    await self?.markWorkerExitOverdue()
                }
                await runTask.value
                watchdog.cancel()
            }
            try? FileManager.default.removeItem(at: workspaceURL)
            await self?.finishCleanup()
        }
    }

    private func markWorkerExitOverdue() {
        isWorkerExitOverdue = true
    }

    private func finishCleanup() {
        endSleepAssertion()
        lease = nil
        isWorkerExitOverdue = false
        isCleanupFinished = true
    }

    private func endSleepAssertion() {
        if let sleepToken {
            sleepAssertion.end(sleepToken)
            self.sleepToken = nil
        }
    }

    var isLeaseReleased: Bool { lease == nil }
}

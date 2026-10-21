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
/// worker finishes. A worker failure, a manifest violation, or a changed source makes it `failed`
/// with the reason, served as a 503 `stream_failed`. `stop` makes it `stopped`, served as a 410
/// `stream_ended`; one tracked cleanup cancels the worker, waits for it to exit, removes the
/// workspace, and releases the source lease, and every caller awaits that same cleanup.
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
    private var stopTask: Task<Void, Never>?
    private(set) var sentPositions: [Int64] = []

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
        guard runTask == nil, stopTask == nil else { return }
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
        await stopTask?.value
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
        guard stopTask == nil else { return }
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
        guard stopTask == nil else { return }
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
        guard stopTask == nil else { return }
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

    private func fail(code: String, message: String) {
        guard stopTask == nil else { return }
        switch currentState {
        case .failed, .stopped:
            return
        case .starting, .live, .complete:
            break
        }
        currentState = .failed(code: code, message: message)
        if !workerFinished {
            runner.cancel()
        }
        endSleepAssertion()
    }

    /// One monitor step: follow the workspace, forward the position, and enforce the idle lease
    /// and the source recheck. Returns false once the stream has stopped.
    private func tick() async -> Bool {
        guard stopTask == nil else { return false }
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
        return stopTask == nil
    }

    private func sourceStillLeased() -> Bool {
        var info = stat()
        guard lstat(sourcePath, &info) == 0 else { return false }
        let observed = LiveSourceIdentity(info)
        return observed.device == sourceIdentity.device && observed.inode == sourceIdentity.inode
    }

    private func forwardPositionIfDue() async {
        guard !sendingPosition, !workerFinished, stopTask == nil else { return }
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

    /// Invalidates synchronously, then starts the one tracked cleanup.
    private func beginStop(_ reason: LiveRelayStopReason) {
        guard stopTask == nil else { return }
        if case .stopped = currentState {} else {
            currentState = .stopped(reason)
        }
        monitorTask?.cancel()
        let runTask = runTask
        let runner = runner
        let grace = configuration.workerExitGrace
        let workspaceURL = workspaceURL
        stopTask = Task { [weak self] in
            if let runTask {
                runner.cancel()
                await Self.wait(for: runTask, upTo: grace + 5)
            }
            try? FileManager.default.removeItem(at: workspaceURL)
            await self?.finishStop()
        }
    }

    private func finishStop() {
        endSleepAssertion()
        lease = nil
    }

    private func endSleepAssertion() {
        if let sleepToken {
            sleepAssertion.end(sleepToken)
            self.sleepToken = nil
        }
    }

    /// Waits for the worker run to return. The client kills the process group after its own
    /// escalation delay; the extra bound keeps a wedged exit from holding app quit forever.
    private static func wait(for task: Task<Void, Never>, upTo seconds: TimeInterval) async {
        let gate = OnceGate()
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            gate.install(continuation)
            Task {
                await task.value
                gate.open()
            }
            Task {
                try? await Task.sleep(for: .seconds(seconds))
                gate.open()
            }
        }
    }

    var isLeaseReleased: Bool { lease == nil }
}

/// Resumes one continuation exactly once, whichever caller opens it first.
private final class OnceGate: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Void, Never>?
    private var isOpen = false

    func install(_ continuation: CheckedContinuation<Void, Never>) {
        let resumeNow = lock.withLock {
            if isOpen { return true }
            self.continuation = continuation
            return false
        }
        if resumeNow { continuation.resume() }
    }

    func open() {
        let pending = lock.withLock {
            isOpen = true
            let pending = continuation
            continuation = nil
            return pending
        }
        pending?.resume()
    }
}

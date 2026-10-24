import Darwin
import Foundation
import XCTest
@testable import BluRayToVisionPro

/// A worker that writes a live workspace like `start_live_hls` and then follows a script.
final class FakeLiveHLSWorker: WorkerProcessRunning, @unchecked Sendable {
    enum Script {
        /// Publishes segments 0...2, then runs until cancelled.
        case live
        /// Publishes a complete window and finishes.
        case complete
        /// Fails with the worker's own error before publishing.
        case fail(code: String, message: String)
        /// Publishes, then writes a manifest that violates the contract, then runs until cancelled.
        case invalidManifest
        /// Never publishes, like a worker stuck opening its source, until cancelled.
        case silent
    }

    private let lock = NSLock()
    private let script: Script
    private let supportsPositions: Bool
    private let exitDelay: TimeInterval
    private var cancelWaiters: [CheckedContinuation<Void, Never>] = []
    private var cancelled = false
    private var _jobs: [WorkerJobSpec] = []
    private var _positions: [WorkerViewerPositionCommand] = []
    private var _exited = false

    init(script: Script = .live, supportsPositions: Bool = true, exitDelay: TimeInterval = 0) {
        self.script = script
        self.supportsPositions = supportsPositions
        self.exitDelay = exitDelay
    }

    var jobs: [WorkerJobSpec] { lock.withLock { _jobs } }
    var positions: [WorkerViewerPositionCommand] { lock.withLock { _positions } }
    var isCancelled: Bool { lock.withLock { cancelled } }
    var hasExited: Bool { lock.withLock { _exited } }

    func run(job: WorkerJobSpec, onEvent: @escaping (WorkerEvent) async throws -> Void) async throws -> WorkerRunResult {
        lock.withLock { _jobs.append(job) }
        defer { lock.withLock { _exited = true } }
        let workspace = URL(fileURLWithPath: try XCTUnwrap(job.destination?.path))
        var sequence = 0
        func emit(_ type: WorkerEventType, _ payload: WorkerEventPayload = WorkerEventPayload()) async throws -> WorkerEvent {
            let event = WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: type,
                jobID: job.jobID,
                sequence: sequence,
                payload: payload
            )
            sequence += 1
            try await onEvent(event)
            return event
        }
        let ready = WorkerEventPayload(liveHLS: LiveHLSArtifact(
            kind: "live_hls_manifest",
            manifestPath: workspace.appendingPathComponent("live-hls.json").path,
            playlistPath: workspace.appendingPathComponent("media.m3u8").path
        ))
        // Like the worker, carry each requested alternate track as an audio rendition.
        let alternateAudioPIDs = job.liveHLS?.alternateAudioPIDs ?? []
        _ = try await emit(.workerReady)
        switch script {
        case .silent:
            await waitUntilCancelled()
            let cancelledEvent = try await emit(.jobCancelled)
            return WorkerRunResult(terminalEvent: cancelledEvent, exitStatus: 0, diagnostics: "")
        case let .fail(code, message):
            try FileManager.default.createDirectory(at: workspace, withIntermediateDirectories: true)
            try Data("partial".utf8).write(to: workspace.appendingPathComponent("init.mp4"))
            let failed = try await emit(.jobFailed, WorkerEventPayload(
                error: WorkerFailure(code: code, message: message, details: nil, retryable: false)
            ))
            return WorkerRunResult(terminalEvent: failed, exitStatus: 1, diagnostics: "")
        case .complete:
            let writer = try LiveHLSWorkspaceWriter(root: workspace)
            try writer.publish(LiveHLSWorkspaceWriter.segments(0 ... 2), complete: true, audioPIDs: alternateAudioPIDs)
            _ = try await emit(.artifactReady, ready)
            let completed = try await emit(.jobCompleted)
            return WorkerRunResult(terminalEvent: completed, exitStatus: 0, diagnostics: "")
        case .live, .invalidManifest:
            let writer = try LiveHLSWorkspaceWriter(root: workspace)
            try writer.publish(LiveHLSWorkspaceWriter.segments(0 ... 2), audioPIDs: alternateAudioPIDs)
            _ = try await emit(.artifactReady, ready)
            if case .invalidManifest = script {
                try await Task.sleep(for: .milliseconds(50))
                try writer.publish(LiveHLSWorkspaceWriter.segments(0 ... 2), overrides: ["media_sequence": 9])
            }
            await waitUntilCancelled()
            if exitDelay > 0 {
                // Like a worker blocked in source I/O: it ignores the cancel until the delay ends.
                try? await Task.sleep(for: .seconds(exitDelay))
            }
            try? FileManager.default.removeItem(at: workspace)
            let cancelledEvent = try await emit(.jobCancelled)
            return WorkerRunResult(terminalEvent: cancelledEvent, exitStatus: 0, diagnostics: "")
        }
    }

    func cancel() {
        let waiters = lock.withLock {
            cancelled = true
            defer { cancelWaiters.removeAll() }
            return cancelWaiters
        }
        waiters.forEach { $0.resume() }
    }

    func sendControl(_ command: WorkerWaitCommand) async throws {
        throw WorkerControlError.unavailable
    }

    func sendViewerPosition(_ command: WorkerViewerPositionCommand) async throws {
        guard supportsPositions else { throw WorkerControlError.unavailable }
        lock.withLock { _positions.append(command) }
    }

    private func waitUntilCancelled() async {
        await withCheckedContinuation { continuation in
            let resumeNow = lock.withLock {
                if cancelled { return true }
                cancelWaiters.append(continuation)
                return false
            }
            if resumeNow { continuation.resume() }
        }
    }
}

final class LockedBox: @unchecked Sendable {
    private let lock = NSLock()
    private var isSet = false
    var value: Bool { lock.withLock { isSet } }
    func set() { lock.withLock { isSet = true } }
}

final class SleepAssertionCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var active = 0
    var activeCount: Int { lock.withLock { active } }

    var assertion: LiveRelaySleepAssertion {
        LiveRelaySleepAssertion(
            begin: { _ in self.lock.withLock { self.active += 1 }; return NSObject() },
            end: { _ in self.lock.withLock { self.active -= 1 } }
        )
    }
}

final class LiveRelayStreamTests: XCTestCase {
    private var root: URL!
    private var sourceURL: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("LiveRelayStreamTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        sourceURL = root.appendingPathComponent("Rainforest.iso")
        try Data("disc".utf8).write(to: sourceURL)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    private func sourceRequest(audioTracks: [SharedLiveAudio] = []) throws -> LiveStreamRequest {
        final class Retainer: Sendable {}
        var info = stat()
        XCTAssertEqual(lstat(sourceURL.path, &info), 0)
        return LiveStreamRequest(
            source: LiveSourceLease(canonicalPath: sourceURL.path, identity: LiveSourceIdentity(info), retainer: Retainer()),
            playlist: 1_005,
            audioPID: 4_353,
            audioTracks: audioTracks
        )
    }

    private func fastConfiguration() -> LiveRelayStreamConfiguration {
        var configuration = LiveRelayStreamConfiguration()
        configuration.pollInterval = 0.02
        configuration.workerExitGrace = 2
        return configuration
    }

    private func makeStream(
        _ worker: FakeLiveHLSWorker,
        configuration: LiveRelayStreamConfiguration? = nil,
        sleep: SleepAssertionCounter = SleepAssertionCounter(),
        audioTracks: [SharedLiveAudio] = []
    ) async throws -> LiveRelayStream {
        let stream = LiveRelayStream(
            id: UUID(),
            request: try sourceRequest(audioTracks: audioTracks),
            workspaceURL: root.appendingPathComponent("LiveHLS/stream", isDirectory: true),
            runner: worker,
            configuration: configuration ?? fastConfiguration(),
            sleepAssertion: sleep.assertion
        )
        await stream.launch()
        return stream
    }

    private func waitForState(
        _ stream: LiveRelayStream,
        timeout: TimeInterval = 5,
        _ matches: (LiveRelayStreamState) -> Bool
    ) async -> LiveRelayStreamState {
        let deadline = Date().addingTimeInterval(timeout)
        var state = await stream.state()
        while !matches(state), Date() < deadline {
            try? await Task.sleep(for: .milliseconds(10))
            state = await stream.state()
        }
        return state
    }

    private func get(_ target: String) -> RelayHTTPRequest {
        RelayHTTPRequest(method: "GET", requestTarget: target, headers: [:], body: Data())
    }

    private func post(_ target: String, body: Data = Data()) -> RelayHTTPRequest {
        RelayHTTPRequest(method: "POST", requestTarget: target, headers: [:], body: body)
    }

    private func isLive(_ state: LiveRelayStreamState) -> Bool {
        if case .live = state { return true }
        return false
    }

    func testReadyWorkspaceBecomesLiveAndIsServed() async throws {
        let worker = FakeLiveHLSWorker()
        let sleep = SleepAssertionCounter()
        let stream = try await makeStream(worker, sleep: sleep)

        let state = await waitForState(stream, isLive)
        XCTAssertEqual(state, .live(earliestMs: 0, latestMs: 6_000))
        XCTAssertEqual(sleep.activeCount, 1)

        let playlist = await stream.relayResponse(for: get(RelayWireContract.playlistPath))
        XCTAssertEqual(playlist.statusCode, 200)
        XCTAssertEqual(playlist.headers["content-type"], "application/vnd.apple.mpegurl")
        let multivariant = String(decoding: playlist.body, as: UTF8.self)
        XCTAssertEqual(multivariant.components(separatedBy: "#EXT-X-MEDIA:TYPE=AUDIO").count, 2, "one track shows as one choice")
        XCTAssertFalse(multivariant.contains("audio-"), "a single-track title has no alternate rendition")
        let video = await stream.relayResponse(for: get(RelayWireContract.mediaPathPrefix + "video.m3u8"))
        XCTAssertEqual(video.headers["content-type"], "application/vnd.apple.mpegurl")
        XCTAssertTrue(String(decoding: video.body, as: UTF8.self).contains("/relay/v1/media/segment-000000002.m4s"))
        let snapshot = await stream.relayResponse(for: get(RelayWireContract.playlistSnapshotPath))
        XCTAssertEqual(try JSONDecoder().decode(RelayPlaylistSnapshot.self, from: snapshot.body).totalDurationMilliseconds, 6_000)
        let media = await stream.relayResponse(for: get(RelayWireContract.mediaPathPrefix + "segment-000000001.m4s"))
        XCTAssertEqual(media.statusCode, 200)
        XCTAssertEqual(media.body, Data("media 1".utf8))
        let missing = await stream.relayResponse(for: get(RelayWireContract.mediaPathPrefix + "segment-000000009.m4s"))
        XCTAssertEqual(missing.statusCode, 404)
        let traversal = await stream.relayResponse(for: get(RelayWireContract.mediaPathPrefix + "../live-hls.json"))
        XCTAssertEqual(traversal.statusCode, 404)

        let job = try XCTUnwrap(worker.jobs.first)
        XCTAssertEqual(job.operation, "start_live_hls")
        XCTAssertEqual(job.source.path, sourceURL.path)
        XCTAssertEqual(job.liveHLS?.viewerLeadSeconds, LiveRelayStreamConfiguration().viewerLeadSeconds)
        XCTAssertEqual(job.source.identity?.inode, try sourceRequest().source.identity.inode)
        await stream.stop(.viewerStopped)
    }

    func testEveryTrackIsAnAudioChoiceAndTheSelectedOneIsTheMuxedDefault() async throws {
        let worker = FakeLiveHLSWorker()
        let stream = try await makeStream(worker, audioTracks: [
            SharedLiveAudio(pid: 4_352, language: "deu", label: "German DTS-HD Master Audio", isDefault: true),
            SharedLiveAudio(pid: 4_353, language: "eng", label: "English DTS-HD Master Audio", isDefault: false),
        ])
        _ = await waitForState(stream, isLive)
        XCTAssertEqual(worker.jobs.first?.liveHLS?.alternateAudioPIDs, [4_352], "the selected PID 4353 plays muxed")

        let playlist = await stream.relayResponse(for: get(RelayWireContract.playlistPath))
        let media = String(decoding: playlist.body, as: UTF8.self).components(separatedBy: "\n")
            .filter { $0.hasPrefix("#EXT-X-MEDIA:") }
        XCTAssertEqual(media.count, 2)
        XCTAssertTrue(media[0].contains("NAME=\"English DTS-HD Master Audio\",LANGUAGE=\"en\",DEFAULT=YES"))
        XCTAssertFalse(media[0].contains("URI="))
        XCTAssertTrue(media[1].contains("NAME=\"German DTS-HD Master Audio\",LANGUAGE=\"de\",DEFAULT=NO"))
        XCTAssertTrue(media[1].hasSuffix("URI=\"/relay/v1/media/audio-4352.m3u8\""))

        let audio = await stream.relayResponse(for: get(RelayWireContract.mediaPathPrefix + "audio-4352.m3u8"))
        XCTAssertEqual(audio.statusCode, 200)
        XCTAssertTrue(String(decoding: audio.body, as: UTF8.self).contains("/relay/v1/media/audio-4352-segment-000000002.m4s"))
        let segment = await stream.relayResponse(for: get(RelayWireContract.mediaPathPrefix + "audio-4352-segment-000000001.m4s"))
        XCTAssertEqual(segment.statusCode, 200)
        XCTAssertEqual(segment.headers["content-type"], "audio/mp4")
        XCTAssertEqual(segment.body, Data("audio 4352 1".utf8))
        let other = await stream.relayResponse(for: get(RelayWireContract.mediaPathPrefix + "audio-4353.m3u8"))
        XCTAssertEqual(other.statusCode, 404, "the muxed track has no playlist of its own")
        await stream.stop(.viewerStopped)
    }

    func testWorkerFailureIsASigned503WithTheWorkersReason() async throws {
        let worker = FakeLiveHLSWorker(script: .fail(code: "unsupported_live_audio", message: "LPCM is not supported."))
        let sleep = SleepAssertionCounter()
        let stream = try await makeStream(worker, sleep: sleep)

        let state = await waitForState(stream) { if case .failed = $0 { return true }; return false }
        XCTAssertEqual(state, .failed(code: "unsupported_live_audio", message: "LPCM is not supported."))
        let response = await stream.relayResponse(for: get(RelayWireContract.playlistPath))
        XCTAssertEqual(response.statusCode, 503)
        XCTAssertEqual(
            try JSONDecoder().decode(RelayLiveStreamEnded.self, from: response.body),
            RelayLiveStreamEnded(error: .streamFailed, code: "unsupported_live_audio", message: "LPCM is not supported.")
        )
        XCTAssertEqual(sleep.activeCount, 0, "a failed stream does not keep the Mac awake")
        try await assertCleanedUpWithoutStop(stream)
        let afterCleanup = await stream.relayResponse(for: get(RelayWireContract.playlistSnapshotPath))
        XCTAssertEqual(afterCleanup.statusCode, 503, "the failure stays visible after cleanup")
        XCTAssertEqual(
            try JSONDecoder().decode(RelayLiveStreamEnded.self, from: afterCleanup.body).code,
            "unsupported_live_audio"
        )
    }

    private func assertCleanedUpWithoutStop(_ stream: LiveRelayStream) async throws {
        let deadline = Date().addingTimeInterval(5)
        while await !stream.isCleanupFinished, Date() < deadline {
            try await Task.sleep(for: .milliseconds(10))
        }
        let finished = await stream.isCleanupFinished
        let leaseReleased = await stream.isLeaseReleased
        let workspace = await stream.workspaceURL
        XCTAssertTrue(finished, "a failure starts the tracked cleanup by itself")
        XCTAssertTrue(leaseReleased)
        XCTAssertFalse(FileManager.default.fileExists(atPath: workspace.path))
        let state = await stream.state()
        guard case .failed = state else { return XCTFail("cleanup replaced the failure: \(state)") }
    }

    func testInvalidManifestFailsTheStreamAndStopsTheWorker() async throws {
        let worker = FakeLiveHLSWorker(script: .invalidManifest)
        let stream = try await makeStream(worker)

        let state = await waitForState(stream) { if case .failed = $0 { return true }; return false }
        guard case let .failed(code, _) = state else { return XCTFail("\(state)") }
        XCTAssertEqual(code, LiveHLSManifestError.code)
        try await assertCleanedUpWithoutStop(stream)
        XCTAssertTrue(worker.isCancelled)
        XCTAssertTrue(worker.hasExited)
    }

    func testStopReleasesNothingUntilAWorkerThatIgnoresTheCancelHasExited() async throws {
        var configuration = fastConfiguration()
        configuration.workerExitGrace = 0.1
        configuration.workerExitOverdueAfter = 0.5
        // Longer than any fixed bound the cleanup could have applied after the grace.
        let worker = FakeLiveHLSWorker(exitDelay: 6.5)
        let stream = try await makeStream(worker, configuration: configuration)
        _ = await waitForState(stream, isLive)
        let workspace = await stream.workspaceURL

        let stopReturned = LockedBox()
        let stop = Task {
            await stream.stop(.replaced)
            stopReturned.set()
        }
        try await Task.sleep(for: .seconds(6))
        XCTAssertFalse(stopReturned.value, "stop returned before the worker exited")
        XCTAssertFalse(worker.hasExited)
        XCTAssertTrue(FileManager.default.fileExists(atPath: workspace.path), "workspace removed under a live worker")
        let leaseReleased = await stream.isLeaseReleased
        let finished = await stream.isCleanupFinished
        let overdue = await stream.isWorkerExitOverdue
        XCTAssertFalse(leaseReleased, "source lease released under a live worker")
        XCTAssertFalse(finished)
        XCTAssertTrue(overdue, "the stuck drain is reported")

        await stop.value
        XCTAssertTrue(worker.hasExited)
        let finishedAfter = await stream.isCleanupFinished
        let overdueAfter = await stream.isWorkerExitOverdue
        XCTAssertTrue(finishedAfter)
        XCTAssertFalse(overdueAfter)
        XCTAssertFalse(FileManager.default.fileExists(atPath: workspace.path))
    }

    func testStopAwaitsWorkerExitAndWorkspaceRemovalForEveryCaller() async throws {
        let worker = FakeLiveHLSWorker(exitDelay: 0.3)
        let sleep = SleepAssertionCounter()
        let stream = try await makeStream(worker, sleep: sleep)
        _ = await waitForState(stream, isLive)
        let workspace = await stream.workspaceURL

        async let first: Void = stream.stop(.appQuit)
        async let second: Void = stream.stop(.sessionEnded)
        _ = await (first, second)

        XCTAssertTrue(worker.isCancelled)
        XCTAssertTrue(worker.hasExited)
        XCTAssertFalse(FileManager.default.fileExists(atPath: workspace.path))
        let leaseReleased = await stream.isLeaseReleased
        XCTAssertTrue(leaseReleased)
        XCTAssertEqual(sleep.activeCount, 0)
        let response = await stream.relayResponse(for: get(RelayWireContract.playlistSnapshotPath))
        XCTAssertEqual(response.statusCode, 410)
        // The two stops run concurrently, so either may record the reason; the body must
        // still be a signed stream end carrying one of them.
        let ended = try JSONDecoder().decode(RelayLiveStreamEnded.self, from: response.body)
        XCTAssertEqual(ended.error, .streamEnded)
        XCTAssertNil(ended.message)
        XCTAssertTrue(
            [LiveRelayStopReason.appQuit.rawValue, LiveRelayStopReason.sessionEnded.rawValue].contains(ended.code),
            "unexpected stop reason \(String(describing: ended.code))"
        )
    }

    func testViewerCancelStopsTheStream() async throws {
        let worker = FakeLiveHLSWorker()
        let stream = try await makeStream(worker)
        _ = await waitForState(stream, isLive)

        let cancel = await stream.relayResponse(for: post(RelayWireContract.cancelPath))
        XCTAssertEqual(cancel.statusCode, 204)
        await stream.stop(.viewerStopped)
        let state = await stream.state()
        XCTAssertEqual(state, .stopped(.viewerStopped))
        XCTAssertTrue(worker.hasExited)
    }

    func testCompletedStreamServesItsFinalWindow() async throws {
        let worker = FakeLiveHLSWorker(script: .complete)
        let stream = try await makeStream(worker)

        let state = await waitForState(stream) { if case .complete = $0 { return true }; return false }
        XCTAssertEqual(state, .complete(earliestMs: 0, latestMs: 6_000))
        let playlist = await stream.relayResponse(for: get(RelayWireContract.mediaPathPrefix + "video.m3u8"))
        XCTAssertTrue(String(decoding: playlist.body, as: UTF8.self).hasSuffix("#EXT-X-ENDLIST\n"))
        await stream.stop(.viewerStopped)
    }

    func testAStreamThatNeverGetsReadyFailsWithTheMacsReasonAndStopsItsWorker() async throws {
        var configuration = fastConfiguration()
        configuration.readyTimeout = 0.3
        let worker = FakeLiveHLSWorker(script: .silent)
        let stream = try await makeStream(worker, configuration: configuration)
        let early = await stream.state()
        XCTAssertEqual(early, .starting)

        let state = await waitForState(stream) { if case .failed = $0 { return true }; return false }
        XCTAssertEqual(state, .failed(code: "live_start_timeout", message: "The Mac could not start the live stream in time."))
        let response = await stream.relayResponse(for: get(RelayWireContract.playlistPath))
        XCTAssertEqual(response.statusCode, 503)
        XCTAssertEqual(try JSONDecoder().decode(RelayLiveStreamEnded.self, from: response.body).code, "live_start_timeout")
        await stream.stop(.cancelled)
        XCTAssertTrue(worker.isCancelled)
        XCTAssertTrue(worker.hasExited)
    }

    func testUnrenewedLeaseStopsAsIdle() async throws {
        var configuration = fastConfiguration()
        configuration.initialLeaseTimeout = 0.4
        configuration.idleTimeout = 0.3
        let worker = FakeLiveHLSWorker()
        let stream = try await makeStream(worker, configuration: configuration)
        _ = await waitForState(stream, isLive)

        // Renewals keep it past the initial lease.
        for _ in 0 ..< 6 {
            await stream.renewLease()
            try await Task.sleep(for: .milliseconds(100))
        }
        let renewed = await stream.state()
        XCTAssertTrue(isLive(renewed), "\(renewed)")

        let state = await waitForState(stream) { $0 == .stopped(.idle) }
        XCTAssertEqual(state, .stopped(.idle))
        await stream.stop(.viewerStopped)
        XCTAssertTrue(worker.hasExited)
    }

    func testReplacedSourceStopsTheStream() async throws {
        var configuration = fastConfiguration()
        configuration.sourceRecheckInterval = 0.05
        let worker = FakeLiveHLSWorker()
        let stream = try await makeStream(worker, configuration: configuration)
        _ = await waitForState(stream, isLive)

        let replacement = root.appendingPathComponent("replacement.iso")
        try Data("other".utf8).write(to: replacement)
        _ = try FileManager.default.replaceItemAt(sourceURL, withItemAt: replacement)

        let state = await waitForState(stream) { $0 == .stopped(.sourceChanged) }
        XCTAssertEqual(state, .stopped(.sourceChanged))
        await stream.stop(.viewerStopped)
    }

    func testReportedPositionReachesTheWorkerClampedAndThrottled() async throws {
        var configuration = fastConfiguration()
        configuration.pacing.minimumInterval = 0.3
        let worker = FakeLiveHLSWorker()
        let stream = try await makeStream(worker, configuration: configuration)
        _ = await waitForState(stream, isLive)

        func report(_ milliseconds: Int64) async {
            let body = try! JSONEncoder().encode(RelayViewerPosition(positionMilliseconds: milliseconds))
            let response = await stream.relayResponse(for: post(RelayWireContract.positionPath, body: body))
            XCTAssertEqual(response.statusCode, 204)
        }
        await report(4_000)
        await report(4_200) // under the minimum change
        await report(5_000) // inside the minimum interval
        XCTAssertEqual(worker.positions.map(\.positionMicroseconds), [4_000_000])
        XCTAssertEqual(worker.positions.first?.jobID, worker.jobs.first?.jobID)

        // The monitor forwards the latest value once the interval passes.
        try await Task.sleep(for: .milliseconds(450))
        XCTAssertEqual(worker.positions.map(\.positionMicroseconds), [4_000_000, 5_000_000])

        // A position past the window is clamped to the live edge.
        try await Task.sleep(for: .milliseconds(350))
        await report(60_000)
        XCTAssertEqual(worker.positions.last?.positionMicroseconds, 6_000_000)

        let malformed = await stream.relayResponse(for: post(RelayWireContract.positionPath, body: Data("{}".utf8)))
        XCTAssertEqual(malformed.statusCode, 400)
        await stream.stop(.viewerStopped)
    }

    func testPositionSendFailureIsNotFatal() async throws {
        let worker = FakeLiveHLSWorker(supportsPositions: false)
        let stream = try await makeStream(worker)
        _ = await waitForState(stream, isLive)
        let body = try JSONEncoder().encode(RelayViewerPosition(positionMilliseconds: 1_000))
        let response = await stream.relayResponse(for: post(RelayWireContract.positionPath, body: body))
        XCTAssertEqual(response.statusCode, 204)
        let state = await stream.state()
        XCTAssertTrue(isLive(state))
        await stream.stop(.viewerStopped)
    }

    func testStarterCancelledBeforeItRunsLaunchesNothing() async throws {
        let worker = FakeLiveHLSWorker()
        let workspaceRoot = root.appendingPathComponent("LiveHLS")
        let starter = LiveRelayStreamStarter(workspaceRoot: workspaceRoot, configuration: fastConfiguration()) { _ in worker }
        let request = try sourceRequest()
        let start = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try await starter.start(request, id: UUID())
        }
        do {
            _ = try await start.value
            XCTFail("Expected cancellation")
        } catch {
            XCTAssertTrue(error is CancellationError)
        }
        XCTAssertTrue(worker.jobs.isEmpty)
    }

    func testStarterGivesEachStreamAFreshPrivateWorkspace() async throws {
        let worker = FakeLiveHLSWorker()
        let workspaceRoot = root.appendingPathComponent("LiveHLS")
        let starter = LiveRelayStreamStarter(workspaceRoot: workspaceRoot, configuration: fastConfiguration()) { _ in worker }
        let id = UUID()

        let stream = try await starter.start(try sourceRequest(), id: id)

        XCTAssertEqual(stream.id, id)
        _ = await waitForState(try XCTUnwrap(stream as? LiveRelayStream), isLive)
        let workspace = workspaceRoot.appendingPathComponent(id.uuidString.lowercased())
        XCTAssertEqual(worker.jobs.first?.destination?.path, workspace.path)
        let permissions = try FileManager.default.attributesOfItem(atPath: workspaceRoot.path)[.posixPermissions] as? Int
        XCTAssertEqual(permissions, 0o700)
        let marker = LiveLeftoverOwner.marker(forWorkspace: workspace)
        XCTAssertEqual(LiveLeftoverOwner.read(marker), getpid(), "the workspace is marked as this instance's")
        await XCTAssertThrowsAsyncError(try await starter.start(try sourceRequest(), id: id))
        await stream.stop(.viewerStopped)
        XCTAssertFalse(FileManager.default.fileExists(atPath: workspace.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: marker.path))
    }

    func testTheLaunchSweepRemovesOnlyWhatNoRunningInstanceOwns() throws {
        let root = root.appendingPathComponent("sweep", isDirectory: true)
        let workspaces = root.appendingPathComponent("LiveHLS", isDirectory: true)
        let temporary = root.appendingPathComponent("tmp", isDirectory: true)
        try FileManager.default.createDirectory(at: workspaces, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
        let running: pid_t = 4_242
        let exited: pid_t = 4_343
        func workspace(_ name: String, owner: pid_t?) throws -> URL {
            let url = workspaces.appendingPathComponent(name, isDirectory: true)
            try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
            if let owner { LiveLeftoverOwner.write(LiveLeftoverOwner.marker(forWorkspace: url), pid: owner) }
            return url
        }
        func handoff(_ name: String, owner: pid_t?, age: TimeInterval) throws -> URL {
            let url = temporary.appendingPathComponent(name, isDirectory: true)
            try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
            if let owner { LiveLeftoverOwner.write(url.appendingPathComponent(LiveLeftoverOwner.handoffMarkerName), pid: owner) }
            try FileManager.default.setAttributes([.modificationDate: Date().addingTimeInterval(-age)], ofItemAtPath: url.path)
            return url
        }
        let active = try workspace("active", owner: running)
        let crashed = try workspace("crashed", owner: exited)
        let unmarked = try workspace("unmarked", owner: nil)
        let activeHandoff = try handoff("bdtoavp-Active", owner: running, age: 600)
        let crashedHandoff = try handoff("bdtoavp-Crashd", owner: exited, age: 600)
        let newHandoff = try handoff("bdtoavp-NewOne", owner: nil, age: 1)
        let oldHandoff = try handoff("bdtoavp-OldOne", owner: nil, age: 600)
        let unrelated = try handoff("something-else", owner: nil, age: 600)

        LiveRelayStreamStarter.sweepLeftovers(workspaceRoot: workspaces, temporaryDirectory: temporary, isRunning: { $0 == running })

        let exists = { (url: URL) in FileManager.default.fileExists(atPath: url.path) }
        XCTAssertTrue(exists(active), "a running instance's workspace stays")
        XCTAssertTrue(exists(LiveLeftoverOwner.marker(forWorkspace: active)))
        XCTAssertFalse(exists(crashed))
        XCTAssertFalse(exists(LiveLeftoverOwner.marker(forWorkspace: crashed)))
        XCTAssertFalse(exists(unmarked))
        XCTAssertTrue(exists(activeHandoff), "a running instance's handoff socket stays")
        XCTAssertFalse(exists(crashedHandoff))
        XCTAssertTrue(exists(newHandoff), "an unmarked directory that may be mid-creation stays")
        XCTAssertFalse(exists(oldHandoff))
        XCTAssertTrue(exists(unrelated))
    }

    func testLiveJobSpecAndEventsMatchSharedWorkerFixtures() throws {
        let spec = WorkerJobSpec(
            liveHLSSourcePath: "/Volumes/Movies/Rainforest.iso",
            isBluRayFolder: false,
            identity: LiveSourceIdentity(
                device: 16_777_234,
                inode: 123_456_789,
                sizeBytes: 41_234_567_168,
                modificationSeconds: 1_760_000_000,
                modificationNanoseconds: 123_456_789
            ),
            descriptorHandoff: .init(
                socketPath: "/tmp/bdtoavp-Ab12Cd/s",
                token: "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0"
            ),
            workspaceURL: URL(fileURLWithPath: "/tmp/LiveHLS/66666666-6666-4666-8666-666666666666", isDirectory: true),
            playlist: 1_005,
            audioPID: 4_353,
            retentionSeconds: 120,
            retentionMaxBytes: 268_435_456,
            viewerLeadSeconds: 30,
            jobID: UUID(uuidString: "55555555-5555-4555-8555-555555555555")!
        )
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(spec)) as? NSDictionary
        var fixture = try XCTUnwrap(JSONSerialization.jsonObject(with: fixtureData("native_worker_start_live_hls")) as? [String: Any])
        fixture["job_id"] = (fixture["job_id"] as? String)?.uppercased()
        XCTAssertEqual(encoded, fixture as NSDictionary)

        let ready = try JSONDecoder().decode(WorkerEvent.self, from: fixtureData("native_worker_live_hls_ready"))
        XCTAssertEqual(ready.payload.liveHLS?.manifestPath, "/tmp/LiveHLS/66666666-6666-4666-8666-666666666666/live-hls.json")
        let completed = try JSONDecoder().decode(WorkerEvent.self, from: fixtureData("native_worker_live_hls_completed"))
        let result = try XCTUnwrap(completed.payload.liveHLSResult)
        XCTAssertEqual(result.latestAvailableMicroseconds, 601_601_000)
        XCTAssertEqual(result.viewerHeldSeconds, 0)
    }

    private func fixtureData(_ family: String) throws -> Data {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/\(family)_v\(WorkerJobSpec.protocolVersion).json")
        return try XCTUnwrap(FileManager.default.contents(atPath: url.path))
    }
}

final class LiveViewerPositionPacerTests: XCTestCase {
    private let start = Date(timeIntervalSince1970: 1_000)

    func testReportsAreForwardedOnMeaningfulChangesAtMostOncePerInterval() {
        var pacer = LiveViewerPositionPacer(startedAt: start)
        pacer.report(10_000, at: start)
        XCTAssertEqual(pacer.due(at: start, window: 0 ... 60_000), 10_000)
        pacer.markSent(10_000, at: start)

        pacer.report(10_400, at: start.addingTimeInterval(2))
        XCTAssertNil(pacer.due(at: start.addingTimeInterval(2), window: nil), "under 500 ms of change")
        pacer.report(11_000, at: start.addingTimeInterval(2.5))
        XCTAssertEqual(pacer.due(at: start.addingTimeInterval(2.5), window: nil), 11_000)
        pacer.markSent(11_000, at: start.addingTimeInterval(2.5))
        pacer.report(3_000, at: start.addingTimeInterval(3))
        XCTAssertNil(pacer.due(at: start.addingTimeInterval(3), window: nil), "within one second of the last send")
        XCTAssertEqual(pacer.due(at: start.addingTimeInterval(3.5), window: nil), 3_000, "a backward seek is forwarded")
    }

    func testWithoutReportsTheRequestedSegmentStandsInAfterTheTimeout() {
        var pacer = LiveViewerPositionPacer(startedAt: start)
        pacer.requestedSegment(startingAt: 8_000)
        XCTAssertNil(pacer.due(at: start.addingTimeInterval(9), window: nil))
        XCTAssertEqual(pacer.due(at: start.addingTimeInterval(10), window: nil), 8_000)

        pacer.report(2_000, at: start.addingTimeInterval(11))
        pacer.requestedSegment(startingAt: 30_000)
        XCTAssertEqual(pacer.effectivePosition(at: start.addingTimeInterval(20)), 2_000, "a fresh report wins")
        XCTAssertEqual(pacer.effectivePosition(at: start.addingTimeInterval(21)), 30_000, "a stale report falls back")
    }

    func testPositionsAreClampedToTheWindow() {
        var pacer = LiveViewerPositionPacer(startedAt: start)
        pacer.report(1_000, at: start)
        XCTAssertEqual(pacer.due(at: start, window: 4_000 ... 9_000), 4_000)
        pacer.report(20_000, at: start)
        XCTAssertEqual(pacer.due(at: start, window: 4_000 ... 9_000), 9_000)
    }
}

private func XCTAssertThrowsAsyncError<T>(
    _ expression: @autoclosure () async throws -> T,
    file: StaticString = #filePath,
    line: UInt = #line
) async {
    do {
        _ = try await expression()
        XCTFail("Expected an error", file: file, line: line)
    } catch {}
}

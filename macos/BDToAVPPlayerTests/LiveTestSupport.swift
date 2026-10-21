import Foundation
import XCTest
@testable import BDToAVPPlayer

/// Virtual time for live startup and cleanup. `sleep` advances time at once and then runs any
/// action scheduled at or before the new time, so minutes of retry schedule run instantly.
final class ManualLiveClock: LiveClock, @unchecked Sendable {
    private let lock = NSLock()
    private var current: Date
    private var actions: [(at: Date, run: @Sendable () async -> Void)] = []
    private(set) var sleeps: [TimeInterval] = []

    init(_ start: Date) { current = start }

    func now() -> Date { lock.withLock { current } }

    func set(_ date: Date) { lock.withLock { current = date } }

    func advance(by seconds: TimeInterval) { lock.withLock { current = current.addingTimeInterval(seconds) } }

    /// Runs `action` the first time virtual time passes `date`.
    func schedule(at date: Date, _ action: @escaping @Sendable () async -> Void) {
        lock.withLock { actions.append((date, action)) }
    }

    func sleep(seconds: TimeInterval) async throws {
        let due: [@Sendable () async -> Void] = lock.withLock {
            sleeps.append(seconds)
            current = current.addingTimeInterval(seconds)
            let ready = actions.filter { $0.at <= current }
            actions.removeAll { $0.at <= current }
            return ready.map(\.run)
        }
        for action in due { await action() }
        await Task.yield()
    }
}

func makeLiveTitle(state: SharedLiveTitle.State = .eligible, name: String = "Rainforest") -> SharedLiveTitle {
    SharedLiveTitle(
        id: MovieLibraryContract.digest(Data(name.utf8)),
        rootID: "11111111-1111-4111-8111-111111111111",
        fileName: "\(name).iso",
        revision: MovieLibraryContract.digest(Data("\(name)-rev".utf8)),
        state: state,
        reason: state == .ineligible ? "no_live_titles" : nil,
        durationSeconds: state == .eligible ? 2_400 : nil,
        audio: state == .eligible ? [SharedLiveAudio(pid: 4352, language: "eng", label: "English DTS-HD MA", isDefault: true)] : []
    )
}

func makeLiveCatalog(_ titles: [SharedLiveTitle]) -> SharedMovieCatalog {
    SharedMovieCatalog(
        roots: [SharedMovieRoot(id: "11111111-1111-4111-8111-111111111111", name: "Movies", isAvailable: true)],
        movies: [],
        liveTitles: titles
    )
}

/// A fake Mac that follows the live admission contract closely enough for player tests: the
/// start-ID signing window, start records, tombstones, the single slot, `release`, status,
/// and the derived-session relay routes. Every relay request is verified under the session
/// derived for its session ID, so a request signed with the parent session fails.
actor FakeLiveMac: MovieLibraryConnection {
    enum StartMode {
        case admit
        /// Admit, then lose the response.
        case admitAndLoseResponse
        /// Lose the request: nothing is admitted.
        case loseRequest
        case refuse(LiveStartRefusal.Reason)
    }

    struct Call: Equatable {
        let at: Date
        let reference: LiveStartReference
    }

    let parent: (client: RelayEstablishedSession, server: RelayEstablishedSession)
    let baseURL = URL(string: "http://mac.local:7432")!
    private let clock: ManualLiveClock
    private let policy = try! RelayRequestValidationPolicy()
    private let replay = try! RelayReplayNonceStore(capacity: 4_096)

    var startMode: StartMode = .admit
    var catalogs: [SharedMovieCatalog] = []
    /// Status states served in order for the current stream; the last one repeats.
    var statusStates: [LiveStreamStatus.State] = [.live]
    var statusFailure: LiveStreamStatus.Failure?
    var busyConverting = false
    var cancelAnswers: [LiveCancelOutcome] = []

    private(set) var catalogCalls = 0
    private(set) var startCalls: [(request: LiveStartRequest, signedAt: Date)] = []
    private(set) var cancelCalls: [Call] = []
    private(set) var releaseCalls: [Call] = []
    private(set) var statusCalls = 0
    private(set) var relaySessionIDs: [String] = []
    private(set) var relayPaths: [String] = []
    private(set) var stoppedStreams: [UUID: String] = [:]
    private(set) var slot: (reference: LiveStartReference, streamID: UUID)?
    private var records: [LiveStartReference: (request: LiveStartRequest, streamID: UUID)] = [:]
    private var tombstones: Set<LiveStartReference> = []
    private var statusIndex = 0
    private var holdsStatus = false
    private var statusWaiters: [CheckedContinuation<Void, Never>] = []

    /// Holds every status request until `releaseStatus`, so a test can act mid-startup.
    func holdStatus() { holdsStatus = true }
    func releaseStatus() {
        holdsStatus = false
        statusWaiters.forEach { $0.resume() }
        statusWaiters.removeAll()
    }

    init(parent: (client: RelayEstablishedSession, server: RelayEstablishedSession), clock: ManualLiveClock) {
        self.parent = parent
        self.clock = clock
    }

    static func make(clock: ManualLiveClock) async throws -> FakeLiveMac {
        FakeLiveMac(parent: try await makePairedSessions(now: clock.now()), clock: clock)
    }

    func set(startMode: StartMode) { self.startMode = startMode }
    func set(catalogs: [SharedMovieCatalog]) { self.catalogs = catalogs }
    func set(statusStates: [LiveStreamStatus.State], failure: LiveStreamStatus.Failure? = nil, busy: Bool = false) {
        self.statusStates = statusStates
        statusFailure = failure
        busyConverting = busy
        statusIndex = 0
    }
    func set(cancelAnswers: [LiveCancelOutcome]) { self.cancelAnswers = cancelAnswers }

    /// Admits a start whose request was delayed in the network, as the real host would.
    @discardableResult
    func admitLate(_ request: LiveStartRequest) -> UUID? {
        let reference = LiveStartReference(startID: request.startID, issuedAt: request.issuedAt)
        guard !tombstones.contains(reference), records[reference] == nil, slot == nil else { return nil }
        return admit(request, reference: reference)
    }

    var isStreamRunning: Bool { slot != nil }

    // MARK: MovieLibraryConnection

    func catalog() async throws -> SharedMovieCatalog {
        catalogCalls += 1
        guard !catalogs.isEmpty else { throw MovieLibraryError.unavailable }
        return catalogs.count > 1 ? catalogs.removeFirst() : catalogs[0]
    }

    func startLive(_ request: LiveStartRequest, signedAt: Date) async throws -> LiveStartResult {
        startCalls.append((request, signedAt))
        let reference = LiveStartReference(startID: request.startID, issuedAt: request.issuedAt)
        guard LiveStartLimits.acceptsRequest(
            signedAtUnixMilliseconds: signedAt.unixMilliseconds, issuedAtUnixMilliseconds: request.issuedAt, policy: policy
        ) else { return .expired }
        if case .loseRequest = startMode { throw URLError(.timedOut) }
        if tombstones.contains(reference) { return .refused(LiveStartRefusal(outcome: .cancelled)) }
        if let record = records[reference] {
            guard record.request == request else { return .refused(LiveStartRefusal(outcome: .conflict)) }
            return .started(LiveStartResponse(streamID: record.streamID))
        }
        if case let .refuse(reason) = startMode { return .refused(LiveStartRefusal(outcome: reason)) }
        if slot != nil { stopSlot(reason: "replaced") }
        let streamID = admit(request, reference: reference)
        if case .admitAndLoseResponse = startMode { throw URLError(.timedOut) }
        return .started(LiveStartResponse(streamID: streamID))
    }

    func cancelLive(_ reference: LiveStartReference) async throws -> LiveCancelOutcome {
        cancelCalls.append(Call(at: clock.now(), reference: reference))
        guard LiveStartLimits.acceptsRequest(
            signedAtUnixMilliseconds: clock.now().unixMilliseconds, issuedAtUnixMilliseconds: reference.issuedAt, policy: policy
        ) else { return .expired }
        if !cancelAnswers.isEmpty { return cancelAnswers.removeFirst() }
        if records[reference] != nil {
            if slot?.reference == reference { stopSlot(reason: "cancelled") }
            return .acknowledged
        }
        tombstones.insert(reference)
        return .acknowledged
    }

    func releaseLive(_ reference: LiveStartReference) async throws -> Bool {
        releaseCalls.append(Call(at: clock.now(), reference: reference))
        guard slot?.reference == reference else { return false }
        stopSlot(reason: "cancelled")
        return true
    }

    func liveStatus(streamID: UUID) async throws -> LiveStreamStatus {
        statusCalls += 1
        if isRevoked { throw MovieLibraryError.needsPairing }
        if holdsStatus { await withCheckedContinuation { statusWaiters.append($0) } }
        if let reason = stoppedStreams[streamID] {
            return LiveStreamStatus(state: .stopped, stopReason: reason, macBusyConverting: busyConverting)
        }
        guard slot?.streamID == streamID else { return LiveStreamStatus(state: .unknown, macBusyConverting: busyConverting) }
        let state = statusStates[min(statusIndex, statusStates.count - 1)]
        statusIndex += 1
        return LiveStreamStatus(
            state: state, earliestMilliseconds: 0, latestMilliseconds: 8_000,
            failure: state == .failed ? statusFailure : nil, macBusyConverting: busyConverting
        )
    }

    func liveStreamSession(streamID: UUID) throws -> (session: RelayEstablishedSession, baseURL: URL) {
        (try parent.client.liveStreamSession(streamID: streamID), baseURL)
    }

    // MARK: Relay routes under the derived stream session

    /// A transport that serves this Mac's relay routes.
    nonisolated func makeRelayTransport() async -> FakeRelayTransport {
        let transport = FakeRelayTransport()
        await transport.setHandler { [self] request in try await self.relay(request) }
        return transport
    }

    func relay(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let path = request.url?.path ?? ""
        relayPaths.append(path)
        guard let header = request.value(forHTTPHeaderField: RelayWireContract.authenticationHeader),
              let data = Data(base64Encoded: header),
              let authentication = try? JSONDecoder().decode(RelayAuthenticatedRequest.self, from: data),
              let streamID = UUID(uuidString: authentication.sessionID.rawValue)
        else { return (Data(), makeHTTPResponse(request, statusCode: 401)) }
        relaySessionIDs.append(authentication.sessionID.rawValue)
        if authentication.sessionID == parent.server.sessionID {
            // The parent session signs only /movies/v1 routes; it serves no relay route.
            return try signed(request, status: 404, body: Data(), session: parent.server)
        }
        let derived = try parent.server.liveStreamSession(streamID: streamID)
        do {
            try await derived.verify(
                authentication, actualMethod: request.httpMethod ?? "GET", actualRequestTarget: path,
                body: request.httpBody ?? Data(), now: clock.now(), replayStore: replay
            )
        } catch {
            return (Data(), makeHTTPResponse(request, statusCode: 401))
        }
        guard request.value(forHTTPHeaderField: RelayWireContract.mediaCapabilityHeader)
            .map(derived.mediaCapability.matches) == true else {
            return try signed(request, status: 403, body: Data(), session: derived)
        }
        if slot?.streamID != streamID {
            let ended = RelayLiveStreamEnded(
                error: .streamEnded, code: stoppedStreams[streamID] ?? RelayLiveStreamEnded.notCurrentCode, message: nil
            )
            return try signed(request, status: 410, body: JSONEncoder().encode(ended), session: derived)
        }
        switch path {
        case RelayWireContract.cancelPath:
            stopSlot(reason: "viewerStopped")
            return try signed(request, status: 204, body: Data(), session: derived)
        case RelayWireContract.positionPath:
            return try signed(request, status: 204, body: Data(), session: derived)
        case RelayWireContract.playlistSnapshotPath where failsStream:
            let failed = RelayLiveStreamEnded(error: .streamFailed, code: "live_video_decode_failed", message: "The decoder stopped.")
            return try signed(request, status: 503, body: JSONEncoder().encode(failed), session: derived)
        case RelayWireContract.playlistSnapshotPath:
            let snapshot = try RelayPlaylistSnapshot(
                earliestPlayableTimeMilliseconds: 0, totalDurationMilliseconds: 4_000, isFinalized: false,
                segments: [try RelayPlaylistSegment(
                    sequenceNumber: 0, startTimeMilliseconds: 0, durationMilliseconds: 4_000,
                    resourceIdentifier: "segment-000000000.m4s"
                )]
            )
            return try signed(request, status: 200, body: JSONEncoder().encode(snapshot), session: derived)
        default:
            return try signed(request, status: 200, body: Data("media".utf8), session: derived)
        }
    }

    /// The parent session ended on the Mac (the device was forgotten, or sharing restarted).
    func revokeSession() { isRevoked = true }
    private var isRevoked = false

    /// The running stream fails: its snapshot answers a signed 503 `stream_failed`, while the
    /// stream still holds the slot until the player stops it.
    func failStream() { failsStream = true }
    private var failsStream = false

    /// Ends the current stream on the Mac, as a failure, idle lease, or Stop would.
    func endStream(reason: String) { stopSlot(reason: reason) }

    private func signed(
        _ request: URLRequest, status: Int, body: Data, session: RelayEstablishedSession
    ) throws -> (Data, HTTPURLResponse) {
        (body, try makeAuthenticatedHTTPResponse(request, body: body, serverSession: session, statusCode: status))
    }

    private func admit(_ request: LiveStartRequest, reference: LiveStartReference) -> UUID {
        let streamID = UUID()
        records[reference] = (request, streamID)
        slot = (reference, streamID)
        return streamID
    }

    private func stopSlot(reason: String) {
        guard let slot else { return }
        stoppedStreams[slot.streamID] = reason
        self.slot = nil
    }
}

@MainActor
func waitUntil(
    timeout: TimeInterval = 5,
    file: StaticString = #filePath,
    line: UInt = #line,
    _ condition: @MainActor () async -> Bool
) async {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if await condition() { return }
        try? await Task.sleep(for: .milliseconds(10))
    }
    XCTFail("Timed out waiting for condition", file: file, line: line)
}

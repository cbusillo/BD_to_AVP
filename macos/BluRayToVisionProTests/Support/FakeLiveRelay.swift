import Foundation
@testable import BluRayToVisionPro

/// A scripted live stream that follows the `LiveRelayStreaming` contract: after stop it answers
/// 410 `stream_ended`, after failure 503 `stream_failed`, and `stop` is idempotent with every
/// caller awaiting the same cleanup. `holdStop()` keeps that cleanup suspended until
/// `releaseStop()`, so a test can observe a slot while it drains.
actor FakeLiveRelayStream: LiveRelayStreaming {
    nonisolated let id: UUID
    private var currentState: LiveRelayStreamState
    private var scriptedStates: [LiveRelayStreamState]
    private var response: RelayHTTPResponse
    private(set) var stopCalls: [LiveRelayStopReason] = []
    private(set) var leaseRenewals = 0
    private(set) var requests: [RelayHTTPRequest] = []
    private(set) var isStopFinished = false
    private var stopTask: Task<Void, Never>?
    private var stopGate: CheckedContinuation<Void, Never>?
    private var isStopHeld = false

    init(
        id: UUID = UUID(),
        states: [LiveRelayStreamState] = [.starting],
        response: RelayHTTPResponse = RelayHTTPResponse(statusCode: 200)
    ) {
        precondition(!states.isEmpty)
        self.id = id
        currentState = states[0]
        scriptedStates = Array(states.dropFirst())
        self.response = response
    }

    func state() -> LiveRelayStreamState {
        currentState
    }

    /// Moves to the next scripted state, unless the stream has stopped.
    func advance() {
        guard !isStopped, !scriptedStates.isEmpty else { return }
        currentState = scriptedStates.removeFirst()
    }

    func relayResponse(for request: RelayHTTPRequest) async -> RelayHTTPResponse {
        requests.append(request)
        switch currentState {
        case let .stopped(reason):
            return .liveStreamEnded(RelayLiveStreamEnded(error: .streamEnded, code: reason.rawValue, message: nil))
        case let .failed(code, message):
            return .liveStreamEnded(RelayLiveStreamEnded(error: .streamFailed, code: code, message: message))
        case .starting, .live, .complete:
            return response
        }
    }

    func renewLease() {
        leaseRenewals += 1
    }

    func holdStop() {
        isStopHeld = true
    }

    func releaseStop() {
        isStopHeld = false
        stopGate?.resume()
        stopGate = nil
    }

    func stop(_ reason: LiveRelayStopReason) async {
        stopCalls.append(reason)
        if stopTask == nil {
            if !isStopped {
                currentState = .stopped(reason)
            }
            stopTask = Task { await self.finishStop() }
        }
        await stopTask?.value
    }

    private func finishStop() async {
        if isStopHeld {
            await withCheckedContinuation { stopGate = $0 }
        }
        isStopFinished = true
    }

    private var isStopped: Bool {
        if case .stopped = currentState { return true }
        return false
    }
}

/// A starter whose `start` launches a `FakeLiveRelayStream`, then stays suspended until the test
/// releases it. It honors the `LiveRelayStreamStarting` cancellation contract: a cancelled start
/// stops the stream it launched before throwing `CancellationError`, including when the task was
/// cancelled before `start` reached its gate.
actor FakeLiveRelayStreamStarter: LiveRelayStreamStarting {
    struct Call: Sendable {
        let request: LiveStreamRequest
        let id: UUID
    }

    private(set) var calls: [Call] = []
    private(set) var launched: [FakeLiveRelayStream] = []
    private var holdsStarts: Bool
    private var nextFailure: (any Error)?
    private var gates: [UUID: CheckedContinuation<Void, any Error>] = [:]
    private var cancelledIDs: Set<UUID> = []
    private var callWaiters: [(count: Int, continuation: CheckedContinuation<Void, Never>)] = []
    private let streamStates: [LiveRelayStreamState]

    init(holdsStarts: Bool = true, streamStates: [LiveRelayStreamState] = [.starting]) {
        self.holdsStarts = holdsStarts
        self.streamStates = streamStates
    }

    /// The next `start` throws `error` after launching nothing.
    func failNextStart(with error: any Error) {
        nextFailure = error
    }

    /// Lets the suspended start for `id` return its stream.
    func release(_ id: UUID) {
        gates.removeValue(forKey: id)?.resume()
    }

    /// Lets every current and future start return immediately.
    func releaseAll() {
        holdsStarts = false
        for gate in gates.values {
            gate.resume()
        }
        gates.removeAll()
    }

    /// Suspends until `start` has been entered `count` times.
    func waitForCalls(_ count: Int) async {
        guard calls.count < count else { return }
        await withCheckedContinuation { callWaiters.append((count, $0)) }
    }

    var suspendedStartCount: Int {
        gates.count
    }

    func start(_ request: LiveStreamRequest, id: UUID) async throws -> any LiveRelayStreaming {
        calls.append(Call(request: request, id: id))
        resumeCallWaiters()
        if let failure = nextFailure {
            nextFailure = nil
            throw failure
        }
        let stream = FakeLiveRelayStream(id: id, states: streamStates)
        launched.append(stream)
        do {
            try await withTaskCancellationHandler {
                try await waitAtGate(id)
            } onCancel: {
                Task { await self.cancelGate(id) }
            }
        } catch {
            await stream.stop(.cancelled)
            throw CancellationError()
        }
        return stream
    }

    private func waitAtGate(_ id: UUID) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
            if Task.isCancelled || cancelledIDs.remove(id) != nil {
                continuation.resume(throwing: CancellationError())
            } else if !holdsStarts {
                continuation.resume()
            } else {
                gates[id] = continuation
            }
        }
    }

    private func cancelGate(_ id: UUID) {
        if let gate = gates.removeValue(forKey: id) {
            gate.resume(throwing: CancellationError())
        } else {
            cancelledIDs.insert(id)
        }
    }

    private func resumeCallWaiters() {
        let ready = callWaiters.filter { calls.count >= $0.count }
        callWaiters.removeAll { calls.count >= $0.count }
        for waiter in ready {
            waiter.continuation.resume()
        }
    }
}

/// A source lease for tests; nothing on disk backs it.
func makeFakeLiveStreamRequest(playlist: Int = 800, audioPID: Int = 4352) -> LiveStreamRequest {
    final class Retainer: Sendable {}
    return LiveStreamRequest(
        source: LiveSourceLease(
            canonicalPath: "/Volumes/Movies/Rainforest.iso",
            identity: LiveSourceIdentity(
                device: 1,
                inode: 2,
                sizeBytes: 3,
                modificationSeconds: 4,
                modificationNanoseconds: 5
            ),
            retainer: Retainer()
        ),
        playlist: playlist,
        audioPID: audioPID
    )
}

/// A reference model of the Mac host's live admission contract (`LiveStartAdmissionSlot`,
/// `LiveStartLimits`), driven by a fake clock. Host and player fakes use it as the Mac side.
actor FakeLiveStartAdmission {
    private struct StartIdentity: Hashable {
        let parentSessionID: RelaySessionIdentifier
        let startID: UUID
    }

    /// The remembered outcome of an admitted start, consulted before the slot.
    private struct StartRecord {
        let key: LiveStartKey
        var outcome: LiveStartOutcome
        var expiresAt: Date
    }

    private let starter: any LiveRelayStreamStarting
    private let liveParentSessionIDs: Set<RelaySessionIdentifier>
    private(set) var now: Date
    private(set) var slot: LiveStartAdmissionSlot = .empty
    private var history: [StartIdentity: StartRecord] = [:]
    private var tombstones: [StartIdentity: Date] = [:]
    private var admittedStarts: [RelaySessionIdentifier: [Date]] = [:]

    init(
        starter: any LiveRelayStreamStarting,
        liveParentSessionIDs: Set<RelaySessionIdentifier>,
        now: Date = Date(timeIntervalSince1970: 1_700_000_000)
    ) {
        self.starter = starter
        self.liveParentSessionIDs = liveParentSessionIDs
        self.now = now
    }

    func advanceClock(by interval: TimeInterval) {
        now = now.addingTimeInterval(interval)
    }

    var tombstoneCount: Int {
        expire()
        return tombstones.count
    }

    func start(_ key: LiveStartKey, request: LiveStreamRequest) async -> LiveStartOutcome {
        let identity = StartIdentity(parentSessionID: key.parentSessionID, startID: key.startID)
        while true {
            expire()
            if let record = history[identity] {
                return record.key == key ? record.outcome : .conflict
            }
            if tombstones[identity] != nil {
                return .cancelled
            }
            switch slot {
            case let .draining(task):
                await settle(task)
                continue
            case .reserved, .running:
                if slot.key!.parentSessionID != key.parentSessionID {
                    return .busy
                }
                await drain(reason: .replaced)
                continue
            case .empty:
                let recent = admittedStarts[key.parentSessionID, default: []]
                guard recent.count < LiveStartLimits.startsPerSessionPerMinute else {
                    return .rateLimited
                }
                admittedStarts[key.parentSessionID] = recent + [now]
                let streamID = LiveStreamIdentifier.make(excluding: liveParentSessionIDs)
                let starter = starter
                let startTask = Task { try await starter.start(request, id: streamID) }
                history[identity] = StartRecord(
                    key: key,
                    outcome: .started(streamID: streamID),
                    expiresAt: now.addingTimeInterval(LiveStartLimits.tombstoneLifetime)
                )
                slot = .reserved(key, startTask: startTask)
                Task { await self.install(key, from: startTask) }
                return .started(streamID: streamID)
            }
        }
    }

    func cancel(parentSessionID: RelaySessionIdentifier, startID: UUID) async -> LiveCancelOutcome {
        expire()
        let identity = StartIdentity(parentSessionID: parentSessionID, startID: startID)
        if var record = history[identity] {
            // Recorded before the drain, so a retry arriving meanwhile is refused. Never limited.
            record.outcome = .cancelled
            record.expiresAt = max(record.expiresAt, now.addingTimeInterval(LiveStartLimits.tombstoneLifetime))
            history[identity] = record
            if let held = slot.key, held.parentSessionID == parentSessionID, held.startID == startID {
                await drain(reason: .cancelled)
            }
            return .acknowledged
        }
        if tombstones[identity] != nil {
            return .acknowledged
        }
        let sessionTombstones = tombstones.keys.filter { $0.parentSessionID == parentSessionID }.count
        guard sessionTombstones < LiveStartLimits.tombstoneCancelsPerSessionPerMinute,
              tombstones.count < LiveStartLimits.tombstoneCapacity
        else {
            return .retryLater
        }
        tombstones[identity] = now.addingTimeInterval(LiveStartLimits.tombstoneLifetime)
        return .acknowledged
    }

    /// Forget, parent expiry, sharing shutdown, and quit: drain and await.
    func release(reason: LiveRelayStopReason) async {
        await drain(reason: reason)
    }

    private func drain(reason: LiveRelayStopReason) async {
        guard let task = slot.drainTask(reason: reason) else { return }
        slot = .draining(task)
        await settle(task)
    }

    /// Awaits a drain, then empties the slot if that drain still holds it. Every waiter does
    /// this, so the slot never stays draining after its drain has finished.
    private func settle(_ task: Task<Void, Never>) async {
        await task.value
        if case let .draining(current) = slot, current == task {
            slot = .empty
        }
    }

    private func install(_ key: LiveStartKey, from startTask: Task<any LiveRelayStreaming, Error>) async {
        let result = await startTask.result
        guard case let .reserved(heldKey, _) = slot, heldKey == key else {
            // A late result for a key that no longer holds the slot: the drain owns it.
            return
        }
        switch result {
        case let .success(stream):
            slot = .running(key, stream: stream)
        case .failure:
            slot = .empty
        }
    }

    private func expire() {
        let heldKey = slot.key
        history = history.filter { $0.value.expiresAt > now || $0.value.key == heldKey }
        tombstones = tombstones.filter { $0.value > now }
        let windowStart = now.addingTimeInterval(-60)
        admittedStarts = admittedStarts.mapValues { $0.filter { $0 > windowStart } }
    }
}

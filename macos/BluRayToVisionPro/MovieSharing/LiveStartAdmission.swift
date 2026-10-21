import Foundation

/// What the host answers a live start, with a plain-language reason for `sourceChanged`.
struct LiveStartDecision: Equatable, Sendable {
    let outcome: LiveStartOutcome
    let message: String?

    init(_ outcome: LiveStartOutcome, message: String? = nil) {
        self.outcome = outcome
        self.message = message
    }
}

/// Thrown by a start's `prepare` step when the title cannot be streamed as requested.
struct LiveStartPreparationRefusal: Error, Equatable {
    let message: String
}

/// The Mac host's single live slot and its start records, implementing the contract documented
/// on `LiveStartAdmissionSlot` and `LiveStartLimits` (the same rules `FakeLiveStartAdmission`
/// models for tests).
///
/// `start` and `cancel` return nil where the host answers an unsigned 401: the parent session
/// ended while the request was in flight. Time is the host's verification clock.
actor LiveStartAdmission {
    /// The stream that holds the slot for a parent session. `stream` is nil while its start runs.
    struct Current: Sendable {
        let key: LiveStartKey
        let streamID: UUID
        let stream: (any LiveRelayStreaming)?
    }

    /// The last stream a parent session held, kept so a late request or status learns why it
    /// ended. One per session; nothing else is kept per stream.
    struct Ended: Equatable, Sendable {
        let streamID: UUID
        let body: RelayLiveStreamEnded
    }

    private struct StartIdentity: Hashable {
        let parentSessionID: RelaySessionIdentifier
        let startID: UUID
        let issuedAtUnixMilliseconds: Int64

        init(_ key: LiveStartKey) {
            self.init(parentSessionID: key.parentSessionID, startID: key.startID, issuedAt: key.issuedAtUnixMilliseconds)
        }

        init(parentSessionID: RelaySessionIdentifier, startID: UUID, issuedAt: Int64) {
            self.parentSessionID = parentSessionID
            self.startID = startID
            issuedAtUnixMilliseconds = issuedAt
        }
    }

    private struct StartRecord {
        let key: LiveStartKey
        var outcome: LiveStartOutcome
        var exitedSlotAt: Date?
    }

    private let starter: any LiveRelayStreamStarting
    private let policy: RelayRequestValidationPolicy
    private let now: @Sendable () -> Date
    private let watchInterval: Duration
    private(set) var slot: LiveStartAdmissionSlot = .empty
    private var slotStreamID: UUID?
    private var drainingKey: LiveStartKey?
    private var drainingStreamID: UUID?
    private var records: [StartIdentity: StartRecord] = [:]
    private var tombstones: Set<StartIdentity> = []
    private var admittedStarts: [RelaySessionIdentifier: [Date]] = [:]
    private var endedSessions: Set<RelaySessionIdentifier> = []
    private var drainingReason: LiveRelayStopReason = .cancelled
    private var lastEnded: [RelaySessionIdentifier: Ended] = [:]
    /// How a running stream ended by itself, captured before the drain stops it.
    private var endings: [UUID: RelayLiveStreamEnded] = [:]
    private var drainWaiters = 0

    init(
        starter: any LiveRelayStreamStarting,
        policy: RelayRequestValidationPolicy,
        now: @escaping @Sendable () -> Date = { Date() },
        watchInterval: Duration = .milliseconds(500)
    ) {
        self.starter = starter
        self.policy = policy
        self.now = now
        self.watchInterval = watchInterval
    }

    // MARK: - Queries

    func current(for parentSessionID: RelaySessionIdentifier) -> Current? {
        guard let key = slot.key, key.parentSessionID == parentSessionID, let streamID = slotStreamID else { return nil }
        if case let .running(_, stream) = slot {
            return Current(key: key, streamID: streamID, stream: stream)
        }
        return Current(key: key, streamID: streamID, stream: nil)
    }

    /// Why a parent session's most recent stream ended, if `streamID` names it.
    func ended(_ streamID: UUID, for parentSessionID: RelaySessionIdentifier) -> Ended? {
        guard let ended = lastEnded[parentSessionID], ended.streamID == streamID else { return nil }
        return ended
    }

    var isEmpty: Bool {
        if case .empty = slot { return true }
        return false
    }

    var startRecordCount: Int {
        expire()
        return records.count
    }

    var tombstoneCount: Int {
        expire()
        return tombstones.count
    }

    /// Sessions with retained records, tombstones, or rate-limit entries. The one remembered
    /// ending per session is not counted; it goes when the session ends.
    var sessionsWithState: Set<RelaySessionIdentifier> {
        expire()
        return Set(records.keys.map(\.parentSessionID))
            .union(tombstones.map(\.parentSessionID))
            .union(admittedStarts.keys)
    }

    /// Whether a start is suspended waiting for another start's drain.
    var isWaitingOnDrain: Bool {
        drainWaiters > 0
    }

    func isEnded(_ parentSessionID: RelaySessionIdentifier) -> Bool {
        endedSessions.contains(parentSessionID)
    }

    // MARK: - Start

    /// Admits a start signed at `signedAt` (Unix milliseconds). `prepare` authorizes the source
    /// and builds the request; it runs only when the slot is free for this start, and every
    /// check is repeated after it returns. `liveParentSessionIDs` are excluded as stream IDs.
    func start(
        _ key: LiveStartKey,
        signedAt: Int64,
        liveParentSessionIDs: Set<RelaySessionIdentifier>,
        prepare: @Sendable () async throws -> LiveStreamRequest
    ) async -> LiveStartDecision? {
        guard !endedSessions.contains(key.parentSessionID) else { return nil }
        guard LiveStartLimits.acceptsRequest(
            signedAtUnixMilliseconds: signedAt,
            issuedAtUnixMilliseconds: key.issuedAtUnixMilliseconds,
            policy: policy
        ) else {
            return LiveStartDecision(.expired)
        }
        let identity = StartIdentity(key)
        var prepared: LiveStreamRequest?
        while true {
            guard !endedSessions.contains(key.parentSessionID) else { return nil }
            expire()
            if let record = records[identity] {
                return LiveStartDecision(record.key == key ? record.outcome : .conflict)
            }
            if tombstones.contains(identity) {
                return LiveStartDecision(.cancelled)
            }
            switch slot {
            case let .draining(task):
                drainWaiters += 1
                await settle(task)
                drainWaiters -= 1
                guard isVerifiable(identity) else { return LiveStartDecision(.expired) }
                continue
            case .reserved, .running:
                if slot.key?.parentSessionID != key.parentSessionID {
                    return LiveStartDecision(.busy)
                }
                await drain(reason: .replaced)
                guard isVerifiable(identity) else { return LiveStartDecision(.expired) }
                continue
            case .empty:
                let recent = admittedStarts[key.parentSessionID, default: []]
                guard recent.count < LiveStartLimits.startsPerSessionPerMinute,
                      records.count < LiveStartLimits.startRecordCapacity
                else {
                    return LiveStartDecision(.rateLimited)
                }
                guard let request = prepared else {
                    do {
                        prepared = try await prepare()
                    } catch let refusal as LiveStartPreparationRefusal {
                        return LiveStartDecision(.sourceChanged, message: refusal.message)
                    } catch {
                        return LiveStartDecision(.sourceChanged, message: nil)
                    }
                    guard isVerifiable(identity) else { return LiveStartDecision(.expired) }
                    continue
                }
                admittedStarts[key.parentSessionID] = recent + [now()]
                let streamID = LiveStreamIdentifier.make(excluding: liveParentSessionIDs)
                let starter = starter
                let startTask = Task { try await starter.start(request, id: streamID) }
                records[identity] = StartRecord(key: key, outcome: .started(streamID: streamID), exitedSlotAt: nil)
                slot = .reserved(key, startTask: startTask)
                slotStreamID = streamID
                Task { await self.install(key, streamID: streamID, from: startTask) }
                return LiveStartDecision(.started(streamID: streamID))
            }
        }
    }

    // MARK: - Cancel and release

    func cancel(
        parentSessionID: RelaySessionIdentifier,
        startID: UUID,
        issuedAtUnixMilliseconds issuedAt: Int64,
        signedAt: Int64
    ) async -> LiveCancelOutcome? {
        guard !endedSessions.contains(parentSessionID) else { return nil }
        guard LiveStartLimits.acceptsRequest(
            signedAtUnixMilliseconds: signedAt,
            issuedAtUnixMilliseconds: issuedAt,
            policy: policy
        ) else {
            return .expired
        }
        expire()
        let identity = StartIdentity(parentSessionID: parentSessionID, startID: startID, issuedAt: issuedAt)
        if var record = records[identity] {
            // Recorded before the drain, so a retry arriving meanwhile is refused. Never limited.
            record.outcome = .cancelled
            records[identity] = record
            if let held = slot.key, StartIdentity(held) == identity {
                await drain(reason: .cancelled)
            }
            return .acknowledged
        }
        if tombstones.contains(identity) {
            return .acknowledged
        }
        let sessionTombstones = tombstones.filter { $0.parentSessionID == parentSessionID }.count
        guard sessionTombstones < LiveStartLimits.tombstonesPerSession,
              tombstones.count < LiveStartLimits.tombstoneCapacity
        else {
            return .retryLater
        }
        tombstones.insert(identity)
        return .acknowledged
    }

    /// Drains the slot only when exactly this start identity holds it. No signing window, no
    /// limit, and nothing is recorded. Returns whether it drained.
    func release(parentSessionID: RelaySessionIdentifier, startID: UUID, issuedAtUnixMilliseconds issuedAt: Int64) async -> Bool {
        let identity = StartIdentity(parentSessionID: parentSessionID, startID: startID, issuedAt: issuedAt)
        guard let held = slot.key, StartIdentity(held) == identity else { return false }
        await drain(reason: .cancelled)
        return true
    }

    /// Sharing shutdown and quit: drain and await whatever holds the slot.
    func releaseAll(reason: LiveRelayStopReason) async {
        while true {
            switch slot {
            case .empty:
                return
            case let .draining(task):
                await settle(task)
            case .reserved, .running:
                await drain(reason: reason)
            }
        }
    }

    /// Forget and parent expiry. The session is marked ended before any cleanup is awaited, so a
    /// start or cancel that already passed verification admits and records nothing.
    func endSession(_ parentSessionID: RelaySessionIdentifier, reason: LiveRelayStopReason = .sessionEnded) async {
        endedSessions.insert(parentSessionID)
        while true {
            if case let .draining(task) = slot, drainingKey?.parentSessionID == parentSessionID {
                await settle(task)
                continue
            }
            if slot.key?.parentSessionID == parentSessionID {
                await drain(reason: reason)
                continue
            }
            break
        }
        records = records.filter { $0.key.parentSessionID != parentSessionID }
        tombstones = tombstones.filter { $0.parentSessionID != parentSessionID }
        admittedStarts[parentSessionID] = nil
        lastEnded[parentSessionID] = nil
    }

    // MARK: - Slot

    private func drain(reason: LiveRelayStopReason) async {
        let key = slot.key
        let streamID = slotStreamID
        guard let task = slot.drainTask(reason: reason) else { return }
        if key != nil {
            drainingKey = key
            drainingStreamID = streamID
            drainingReason = reason
        }
        slot = .draining(task)
        slotStreamID = nil
        await settle(task)
    }

    /// Awaits a drain, then empties the slot if that drain still holds it.
    private func settle(_ task: Task<Void, Never>) async {
        await task.value
        guard case let .draining(current) = slot, current == task else { return }
        slot = .empty
        if let key = drainingKey {
            leftSlot(key)
            if let streamID = drainingStreamID {
                let body = endings.removeValue(forKey: streamID)
                    ?? RelayLiveStreamEnded(error: .streamEnded, code: drainingReason.rawValue, message: nil)
                lastEnded[key.parentSessionID] = Ended(streamID: streamID, body: body)
            }
        }
        drainingKey = nil
        drainingStreamID = nil
    }

    private func leftSlot(_ key: LiveStartKey) {
        let identity = StartIdentity(key)
        guard var record = records[identity] else { return }
        record.exitedSlotAt = now()
        records[identity] = record
    }

    private func install(_ key: LiveStartKey, streamID: UUID, from startTask: Task<any LiveRelayStreaming, Error>) async {
        let result = await startTask.result
        guard case let .reserved(heldKey, _) = slot, heldKey == key else {
            // A late result for a key that no longer holds the slot: the drain owns it.
            return
        }
        switch result {
        case let .success(stream):
            slot = .running(key, stream: stream)
            Task { await self.watch(key, stream: stream) }
        case let .failure(error):
            slot = .empty
            slotStreamID = nil
            leftSlot(key)
            lastEnded[key.parentSessionID] = Ended(
                streamID: streamID,
                body: RelayLiveStreamEnded(
                    error: .streamFailed,
                    code: Self.startFailureCode(for: error),
                    message: "The live stream could not start on the Mac."
                )
            )
        }
    }

    /// A stream that stops or fails by itself (its own cancel, idle, ready timeout, worker
    /// failure, changed source) releases the slot: the host drains it and awaits its cleanup.
    private func watch(_ key: LiveStartKey, stream: any LiveRelayStreaming) async {
        while true {
            try? await Task.sleep(for: watchInterval)
            guard case let .running(heldKey, _) = slot, heldKey == key else { return }
            switch await stream.state() {
            case let .failed(code, message):
                endings[stream.id] = RelayLiveStreamEnded(error: .streamFailed, code: code, message: message)
                guard case let .running(stillHeld, _) = slot, stillHeld == key else { return }
                await drain(reason: .cancelled)
                return
            case let .stopped(reason):
                endings[stream.id] = RelayLiveStreamEnded(error: .streamEnded, code: reason.rawValue, message: nil)
                guard case let .running(stillHeld, _) = slot, stillHeld == key else { return }
                await drain(reason: reason)
                return
            case .starting, .live, .complete:
                continue
            }
        }
    }

    static func startFailureCode(for error: any Error) -> String {
        switch error {
        case LiveRelayStreamStartError.workspaceUnavailable:
            "live_workspace_unavailable"
        case is WorkerLaunchConfigurationError:
            "live_worker_unavailable"
        default:
            "live_start_failed"
        }
    }

    // MARK: - Expiry

    private var nowMilliseconds: Int64 {
        RelayTime.unixMilliseconds(for: now()) ?? 0
    }

    private func isVerifiable(_ identity: StartIdentity) -> Bool {
        LiveStartLimits.isVerifiable(
            issuedAtUnixMilliseconds: identity.issuedAtUnixMilliseconds,
            nowUnixMilliseconds: nowMilliseconds,
            policy: policy
        )
    }

    private func expire() {
        let current = nowMilliseconds
        // A record without an exit time still holds the slot or is draining, so it is kept.
        records = records.filter { identity, record in
            guard let exited = record.exitedSlotAt else { return true }
            return isVerifiable(identity) || (RelayTime.unixMilliseconds(for: exited) ?? 0) >= current
        }
        tombstones = tombstones.filter { isVerifiable($0) }
        let windowStart = now().addingTimeInterval(-60)
        admittedStarts = admittedStarts.compactMapValues { times in
            let recent = times.filter { $0 > windowStart }
            return recent.isEmpty ? nil : recent
        }
    }
}

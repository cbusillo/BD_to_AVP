import Foundation

/// Checks whether an authorized ISO can stream live.
protocol LiveTitleInspecting: Sendable {
    func inspect(_ lease: LiveSourceLease) async throws -> LiveTitleInspection
}

struct LiveTitleInspectionFailure: Error, Equatable {
    let code: String
    let message: String
}

/// Runs `inspect_live_title` in its own worker, which reads the Mac's descriptor through a
/// handoff and never opens the source path.
struct WorkerLiveTitleInspector: LiveTitleInspecting {
    let makeRunner: @Sendable () throws -> any WorkerProcessRunning & Sendable
    var handoffDeadline: TimeInterval = 10

    static func automatic() -> WorkerLiveTitleInspector {
        WorkerLiveTitleInspector {
            WorkerProcessClient(configuration: try WorkerLaunchConfiguration.automatic())
        }
    }

    func inspect(_ lease: LiveSourceLease) async throws -> LiveTitleInspection {
        guard let holder = lease.retainer as? LiveSourceDescriptorHolding else {
            throw LiveTitleInspectionFailure(code: "live_source_unavailable", message: "The disc is not open on the Mac.")
        }
        let handoff = try LiveSourceHandoff()
        defer { handoff.close() }
        let runner = try makeRunner()
        let job = WorkerJobSpec(
            liveTitleInspectionSourcePath: lease.canonicalPath,
            identity: lease.identity,
            descriptorHandoff: handoff.jobField
        )
        let deadline = handoffDeadline
        let serving = Task.detached {
            try? await handoff.serve(
                descriptor: holder.descriptor,
                expectedPeer: { runner.currentProcessIdentifier() },
                deadline: deadline
            )
        }
        let result: WorkerRunResult
        do {
            result = try await withTaskCancellationHandler {
                try await runner.run(job: job) { _ in }
            } onCancel: {
                runner.cancel()
            }
        } catch {
            handoff.close()
            await serving.value
            throw error
        }
        handoff.close()
        await serving.value
        withExtendedLifetime(lease) {}
        guard result.terminalEvent.type == .jobCompleted, let inspection = result.terminalEvent.payload.liveTitleResult else {
            let failure = result.terminalEvent.payload.error
            throw LiveTitleInspectionFailure(
                code: failure?.code ?? "live_title_inspection_failed",
                message: failure?.message ?? "The Mac could not check this disc."
            )
        }
        return inspection
    }
}

/// Live eligibility of the listed ISOs, cached by `(id, revision)`.
///
/// - A cached entry never authorizes anything; it only decides what the catalog shows and
///   which playlist a start uses. `LiveSourceAuthorizer` runs before every inspection and start.
/// - An uncached title shows `checking` and queues one serial background inspection.
/// - A failed check (including a refused or timed-out open) shows the reason and retries after
///   `retryInterval`. Only finished inspections are stored, up to `capacity` entries, in
///   `Application Support/<bundle>/live-titles.v1.json`.
actor LiveTitleIndex {
    struct Record: Codable, Equatable, Sendable {
        let id: String
        let revision: String
        let eligible: Bool
        let reason: String?
        let playlist: Int?
        let durationSeconds: Double?
        let audio: [SharedLiveAudio]
        let checkedAt: Date
    }

    private struct Failure {
        let revision: String
        let reason: String
        let retryAt: Date
    }

    private struct Store: Codable {
        let version: Int
        let records: [Record]
    }

    private let storeURL: URL?
    private let authorizer: LiveSourceAuthorizer
    private let inspector: any LiveTitleInspecting
    private let now: @Sendable () -> Date
    private let retryInterval: TimeInterval
    private let capacity: Int
    private var records: [String: Record] = [:]
    private var failures: [String: Failure] = [:]
    private var queue: [LiveCatalogCandidate] = []
    private var worker: Task<Void, Never>?
    private var idleWaiters: [CheckedContinuation<Void, Never>] = []
    private var stopped = false
    private var loadTask: Task<Void, Never>?
    private var persistTask: Task<Void, Never>?
    private(set) var inspectionCount = 0

    init(
        storeURL: URL?,
        authorizer: LiveSourceAuthorizer = LiveSourceAuthorizer(),
        inspector: any LiveTitleInspecting,
        now: @escaping @Sendable () -> Date = { Date() },
        retryInterval: TimeInterval = 600,
        capacity: Int = 256
    ) {
        self.storeURL = storeURL
        self.authorizer = authorizer
        self.inspector = inspector
        self.now = now
        self.retryInterval = retryInterval
        self.capacity = capacity
    }

    /// The cache file under Application Support. Naming it touches no disk; the directory is
    /// created by the first background write.
    static func defaultStoreURL(fileManager: FileManager = .default) -> URL? {
        guard let support = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else { return nil }
        return support
            .appendingPathComponent(Bundle.main.bundleIdentifier ?? "com.shinycomputers.bd-to-avp", isDirectory: true)
            .appendingPathComponent("live-titles.v1.json")
    }

    static let maximumStoreBytes = 4 * 1_048_576

    /// Reads the cache once, in the background and within the file-access limit. A file that is
    /// missing, unreadable, too large, or slow is ignored; titles are then inspected again.
    private func loadStoreIfNeeded() async {
        if let loadTask { return await loadTask.value }
        let storeURL = storeURL
        let capacity = capacity
        let task = Task<Void, Never> {
            guard let storeURL else { return }
            let loaded = await BoundedFileAccess.run { Self.readStore(at: storeURL, capacity: capacity) } ?? []
            for record in loaded where records[record.id] == nil { records[record.id] = record }
        }
        loadTask = task
        await task.value
    }

    private nonisolated static func readStore(at url: URL, capacity: Int) -> [Record] {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return [] }
        defer { try? handle.close() }
        // Bound the size before reading anything into memory.
        guard let size = try? handle.seekToEnd(), size <= UInt64(maximumStoreBytes) else { return [] }
        try? handle.seek(toOffset: 0)
        guard let data = try? handle.read(upToCount: maximumStoreBytes),
              let store = try? JSONDecoder().decode(Store.self, from: data), store.version == 1
        else { return [] }
        return store.records.suffix(capacity).filter { MovieLibraryContract.isDigest($0.id) }
    }

    /// Stops inspecting and returns once a running inspection's worker has exited and the cache
    /// has been written.
    func stop() async {
        stopped = true
        queue.removeAll()
        let worker = worker
        worker?.cancel()
        await worker?.value
        self.worker = nil
        resumeIdleWaiters()
        await persistTask?.value
    }

    /// The catalog rows for the listed ISOs. Queues an inspection for each uncached title.
    func titles(for candidates: [LiveCatalogCandidate]) async -> [SharedLiveTitle] {
        await loadStoreIfNeeded()
        return candidates.map { candidate in
            if let record = records[candidate.id], record.revision == candidate.revision {
                return SharedLiveTitle(
                    id: candidate.id, rootID: candidate.rootID, fileName: candidate.fileName, revision: candidate.revision,
                    state: record.eligible ? .eligible : .ineligible,
                    reason: record.reason, durationSeconds: record.durationSeconds,
                    audio: record.eligible ? record.audio : []
                )
            }
            if let failure = failures[candidate.id], failure.revision == candidate.revision, now() < failure.retryAt {
                return SharedLiveTitle(
                    id: candidate.id, rootID: candidate.rootID, fileName: candidate.fileName, revision: candidate.revision,
                    state: .ineligible, reason: failure.reason
                )
            }
            enqueue(candidate)
            return SharedLiveTitle(
                id: candidate.id, rootID: candidate.rootID, fileName: candidate.fileName, revision: candidate.revision,
                state: .checking
            )
        }
    }

    /// The finished inspection for exactly this revision, if any.
    func record(id: String, revision: String) async -> Record? {
        await loadStoreIfNeeded()
        guard let record = records[id], record.revision == revision else { return nil }
        return record
    }

    /// Why the last check of exactly this revision failed, if it did.
    func failureReason(id: String, revision: String) -> String? {
        guard let failure = failures[id], failure.revision == revision else { return nil }
        return failure.reason
    }

    /// Suspends until no inspection is queued or running. For tests.
    func waitUntilIdle() async {
        guard worker != nil else { return }
        await withCheckedContinuation { idleWaiters.append($0) }
    }

    private func enqueue(_ candidate: LiveCatalogCandidate) {
        guard !stopped, !queue.contains(where: { $0.id == candidate.id && $0.revision == candidate.revision }) else { return }
        queue.removeAll { $0.id == candidate.id }
        queue.append(candidate)
        if worker == nil {
            worker = Task { await self.drainQueue() }
        }
    }

    private func drainQueue() async {
        while !stopped, !Task.isCancelled, !queue.isEmpty {
            let candidate = queue.removeFirst()
            if records[candidate.id]?.revision == candidate.revision { continue }
            await inspect(candidate)
        }
        worker = nil
        resumeIdleWaiters()
    }

    private func inspect(_ candidate: LiveCatalogCandidate) async {
        inspectionCount += 1
        do {
            let lease = try await authorizer.authorize(candidate, revision: candidate.revision)
            // Sharing may have stopped while the open was waiting; never start a worker after that.
            guard !stopped, !Task.isCancelled else { return }
            let inspection = try await inspector.inspect(lease)
            guard !stopped else { return }
            failures[candidate.id] = nil
            store(Self.record(for: candidate, inspection: inspection, at: now()))
        } catch let error as LiveSourceAuthorizationError {
            fail(candidate, reason: error.message)
        } catch is CancellationError {
            return
        } catch {
            fail(candidate, reason: "The Mac could not check this disc yet. It tries again in a few minutes.")
        }
    }

    private func fail(_ candidate: LiveCatalogCandidate, reason: String) {
        guard !stopped else { return }
        failures[candidate.id] = Failure(revision: candidate.revision, reason: reason, retryAt: now().addingTimeInterval(retryInterval))
    }

    private func store(_ record: Record) {
        records[record.id] = record
        if records.count > capacity {
            for stale in records.values.sorted(by: { $0.checkedAt < $1.checkedAt }).prefix(records.count - capacity) {
                records[stale.id] = nil
            }
        }
        guard let storeURL else { return }
        let ordered = records.values.sorted { $0.checkedAt < $1.checkedAt }
        guard let data = try? JSONEncoder().encode(Store(version: 1, records: ordered)) else { return }
        // Writes run in order, off this actor, so catalog and start queries never wait on disk.
        let previous = persistTask
        persistTask = Task {
            await previous?.value
            _ = await BoundedFileAccess.run {
                try? FileManager.default.createDirectory(at: storeURL.deletingLastPathComponent(), withIntermediateDirectories: true)
                try? data.write(to: storeURL, options: .atomic)
            }
        }
    }

    private func resumeIdleWaiters() {
        let waiters = idleWaiters
        idleWaiters.removeAll()
        for waiter in waiters { waiter.resume() }
    }

    static func record(for candidate: LiveCatalogCandidate, inspection: LiveTitleInspection, at date: Date) -> Record {
        let audio = inspection.audioStreams.prefix(MovieLibraryContract.maximumLiveAudio).enumerated().map { index, stream in
            SharedLiveAudio(
                pid: stream.pid,
                language: stream.language.flatMap { $0.isEmpty ? nil : $0 },
                label: audioLabel(codingType: stream.codingType, language: stream.language),
                isDefault: index == 0
            )
        }
        let eligible = inspection.eligible && inspection.playlist != nil && !audio.isEmpty
        return Record(
            id: candidate.id,
            revision: candidate.revision,
            eligible: eligible,
            reason: eligible ? nil : reasonText(inspection.unsupportedReason),
            playlist: eligible ? inspection.playlist : nil,
            durationSeconds: inspection.durationSeconds,
            audio: eligible ? Array(audio) : [],
            checkedAt: date
        )
    }

    static func audioLabel(codingType: Int, language: String?) -> String {
        let codec = switch codingType {
        case 0x80: "LPCM"
        case 0x81: "Dolby Digital"
        case 0x82: "DTS"
        case 0x83: "Dolby TrueHD"
        case 0x84, 0xA1: "Dolby Digital Plus"
        case 0x85: "DTS-HD High Resolution"
        case 0x86: "DTS-HD Master Audio"
        case 0xA2: "DTS-HD"
        default: "Audio"
        }
        guard let language, !language.isEmpty else { return codec }
        let name = Locale(identifier: "en_US").localizedString(forLanguageCode: language) ?? language.uppercased()
        return "\(name) \(codec)"
    }

    static func reasonText(_ code: String?) -> String {
        switch code {
        case "no_live_titles": "This disc has no 3D title that can play live."
        case "live_title_search_incomplete": "The Mac could not finish checking this disc's titles."
        case "unsupported_live_audio": "This disc's audio can't play live yet."
        case let code?: "This disc can't play live yet (\(code))."
        case nil: "This disc can't play live yet."
        }
    }
}

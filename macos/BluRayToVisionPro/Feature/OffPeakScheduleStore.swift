import Combine
import Foundation

struct OffPeakQueueSchedule: Codable, Equatable, Identifiable {
    let id: UUID
    let startAt: Date
    let endAt: Date
    let createdAt: Date

    init(id: UUID = UUID(), startAt: Date, endAt: Date, createdAt: Date = Date()) {
        self.id = id
        self.startAt = startAt
        self.endAt = endAt
        self.createdAt = createdAt
    }
}

enum OffPeakScheduleMissReason: String, Codable, Equatable {
    case appRelaunchedAfterStart
    case noRunnableItems
    case queueBecameActive
    case queuePersistenceFailed
    case windowEndedBeforeEvaluation

    var message: String {
        switch self {
        case .appRelaunchedAfterStart:
            "The scheduled window was missed because the app was reopened after it started."
        case .noRunnableItems:
            "The scheduled window opened, but no queued videos were available to start."
        case .queueBecameActive:
            "The scheduled window opened, but other work became active before the queue could start."
        case .queuePersistenceFailed:
            "The scheduled window opened, but the queue could not be updated safely."
        case .windowEndedBeforeEvaluation:
            "The scheduled window was missed because the Mac was asleep or unavailable until after it ended."
        }
    }
}

enum OffPeakScheduleOutcomeKind: String, Codable, Equatable {
    case started
    case missed
}

struct OffPeakScheduleOutcome: Codable, Equatable {
    let scheduleID: UUID
    let kind: OffPeakScheduleOutcomeKind
    let occurredAt: Date
    let missReason: OffPeakScheduleMissReason?

    static func started(scheduleID: UUID, at date: Date) -> OffPeakScheduleOutcome {
        OffPeakScheduleOutcome(scheduleID: scheduleID, kind: .started, occurredAt: date, missReason: nil)
    }

    static func missed(
        scheduleID: UUID,
        reason: OffPeakScheduleMissReason,
        at date: Date
    ) -> OffPeakScheduleOutcome {
        OffPeakScheduleOutcome(scheduleID: scheduleID, kind: .missed, occurredAt: date, missReason: reason)
    }

    var message: String {
        switch kind {
        case .started:
            "Scheduled queue start was consumed."
        case .missed:
            missReason?.message ?? "The scheduled window was missed."
        }
    }
}

struct OffPeakScheduleDocument: Codable, Equatable {
    static let currentVersion = 1

    let version: Int
    var schedule: OffPeakQueueSchedule?
    var lastOutcome: OffPeakScheduleOutcome?

    init(
        version: Int = currentVersion,
        schedule: OffPeakQueueSchedule? = nil,
        lastOutcome: OffPeakScheduleOutcome? = nil
    ) {
        self.version = version
        self.schedule = schedule
        self.lastOutcome = lastOutcome
    }

    private enum CodingKeys: String, CodingKey { case version, schedule, lastOutcome }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let version = try values.decode(Int.self, forKey: .version)
        guard version == Self.currentVersion else {
            throw OffPeakScheduleStoreError.unsupportedVersion(version)
        }
        self.init(
            version: version,
            schedule: try values.decodeIfPresent(OffPeakQueueSchedule.self, forKey: .schedule),
            lastOutcome: try values.decodeIfPresent(OffPeakScheduleOutcome.self, forKey: .lastOutcome)
        )
    }
}

enum OffPeakScheduleEvaluation: Equatable {
    case none
    case waiting(OffPeakQueueSchedule)
    case start(OffPeakQueueSchedule)
    case missed(OffPeakScheduleMissReason)
}

enum OffPeakScheduleStoreError: LocalizedError, Equatable {
    case invalidWindow
    case noEligibleItems
    case queueIsActive
    case unsupportedVersion(Int)
    case writesBlocked
    case writeFailed

    var errorDescription: String? {
        switch self {
        case .invalidWindow:
            "Choose a future start time and an end time after it."
        case .noEligibleItems:
            "Add or restore at least one runnable queue item before scheduling."
        case .queueIsActive:
            "Pause or finish the current queue before scheduling it."
        case let .unsupportedVersion(version):
            "Schedule data version \(version) is not supported."
        case .writesBlocked:
            "Schedule changes are disabled because its saved data could not be loaded safely."
        case .writeFailed:
            "The schedule could not be saved."
        }
    }
}

@MainActor
final class OffPeakScheduleStore: ObservableObject {
    @Published private(set) var document: OffPeakScheduleDocument
    @Published private(set) var loadErrorMessage: String?
    @Published private(set) var writesBlocked = false

    private let fileURL: URL?
    private let fileManager: FileManager
    private let dataReader: (URL) throws -> Data
    private let dataWriter: @Sendable (Data, URL) throws -> Void
    private let mutationLock = OffPeakScheduleMutationLock()

    init(
        fileURL: URL? = nil,
        fileManager: FileManager = .default,
        dataReader: @escaping (URL) throws -> Data = { try Data(contentsOf: $0) },
        dataWriter: @escaping @Sendable (Data, URL) throws -> Void = { data, url in
            try data.write(to: url, options: .atomic)
        },
        inMemory: Bool = false
    ) {
        self.fileManager = fileManager
        self.dataReader = dataReader
        self.dataWriter = dataWriter
        self.fileURL = inMemory ? nil : (fileURL ?? Self.defaultFileURL(fileManager: fileManager))
        document = OffPeakScheduleDocument()
        if let fileURL = self.fileURL {
            load(from: fileURL)
        }
    }

    static func inMemory() -> OffPeakScheduleStore {
        OffPeakScheduleStore(inMemory: true)
    }

    var schedule: OffPeakQueueSchedule? { document.schedule }
    var lastOutcome: OffPeakScheduleOutcome? { document.lastOutcome }

    func save(_ schedule: OffPeakQueueSchedule) async throws {
        guard schedule.endAt > schedule.startAt else {
            throw OffPeakScheduleStoreError.invalidWindow
        }
        try await mutate { document in
            document.schedule = schedule
            document.lastOutcome = nil
        }
    }

    func cancel() async throws {
        try await mutate { document in
            document.schedule = nil
            document.lastOutcome = nil
        }
    }

    func clearOutcome() async throws {
        try await mutate { document in
            document.lastOutcome = nil
        }
    }

    func evaluate(at now: Date, appLaunched: Bool) async throws -> OffPeakScheduleEvaluation {
        await mutationLock.lock()
        do {
            guard !writesBlocked else {
                throw OffPeakScheduleStoreError.writesBlocked
            }
            guard let schedule = document.schedule else {
                await mutationLock.unlock()
                return .none
            }
            guard now >= schedule.startAt else {
                await mutationLock.unlock()
                return .waiting(schedule)
            }

            let evaluation: OffPeakScheduleEvaluation
            if now >= schedule.endAt {
                evaluation = .missed(.windowEndedBeforeEvaluation)
            } else if appLaunched, now > schedule.startAt {
                evaluation = .missed(.appRelaunchedAfterStart)
            } else {
                evaluation = .start(schedule)
            }

            var nextDocument = document
            nextDocument.schedule = nil
            switch evaluation {
            case let .start(schedule):
                nextDocument.lastOutcome = .started(scheduleID: schedule.id, at: now)
            case let .missed(reason):
                nextDocument.lastOutcome = .missed(scheduleID: schedule.id, reason: reason, at: now)
            case .none, .waiting:
                await mutationLock.unlock()
                return evaluation
            }
            try persist(nextDocument)
            document = nextDocument
            await mutationLock.unlock()
            return evaluation
        } catch {
            await mutationLock.unlock()
            throw error
        }
    }

    func markStartedScheduleMissed(
        scheduleID: UUID,
        reason: OffPeakScheduleMissReason,
        at date: Date
    ) async throws {
        try await mutate { document in
            guard document.lastOutcome?.scheduleID == scheduleID,
                  document.lastOutcome?.kind == .started
            else {
                return
            }
            document.lastOutcome = .missed(
                scheduleID: scheduleID,
                reason: reason,
                at: date
            )
        }
    }

    private func mutate(_ mutation: (inout OffPeakScheduleDocument) throws -> Void) async throws {
        await mutationLock.lock()
        do {
            guard !writesBlocked else {
                throw OffPeakScheduleStoreError.writesBlocked
            }
            var nextDocument = document
            try mutation(&nextDocument)
            try persist(nextDocument)
            document = nextDocument
            await mutationLock.unlock()
        } catch {
            await mutationLock.unlock()
            throw error
        }
    }

    private func load(from fileURL: URL) {
        guard fileManager.fileExists(atPath: fileURL.path) else {
            return
        }
        do {
            document = try Self.decoder().decode(OffPeakScheduleDocument.self, from: dataReader(fileURL))
        } catch let error as OffPeakScheduleStoreError {
            writesBlocked = true
            loadErrorMessage = error.localizedDescription
        } catch {
            writesBlocked = true
            loadErrorMessage = "Schedule data could not be loaded. Scheduling is disabled to avoid overwriting it."
        }
    }

    private func persist(_ document: OffPeakScheduleDocument) throws {
        guard let fileURL else {
            return
        }
        do {
            try fileManager.createDirectory(
                at: fileURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try dataWriter(Self.encoder().encode(document), fileURL)
        } catch {
            throw OffPeakScheduleStoreError.writeFailed
        }
    }

    private static func defaultFileURL(fileManager: FileManager) -> URL {
        let applicationSupportURL = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        return applicationSupportURL
            .appendingPathComponent("3D Blu-ray to Vision Pro", isDirectory: true)
            .appendingPathComponent("queue.schedule.json")
    }

    private static func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }

    private static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}

private actor OffPeakScheduleMutationLock {
    private var locked = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func lock() async {
        guard locked else {
            locked = true
            return
        }
        await withCheckedContinuation { continuation in
            waiters.append(continuation)
        }
    }

    func unlock() {
        guard !waiters.isEmpty else {
            locked = false
            return
        }
        waiters.removeFirst().resume()
    }
}

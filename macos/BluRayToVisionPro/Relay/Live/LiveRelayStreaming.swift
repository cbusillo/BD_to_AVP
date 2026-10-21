import Darwin
import Foundation

/// The file a live stream reads, as the Mac observed it when it authorized the start.
struct LiveSourceIdentity: Equatable, Sendable {
    let device: UInt64
    let inode: UInt64
    let sizeBytes: Int64
    let modificationSeconds: Int64
    let modificationNanoseconds: Int64

    init(device: UInt64, inode: UInt64, sizeBytes: Int64, modificationSeconds: Int64, modificationNanoseconds: Int64) {
        self.device = device
        self.inode = inode
        self.sizeBytes = sizeBytes
        self.modificationSeconds = modificationSeconds
        self.modificationNanoseconds = modificationNanoseconds
    }

    init(_ info: stat) {
        self.init(
            device: UInt64(UInt32(bitPattern: info.st_dev)),
            inode: info.st_ino,
            sizeBytes: info.st_size,
            modificationSeconds: Int64(info.st_mtimespec.tv_sec),
            modificationNanoseconds: Int64(info.st_mtimespec.tv_nsec)
        )
    }
}

/// An authorized source. `retainer` keeps the descriptor and the approved root's scoped access
/// alive for the whole job.
struct LiveSourceLease: Sendable {
    let canonicalPath: String
    let identity: LiveSourceIdentity
    let retainer: AnyObject & Sendable
}

struct LiveStreamRequest: Sendable {
    let source: LiveSourceLease
    let playlist: Int
    let audioPID: Int
}

enum LiveRelayStreamState: Equatable, Sendable {
    case starting
    case live(earliestMs: Int64, latestMs: Int64)
    case complete(earliestMs: Int64, latestMs: Int64)
    case failed(code: String, message: String)
    case stopped(LiveRelayStopReason)
}

enum LiveRelayStopReason: String, Sendable {
    case viewerStopped
    case replaced
    case cancelled
    case sessionEnded
    case idle
    case sharingStopped
    case appQuit
    case sourceChanged
}

/// One live stream on the Mac.
protocol LiveRelayStreaming: Actor {
    nonisolated var id: UUID { get }
    func state() -> LiveRelayStreamState
    /// Unsigned response for playlist.m3u8, playlist.json, media/*, control/position, control/cancel.
    /// The caller has already verified the request against `session.liveStreamSession(streamID: id)`
    /// and signs the response with that same derived session.
    /// After stop or failure this returns 410 or 503 with a `RelayLiveStreamEnded` body.
    func relayResponse(for request: RelayHTTPRequest) async -> RelayHTTPResponse
    func renewLease()
    /// Returns after the worker exits and the workspace is removed. Idempotent: every caller
    /// awaits the same cleanup.
    func stop(_ reason: LiveRelayStopReason) async
}

protocol LiveRelayStreamStarting: Sendable {
    /// Contract: when the calling task is cancelled, `start` stops anything it already launched
    /// before it throws `CancellationError`. A stream that is returned is owned by the caller,
    /// which must stop it.
    func start(_ request: LiveStreamRequest, id: UUID) async throws -> any LiveRelayStreaming
}

extension RelayHTTPResponse {
    /// The unsigned response a stream returns from every relay route once it has ended or failed.
    static func liveStreamEnded(_ body: RelayLiveStreamEnded) -> RelayHTTPResponse {
        .json(body, statusCode: body.error.statusCode)
    }
}

/// The Mac host's single live slot.
///
/// - `empty`, `reserved(key, startTask)`, `running(key, stream)`, `draining(drainTask)`.
/// - Every release moves the slot to `draining`: replace, cancel by `startID`, forget, parent
///   session expiry, sharing shutdown, quit, and a late result arriving for a key that no longer
///   holds the slot.
/// - `drainTask` first cancels `startTask` and awaits it, then stops and awaits any stream that
///   `startTask` returned. Only then may the owner set the slot to `empty`.
/// - A start that finds the slot draining awaits `drainTask` and then rechecks the slot. A start
///   that finds the slot held by another session gets `busy`.
/// - A start first consults the start records (`LiveStartKey`): a known `startID` returns its
///   recorded outcome or `conflict` without touching the slot. A record stays while its start
///   holds the slot or drains, and expires as `LiveStartLimits` describes.
/// - Cancel by `startID` marks an admitted start's record cancelled, then drains the slot if that
///   start holds it. Ending a parent session drains its start and removes its records and
///   tombstones. For a `startID` never admitted it records a tombstone
///   `(parentSessionID, startID)` so a later start with that ID gets `cancelled`. A cancel that
///   matches a record or an existing tombstone is never limited; tombstones are never removed
///   before they expire (`LiveStartLimits`).
enum LiveStartAdmissionSlot {
    case empty
    case reserved(LiveStartKey, startTask: Task<any LiveRelayStreaming, Error>)
    case running(LiveStartKey, stream: any LiveRelayStreaming)
    case draining(Task<Void, Never>)

    var key: LiveStartKey? {
        switch self {
        case let .reserved(key, _), let .running(key, _):
            key
        case .empty, .draining:
            nil
        }
    }

    /// The task that releases this slot, or nil when it is already empty. Draining an already
    /// draining slot returns the same task, so every caller awaits one cleanup.
    func drainTask(reason: LiveRelayStopReason) -> Task<Void, Never>? {
        switch self {
        case .empty:
            return nil
        case let .draining(task):
            return task
        case let .reserved(_, startTask):
            return Task {
                startTask.cancel()
                if let stream = try? await startTask.value {
                    await stream.stop(reason)
                }
            }
        case let .running(_, stream):
            return Task {
                await stream.stop(reason)
            }
        }
    }
}

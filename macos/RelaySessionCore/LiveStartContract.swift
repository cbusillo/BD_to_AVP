import Foundation

/// Identifies one live start request.
///
/// The host keeps a start record `(parentSessionID, startID) -> (key, outcome)` for every start it
/// admits, and consults it before the slot: a retry with the same key gets the recorded outcome
/// (the original `streamID`, or `cancelled`) even after the start was replaced, released, or
/// failed, and a retry with different parameters gets `conflict`. Neither touches the slot.
/// Retention is described on `LiveStartLimits`.
struct LiveStartKey: Hashable, Sendable {
    let parentSessionID: RelaySessionIdentifier
    let startID: UUID
    let titleID: String
    let revision: String
    let audioPID: Int
}

enum LiveStartOutcome: Equatable, Sendable {
    case started(streamID: UUID)
    case busy
    case cancelled
    case conflict
    case sourceChanged
    case rateLimited
}

/// Result of `POST /movies/v1/live/cancel`.
///
/// - `acknowledged` (204) guarantees that no start with that `startID` is admitted for as long
///   as any request carrying it can still verify. For an admitted start, the host marks its
///   record cancelled before it drains a reserved or running slot.
/// - `retryLater` (signed 429 with a `LiveCancelRetryLater` body) gives no guarantee, and the
///   host records nothing. The client keeps retrying until `clientCancelDeadlineAfterStart`.
enum LiveCancelOutcome: Equatable, Sendable {
    case acknowledged
    case retryLater

    var statusCode: Int {
        switch self {
        case .acknowledged:
            204
        case .retryLater:
            429
        }
    }
}

struct LiveCancelRetryLater: Codable, Equatable, Sendable {
    let retryable: Bool
}

/// Admission bounds for live starts and cancels on the Mac host.
///
/// Start records:
/// - Only an admitted start creates a record, so records are bounded by
///   `startsPerSessionPerMinute`.
/// - A record is never removed while its start holds the slot or drains. Otherwise it expires
///   `clientCancelDeadlineAfterStart` after the latest of its admission, its first cancellation,
///   and the end of its slot ownership (drain included). That outlasts a signed request's default
///   freshness (30 s maximum age + 5 s future skew), so no request that could reach a removed
///   record can still verify.
/// - Cancelling an already cancelled record does not extend it. A first cancellation can only
///   come before expiry, so a record outlives its slot ownership by at most 80 s.
/// - Records and tombstones of a parent session that has ended are removed with it, since none
///   of its requests can verify.
/// - Bound: one start holds the slot, and every other live record left the slot within the last
///   80 s, so it was admitted then. That gives at most 1 + 12 records per session and
///   1 + 4 x 12 = 49 overall for the four parent sessions a host allows.
///   `startRecordCapacity` is a defensive cap above that. At the cap a new start gets
///   `rateLimited` and nothing is recorded. No record is ever removed early.
///
/// Cancels:
/// - A cancel that matches a start record, or repeats an existing tombstone, is never limited.
/// - Only a cancel for a `startID` the host has never admitted creates a tombstone, and only it
///   counts against `tombstoneCancelsPerSessionPerMinute` and `tombstoneCapacity`. A refused
///   cancel gets `retryLater`.
/// - A tombstone lives exactly `tombstoneLifetime` and is never removed before that.
enum LiveStartLimits {
    /// Longer than a signed request's default maximum age plus future skew (30 s + 5 s), so a
    /// delayed start can no longer verify once its tombstone expires.
    static let tombstoneLifetime: TimeInterval = 60
    static let startsPerSessionPerMinute = 6
    static let tombstoneCancelsPerSessionPerMinute = 6
    static let tombstoneCapacity = 32
    static let clientCancelDeadlineAfterStart: TimeInterval = 40
    static let startRecordCapacity = 64
}

enum LiveStreamIdentifier {
    /// A fresh stream ID. It never equals a live parent session ID, because the stream ID is also
    /// the derived stream session's ID.
    static func make(
        excluding liveParentSessionIDs: Set<RelaySessionIdentifier>,
        generate: () -> UUID = UUID.init
    ) -> UUID {
        while true {
            let candidate = generate()
            guard let candidateSessionID = try? RelaySessionIdentifier(rawValue: candidate.uuidString),
                  liveParentSessionIDs.contains(candidateSessionID)
            else {
                return candidate
            }
        }
    }
}

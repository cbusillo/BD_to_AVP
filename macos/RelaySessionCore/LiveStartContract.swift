import Foundation

/// Identifies one live start request.
///
/// The host remembers each admitted start's outcome by `(parentSessionID, startID)` for
/// `LiveStartLimits.tombstoneLifetime` after it was admitted or cancelled, and for as long as it
/// holds the slot. It consults that record before the slot: a retry with the same key gets the
/// recorded outcome (the original `streamID`, or `cancelled`) even after the start was replaced,
/// released, or failed, and a retry with different parameters gets `conflict`. Neither touches
/// the slot.
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
/// - `acknowledged` (204) guarantees that a start with that `startID` never runs again. For an
///   admitted start, the host records the cancellation before it drains a reserved or running
///   slot, so a retry within the record's lifetime gets `cancelled`.
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
/// - A cancel that matches an admitted start's record (reserved, running, or already ended), or
///   repeats an existing tombstone, is never limited. Those records are bounded by
///   `startsPerSessionPerMinute`, not by the tombstone table.
/// - Only a cancel for a `startID` the host has never admitted creates a tombstone, and only it
///   counts against
///   `tombstoneCancelsPerSessionPerMinute` and `tombstoneCapacity`; a refused one gets
///   `retryLater`.
/// - A tombstone lives exactly `tombstoneLifetime` and is never removed before that.
enum LiveStartLimits {
    /// Longer than a signed request's default maximum age plus future skew (30 s + 5 s), so a
    /// delayed start can no longer verify once its tombstone expires.
    static let tombstoneLifetime: TimeInterval = 60
    static let startsPerSessionPerMinute = 6
    static let tombstoneCancelsPerSessionPerMinute = 6
    static let tombstoneCapacity = 32
    static let clientCancelDeadlineAfterStart: TimeInterval = 40
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

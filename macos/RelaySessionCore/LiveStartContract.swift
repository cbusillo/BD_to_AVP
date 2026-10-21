import Foundation

/// Identifies one live start request. A repeated start with the same key is the same start;
/// the same `startID` with different parameters is a conflict.
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
/// - `acknowledged` (204) guarantees that a start with that `startID` never runs. A start that is
///   already reserved or running is drained.
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
/// - A cancel that matches a reserved or running start, or repeats an existing tombstone, is
///   never limited.
/// - Only a cancel that would create a new tombstone counts against
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

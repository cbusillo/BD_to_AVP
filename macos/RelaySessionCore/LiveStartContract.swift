import Foundation

/// Identifies one live start request.
///
/// The client sets `issuedAtUnixMilliseconds` once, to the signed timestamp of the first start
/// request, and sends it unchanged on every retry and cancel for that `startID`. A start
/// identity is `(parentSessionID, startID, issuedAtUnixMilliseconds)`.
///
/// The host keeps a start record `identity -> (key, outcome)` for every start it admits, and
/// consults it before the slot: a retry with the same key gets the recorded outcome (the original
/// `streamID`, or `cancelled`) even after the start was replaced, released, or failed, and a retry
/// with different parameters gets `conflict`. Neither touches the slot. Retention and the signing
/// deadline are described on `LiveStartLimits`.
struct LiveStartKey: Hashable, Sendable {
    let parentSessionID: RelaySessionIdentifier
    let startID: UUID
    let issuedAtUnixMilliseconds: Int64
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
    /// The request was signed outside its start identity's signing window. Not retryable.
    case expired
}

/// Result of `POST /movies/v1/live/cancel`.
///
/// - `acknowledged` (204) guarantees that no start with that identity is admitted afterward. The
///   host keeps the record or tombstone until no request for the identity can verify any more,
///   and refuses every request for it signed outside its signing window. For an admitted start,
///   the record is marked cancelled before the host drains a reserved or running slot.
/// - `retryLater` (signed 429, `LiveCancelRefusal(retryable: true)`) gives no guarantee, and the
///   host records nothing. The client keeps retrying within the signing window.
/// - `expired` (signed 422, `LiveCancelRefusal(retryable: false)`) means the cancel was signed
///   outside the signing window. The host records nothing.
enum LiveCancelOutcome: Equatable, Sendable {
    case acknowledged
    case retryLater
    case expired

    var statusCode: Int {
        switch self {
        case .acknowledged:
            204
        case .retryLater:
            429
        case .expired:
            422
        }
    }
}

struct LiveCancelRefusal: Codable, Equatable, Sendable {
    let retryable: Bool
}

/// Admission bounds for live starts and cancels on the Mac host.
///
/// Signing window:
/// - The host refuses statelessly, with `expired`, any start or cancel whose signed request
///   timestamp is later than `issuedAt + clientCancelDeadlineAfterStart` or earlier than
///   `issuedAt` minus the policy's allowed future skew (`acceptsRequest`).
/// - So no request for an identity can verify after `issuedAt + clientCancelDeadlineAfterStart +`
///   the policy's maximum age (`lastVerifiableUnixMilliseconds`), 70 s with the default policy.
///
/// Start records:
/// - Only an admitted start creates a record. A record is never removed while its start holds
///   the slot or drains. Otherwise it is removed at the later of its last verifiable time and the
///   end of its slot ownership (drain included). Cancels never extend it.
/// - Records and tombstones of a parent session that has ended are removed with it. The session
///   is marked ended before any cleanup is awaited; a start or cancel that already passed
///   verification checks that mark after every suspension and then records and admits nothing.
/// - Bound: a record outside the slot was admitted after `issuedAt - 2 x skew` and lives until
///   `issuedAt + 70 s`, so it was admitted within the last 80 s. With `startsPerSessionPerMinute`,
///   that is at most 12 such records per session, plus the one start in the slot: 13 per session
///   and 1 + 4 x 12 = 49 overall for the four parent sessions a host allows.
///   `startRecordCapacity` is a defensive cap above that. At the cap a new start gets
///   `rateLimited` and nothing is recorded. No record is ever removed early.
///
/// Cancels and tombstones:
/// - A cancel that matches a start record, or repeats an existing tombstone, is never limited.
/// - Only a cancel for an identity the host has never admitted creates a tombstone. A tombstone
///   lives until the identity's last verifiable time. Each session holds at most
///   `tombstonesPerSession` and the host at most `tombstoneCapacity`; a cancel that would exceed
///   either gets `retryLater`. No tombstone is removed early.
enum LiveStartLimits {
    static let startsPerSessionPerMinute = 6
    static let tombstonesPerSession = 6
    static let tombstoneCapacity = 32
    static let clientCancelDeadlineAfterStart: TimeInterval = 40
    static let startRecordCapacity = 64

    /// Whether a request signed at `signedAt` falls inside the signing window of a start issued
    /// at `issuedAt`.
    static func acceptsRequest(
        signedAtUnixMilliseconds signedAt: Int64,
        issuedAtUnixMilliseconds issuedAt: Int64,
        policy: RelayRequestValidationPolicy
    ) -> Bool {
        guard let latest = RelayTime.adding(deadlineMilliseconds, to: issuedAt) else { return false }
        let earliest = issuedAt.subtractingReportingOverflow(policy.allowedFutureSkewMilliseconds)
        return !earliest.overflow && signedAt >= earliest.partialValue && signedAt <= latest
    }

    /// The last moment any request for a start issued at `issuedAt` can verify under `policy`.
    static func lastVerifiableUnixMilliseconds(
        issuedAtUnixMilliseconds issuedAt: Int64,
        policy: RelayRequestValidationPolicy
    ) -> Int64? {
        RelayTime.adding(deadlineMilliseconds + policy.maximumAgeMilliseconds, to: issuedAt)
    }

    private static var deadlineMilliseconds: Int64 {
        Int64(clientCancelDeadlineAfterStart * 1_000)
    }
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

import Foundation

/// The Mac's live routes as the player uses them. `MovieLibraryClient` is the real one; tests
/// supply a fake Mac.
protocol LiveMacClient: Actor {
    func startLive(_ request: LiveStartRequest, signedAt: Date) async throws -> LiveStartResult
    func cancelLive(_ reference: LiveStartReference) async throws -> LiveCancelOutcome
    func releaseLive(_ reference: LiveStartReference) async throws -> Bool
    func liveStatus(streamID: UUID) async throws -> LiveStreamStatus
    func liveStreamSession(streamID: UUID) throws -> (session: RelayEstablishedSession, baseURL: URL)
}

/// A paired Mac's movie library connection: its catalog plus its live routes.
protocol MovieLibraryConnection: LiveMacClient {
    func catalog() async throws -> SharedMovieCatalog
}

extension MovieLibraryClient: MovieLibraryConnection {}

/// Time for live startup and cleanup, injectable so tests can run minutes of schedule at once.
protocol LiveClock: Sendable {
    func now() -> Date
    func sleep(seconds: TimeInterval) async throws
}

struct SystemLiveClock: LiveClock {
    func now() -> Date { Date() }
    func sleep(seconds: TimeInterval) async throws {
        try await Task.sleep(for: .milliseconds(Int64((seconds * 1_000).rounded())))
    }
}

enum LiveStartTiming {
    /// The player gives up on a start this long after sending it.
    static let clientTimeout: TimeInterval = 120
    static let statusPollInterval: TimeInterval = 1
    /// Start retries after a lost response stay well inside the start-ID signing window.
    static let startRetryWindow: TimeInterval = 30
    /// The start-ID cancel is accepted only while signed within this of `issuedAt`.
    static let startIDCancelWindow: TimeInterval = LiveStartLimits.clientCancelDeadlineAfterStart
    /// A start can still be admitted until `issuedAt + 70 s`; `release` is retried past that.
    static let releaseUntil: TimeInterval = 75
    /// How long the derived stream cancel is retried before the Mac's idle lease takes over.
    static let streamCancelRetryWindow: TimeInterval = 120
    /// Retry delays: 0.5, 1, 2, 4 s, then every 4 s.
    static func backoff(attempt: Int) -> TimeInterval {
        min(0.5 * pow(2, Double(max(0, attempt))), 4)
    }
}

extension Date {
    init(unixMilliseconds: Int64) {
        self.init(timeIntervalSince1970: TimeInterval(unixMilliseconds) / 1_000)
    }

    var unixMilliseconds: Int64 {
        RelayTime.unixMilliseconds(for: self) ?? Int64(timeIntervalSince1970 * 1_000)
    }
}

/// How a live start's cleanup reached the Mac.
enum LiveCleanupRoute: Equatable, Sendable {
    /// Route 1: `POST /relay/v1/control/cancel`, signed with the derived stream session.
    case streamCancel
    /// Route 2: `POST /movies/v1/live/cancel` by start ID, within `issuedAt + 40 s`.
    case startIDCancel
    /// Route 3: `POST /movies/v1/live/release`, retried until `issuedAt + 75 s`.
    case release
}

enum LiveCleanupResult: Equatable, Sendable {
    /// The Mac confirmed the stream or start is gone.
    case done(LiveCleanupRoute)
    /// Nothing confirmed it; the stream's idle lease on the Mac is the backstop.
    case unconfirmed
    /// The parent session is gone, which ends every stream it started.
    case sessionRevoked
}

/// Stops a live start or stream on the Mac by the route the player's knowledge allows.
/// It runs detached from whatever started it, and every attempt is freshly signed.
struct LiveStreamCleanup: Sendable {
    let reference: LiveStartReference
    let streamID: UUID?
    let client: any LiveMacClient
    let relayTransport: any RelayTransport
    let clock: any LiveClock

    func run() async -> LiveCleanupResult {
        if let streamID {
            return await cancelStream(streamID)
        }
        let issuedAt = Date(unixMilliseconds: reference.issuedAt)
        var attempt = 0
        while clock.now() <= issuedAt.addingTimeInterval(LiveStartTiming.startIDCancelWindow) {
            do {
                switch try await client.cancelLive(reference) {
                case .acknowledged:
                    return .done(.startIDCancel)
                case .expired:
                    return await release()
                case .retryLater:
                    break
                }
            } catch MovieLibraryError.needsPairing {
                return .sessionRevoked
            } catch {}
            try? await clock.sleep(seconds: LiveStartTiming.backoff(attempt: attempt))
            attempt += 1
        }
        return await release()
    }

    private func release() async -> LiveCleanupResult {
        let until = Date(unixMilliseconds: reference.issuedAt).addingTimeInterval(LiveStartTiming.releaseUntil)
        var attempt = 0
        while clock.now() <= until {
            do {
                if try await client.releaseLive(reference) { return .done(.release) }
            } catch MovieLibraryError.needsPairing {
                return .sessionRevoked
            } catch {}
            try? await clock.sleep(seconds: LiveStartTiming.backoff(attempt: attempt))
            attempt += 1
        }
        return .unconfirmed
    }

    private func cancelStream(_ streamID: UUID) async -> LiveCleanupResult {
        guard let stream = try? await client.liveStreamSession(streamID: streamID) else { return .sessionRevoked }
        let deadline = clock.now().addingTimeInterval(LiveStartTiming.streamCancelRetryWindow)
        let clock = clock
        var attempt = 0
        while clock.now() <= deadline {
            do {
                try await RelayRemotePlaybackSource.signedPost(
                    path: RelayWireContract.cancelPath,
                    body: Data(),
                    session: stream.session,
                    serverBaseURL: stream.baseURL,
                    transport: relayTransport,
                    clock: { clock.now() }
                )
                return .done(.streamCancel)
            } catch RelayTransportError.streamEnded {
                // A signed stream-ended answer means the stream is already gone.
                return .done(.streamCancel)
            } catch RelayTransportError.sessionRevoked {
                return .sessionRevoked
            } catch {}
            try? await clock.sleep(seconds: LiveStartTiming.backoff(attempt: attempt))
            attempt += 1
        }
        return .unconfirmed
    }
}

/// A live stream that reached `live` on the Mac and is ready for the player.
struct LiveStartReady: Sendable {
    let streamID: UUID
    let configuration: RelayRemotePlaybackConfiguration
}

enum LiveStartFailure: Error, Equatable, Sendable {
    case refused(String)
    case failed(String)
    case timedOut
    case needsPairing
    case cancelled

    var message: String {
        switch self {
        case let .refused(message), let .failed(message): message
        case .timedOut: "The Mac did not start the live stream within two minutes. Try again."
        case .needsPairing: MovieLibraryError.needsPairing.localizedDescription
        case .cancelled: "Live playback was cancelled."
        }
    }
}

/// One live start, from the start request until the stream is live, and its cleanup.
///
/// The start ID and `issuedAt` exist before the start is sent, so cleanup is possible at any
/// point. Cleanup runs once, detached, by the route `LiveStreamCleanup` picks from what the
/// operation knows: the stream ID once a start response or status gave it, otherwise the
/// start ID while it can still be cancelled, otherwise `release`.
@MainActor
final class LiveStartOperation {
    let title: SharedLiveTitle
    let reference: LiveStartReference
    private let request: LiveStartRequest
    private let client: any LiveMacClient
    private let relayTransport: any RelayTransport
    private let clock: any LiveClock
    private let onStatus: @MainActor (LiveStreamStatus) -> Void
    private(set) var streamID: UUID?
    private(set) var cleanupTask: Task<LiveCleanupResult, Never>?

    init(
        title: SharedLiveTitle,
        audioPID: Int,
        client: any LiveMacClient,
        relayTransport: any RelayTransport,
        clock: any LiveClock,
        onStatus: @escaping @MainActor (LiveStreamStatus) -> Void = { _ in }
    ) {
        self.title = title
        self.client = client
        self.relayTransport = relayTransport
        self.clock = clock
        self.onStatus = onStatus
        let issuedAt = clock.now().unixMilliseconds
        reference = LiveStartReference(startID: UUID(), issuedAt: issuedAt)
        request = LiveStartRequest(
            startID: reference.startID, issuedAt: issuedAt, titleID: title.id,
            revision: title.revision, audioPID: audioPID
        )
    }

    var issuedAt: Date { Date(unixMilliseconds: reference.issuedAt) }

    /// Starts the stream and waits until it is live. Every failure, cancellation, and timeout
    /// after a start may have been admitted begins cleanup before returning.
    func run() async -> Result<LiveStartReady, LiveStartFailure> {
        let result = await start()
        if case .failure = result, cleanupTask == nil, mayHaveBeenAdmitted {
            cleanUp()
        }
        return result
    }

    /// Begins cleanup once and returns its task. Safe to call from any owner at any time.
    @discardableResult
    func cleanUp() -> Task<LiveCleanupResult, Never> {
        if let cleanupTask { return cleanupTask }
        let cleanup = LiveStreamCleanup(
            reference: reference, streamID: streamID, client: client,
            relayTransport: relayTransport, clock: clock
        )
        let task = Task.detached { await cleanup.run() }
        cleanupTask = task
        return task
    }

    private var sentStart = false
    private var refusedCleanly = false
    private var mayHaveBeenAdmitted: Bool { sentStart && !refusedCleanly }

    private func start() async -> Result<LiveStartReady, LiveStartFailure> {
        var attempt = 0
        var lostResponse = false
        startLoop: while true {
            guard !Task.isCancelled, cleanupTask == nil else { return .failure(.cancelled) }
            // The first request is signed at `issuedAt`; a retry repeats the body.
            let signedAt = attempt == 0 ? issuedAt : clock.now()
            sentStart = true
            do {
                switch try await client.startLive(request, signedAt: signedAt) {
                case let .started(response):
                    streamID = response.streamID
                    break startLoop
                case let .refused(refusal):
                    // A start ID's recorded outcome answers every retry, so a refusal means
                    // this identity was not admitted, unless an earlier answer was lost.
                    refusedCleanly = !lostResponse
                    return .failure(.refused(Self.message(for: refusal)))
                case .expired:
                    return .failure(.failed("The Mac refused the live start because it arrived too late. Try again."))
                }
            } catch MovieLibraryError.needsPairing {
                refusedCleanly = true
                return .failure(.needsPairing)
            } catch {
                if Task.isCancelled { return .failure(.cancelled) }
                lostResponse = true
                guard clock.now() < issuedAt.addingTimeInterval(LiveStartTiming.startRetryWindow) else {
                    return .failure(.failed("The Mac did not answer the live start. Check the Mac and try again."))
                }
                try? await clock.sleep(seconds: LiveStartTiming.backoff(attempt: attempt))
                attempt += 1
            }
        }
        guard let streamID else { return .failure(.cancelled) }
        let deadline = issuedAt.addingTimeInterval(LiveStartTiming.clientTimeout)
        while true {
            guard !Task.isCancelled, cleanupTask == nil else { return .failure(.cancelled) }
            let polled: LiveStreamStatus?
            do {
                polled = try await client.liveStatus(streamID: streamID)
            } catch MovieLibraryError.needsPairing {
                // The parent session is gone: the Mac already ended its stream, so there is
                // nothing to clean up and nothing to wait for.
                refusedCleanly = true
                return .failure(.needsPairing)
            } catch {
                // A transient failure; keep polling until the client timeout.
                polled = nil
            }
            if let status = polled {
                guard !Task.isCancelled, cleanupTask == nil else { return .failure(.cancelled) }
                onStatus(status)
                switch status.state {
                case .live, .complete:
                    do {
                        let stream = try await client.liveStreamSession(streamID: streamID)
                        return .success(LiveStartReady(
                            streamID: streamID,
                            configuration: RelayRemotePlaybackConfiguration(
                                session: stream.session,
                                serverBaseURL: stream.baseURL,
                                serverName: title.title,
                                transport: relayTransport
                            )
                        ))
                    } catch {
                        return .failure(.needsPairing)
                    }
                case .failed:
                    return .failure(.failed(
                        "Live playback stopped on the Mac: \(status.failure?.message ?? "the stream failed.")"
                    ))
                case .stopped, .unknown:
                    let reason = MVHEVCPlayerSession.liveStopDescription(status.stopReason ?? RelayLiveStreamEnded.notCurrentCode)
                    return .failure(.failed("Live playback stopped on the Mac: \(reason)"))
                case .starting:
                    break
                }
            }
            guard clock.now() < deadline else { return .failure(.timedOut) }
            try? await clock.sleep(seconds: LiveStartTiming.statusPollInterval)
        }
    }

    static func message(for refusal: LiveStartRefusal) -> String {
        if let message = refusal.message, !message.isEmpty { return message }
        switch refusal.outcome {
        case .busy: return "The Mac is already streaming to another device."
        case .cancelled: return "This live start was cancelled."
        case .conflict: return "The Mac has a different start with this ID. Try again."
        case .sourceChanged: return "The disc image changed or is no longer available on the Mac. Refresh and try again."
        case .rateLimited: return "Too many live starts. Wait a minute, then try again."
        }
    }
}

/// Owns cleanup of a live stream that the player installed. Done and teardown stop the stream
/// with its own derived `control/cancel`, which works after any length of playback.
@MainActor
struct LivePlaybackControl {
    let operation: LiveStartOperation

    var streamID: UUID? { operation.streamID }

    @discardableResult
    func stop() -> Task<LiveCleanupResult, Never> {
        operation.cleanUp()
    }
}

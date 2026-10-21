import AVFoundation
import Foundation

enum RelayRetainedSeekDecision: Equatable {
    case playable
    case beforeRetainedHistory(earliestPlayableTime: TimeInterval)
    case notYetAvailable(latestAvailableTime: TimeInterval)
    case ended(finalDuration: TimeInterval)
    case invalidTime
}

struct RelayRetainedSeekPolicy: Equatable, Sendable {
    private(set) var window: RelayPlaylistSnapshot?

    mutating func update(with snapshot: RelayPlaylistSnapshot) {
        window = snapshot
    }

    var earliestPlayableTime: TimeInterval {
        TimeInterval(window?.earliestPlayableTimeMilliseconds ?? 0) / 1_000
    }

    var latestAvailableTime: TimeInterval {
        TimeInterval(window?.totalDurationMilliseconds ?? 0) / 1_000
    }

    func validateSeek(to time: TimeInterval) -> RelayRetainedSeekDecision {
        guard let milliseconds = RelayTime.milliseconds(
            from: time,
            maximum: RelayPlaylistLimits.maximumTimelineDurationMilliseconds
        ) else {
            return .invalidTime
        }
        guard let window else { return .notYetAvailable(latestAvailableTime: 0) }
        if milliseconds < window.earliestPlayableTimeMilliseconds {
            return .beforeRetainedHistory(
                earliestPlayableTime: TimeInterval(window.earliestPlayableTimeMilliseconds) / 1_000
            )
        }
        if milliseconds >= window.totalDurationMilliseconds {
            if window.isFinalized {
                return .ended(finalDuration: TimeInterval(window.totalDurationMilliseconds) / 1_000)
            }
            return .notYetAvailable(latestAvailableTime: TimeInterval(window.totalDurationMilliseconds) / 1_000)
        }
        return .playable
    }
}

struct RelayRemotePlaybackSource {
    let session: RelayEstablishedSession
    let serverBaseURL: URL
    private(set) var playlist: RelayEventPlaylist
    private var loader: RelayLoopbackHTTPServer?
    private(set) var retainedSeekPolicy = RelayRetainedSeekPolicy()

    init(
        session: RelayEstablishedSession,
        serverBaseURL: URL,
        targetDuration: TimeInterval = 10,
        retainedSegmentLimit: Int = 300
    ) throws {
        self.session = session
        self.serverBaseURL = serverBaseURL
        playlist = try RelayEventPlaylist(
            targetDuration: targetDuration,
            retainedSegmentLimit: retainedSegmentLimit
        )
    }

    @discardableResult
    mutating func append(resourceIdentifier: String, duration: TimeInterval) throws -> RelayPlaylistSegment {
        try playlist.append(resourceIdentifier: resourceIdentifier, duration: duration)
    }

    mutating func finalize() {
        playlist.finalize()
    }

    func validateSeek(to time: TimeInterval) -> RelayPlaylistSeekValidation {
        playlist.validateSeek(to: time)
    }

    mutating func updateRetainedWindow(with data: Data) throws {
        retainedSeekPolicy.update(with: try JSONDecoder().decode(RelayPlaylistSnapshot.self, from: data))
    }

    mutating func refreshRetainedWindow(
        transport: any RelayTransport,
        clock: @escaping @Sendable () -> Date = { Date() },
        nonce: @escaping @Sendable () -> String = { UUID().uuidString }
    ) async throws {
        let request = try RelayAuthenticatedRequestFactory.makeRequest(
            baseURL: serverBaseURL,
            path: RelayWireContract.playlistSnapshotPath,
            signer: session,
            clock: clock,
            nonce: nonce
        )
        let (data, response) = try await transport.data(for: request.request)
        try RelayAuthenticatedResponseVerifier.verify(
            data: data,
            response: response,
            request: request,
            signer: session,
            now: clock()
        )
        switch response.statusCode {
        case 200:
            try updateRetainedWindow(with: data)
        case 410, 503:
            try Self.throwIfStreamEnded(data)
            throw response.statusCode == 410 ? RelayTransportError.sessionExpired : RelayTransportError.unpaired
        default:
            throw RelayTransportError.unexpectedStatusCode(response.statusCode)
        }
    }

    /// A verified 410 or 503 whose body is `RelayLiveStreamEnded` means the live stream ended.
    /// An empty body keeps the fixture relay's session mapping.
    static func throwIfStreamEnded(_ data: Data) throws {
        guard !data.isEmpty, let ended = try? JSONDecoder().decode(RelayLiveStreamEnded.self, from: data) else { return }
        throw RelayTransportError.streamEnded(code: ended.code ?? ended.error.rawValue, message: ended.message)
    }

    /// Reports the playhead of a live stream. Signed with this source's (stream) session.
    func reportPosition(
        milliseconds: Int64,
        transport: any RelayTransport,
        clock: @escaping @Sendable () -> Date = { Date() },
        nonce: @escaping @Sendable () -> String = { UUID().uuidString }
    ) async throws {
        try await Self.signedPost(
            path: RelayWireContract.positionPath,
            body: JSONEncoder().encode(RelayViewerPosition(positionMilliseconds: max(0, milliseconds))),
            session: session, serverBaseURL: serverBaseURL, transport: transport, clock: clock, nonce: nonce
        )
    }

    /// Sends a signed relay POST and maps the verified answer. 204 succeeds; a signed
    /// `RelayLiveStreamEnded` throws `streamEnded`.
    static func signedPost(
        path: String,
        body: Data,
        session: RelayEstablishedSession,
        serverBaseURL: URL,
        transport: any RelayTransport,
        clock: @escaping @Sendable () -> Date = { Date() },
        nonce: @escaping @Sendable () -> String = { UUID().uuidString }
    ) async throws {
        let request = try RelayAuthenticatedRequestFactory.makeRequest(
            baseURL: serverBaseURL,
            path: path,
            method: "POST",
            body: body,
            signer: session,
            clock: clock,
            nonce: nonce
        )
        let (data, response) = try await transport.data(for: request.request)
        try RelayAuthenticatedResponseVerifier.verify(
            data: data,
            response: response,
            request: request,
            signer: session,
            now: clock()
        )
        switch response.statusCode {
        case 200, 204:
            return
        case 410, 503:
            try throwIfStreamEnded(data)
            throw RelayTransportError.unexpectedStatusCode(response.statusCode)
        default:
            throw RelayTransportError.unexpectedStatusCode(response.statusCode)
        }
    }

    func resolveSegmentURL(for segment: RelayPlaylistSegment) -> URL {
        RelayHLSResourceLoader.customPlaylistURL(for: serverBaseURL)?
            .deletingLastPathComponent()
            .appendingPathComponent("media/\(segment.resourceIdentifier)")
            ?? serverBaseURL
    }

    mutating func makeAssetAndLoader(
        transport: any RelayTransport,
        clock: @escaping @Sendable () -> Date = { Date() },
        nonce: @escaping @Sendable () -> String = { UUID().uuidString }
    ) async throws -> (AVURLAsset, RelayLoopbackHTTPServer) {
        cancelLoader()
        let client = RelayAuthenticatedResourceClient(
            signer: session,
            transport: transport,
            serverBaseURL: serverBaseURL,
            clock: clock,
            nonce: nonce
        )
        let (newLoader, assetURL) = try await RelayLoopbackHTTPServer.start(serverBaseURL: serverBaseURL) {
            try await client.load($0)
        }
        loader = newLoader

        let asset = AVURLAsset(url: assetURL)
        return (asset, newLoader)
    }

    mutating func cancelLoader() {
        loader?.cancelAllRequests()
        loader = nil
    }
}

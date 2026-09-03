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
        guard time.isFinite, time >= 0 else { return .invalidTime }
        guard let window else { return .notYetAvailable(latestAvailableTime: 0) }
        let milliseconds = Int64(time * 1_000)
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
    private var loader: RelayHLSResourceLoader?
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
        let (data, response) = try await transport.data(for: request)
        switch response.statusCode {
        case 200:
            try updateRetainedWindow(with: data)
        case 410:
            throw RelayTransportError.sessionExpired
        case 503:
            throw RelayTransportError.unpaired
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
    ) -> (AVURLAsset, RelayHLSResourceLoader) {
        let assetURL = RelayHLSResourceLoader.customPlaylistURL(for: serverBaseURL) ?? serverBaseURL

        let newLoader = RelayHLSResourceLoader(
            signer: session,
            transport: transport,
            serverBaseURL: serverBaseURL,
            clock: clock,
            nonce: nonce
        )
        loader = newLoader

        let asset = AVURLAsset(url: assetURL)
        asset.resourceLoader.setDelegate(newLoader, queue: .global(qos: .userInitiated))
        return (asset, newLoader)
    }

    mutating func cancelLoader() {
        loader?.cancelAllRequests()
        loader = nil
    }
}

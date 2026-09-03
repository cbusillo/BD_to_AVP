import Foundation

enum RelayHostLifecycle: String, Codable, Equatable, Sendable {
    case pairing
    case paired
    case finished
    case cancelled
    case expired
    case stopped
}

enum RelayHostStopReason: Sendable, Equatable {
    case cancelled
    case networkLoss
    case appQuit
    case stopped
}

enum RelayHostPeer: Sendable, Equatable {
    case localNetwork
    case loopback
    case nonLocal

    var isAllowed: Bool {
        self == .localNetwork
    }
}

enum RelayHostError: Error, Equatable, Sendable {
    case invalidFixtureDirectory
    case unavailable
    case expired
    case notPaired
    case invalidResourceIdentifier
    case resourceNotFound
    case resourceTooLarge
}

struct RelayHostConfiguration: Sendable, Equatable {
    let fixtureDirectory: URL
    let initializationResourceIdentifier: String
    let playlistTargetDuration: TimeInterval
    let retainedSegmentLimit: Int
    let maximumMediaBytes: Int
    let requestLimits: RelayHTTPParsingLimits
    let requestValidationPolicy: RelayRequestValidationPolicy

    init(
        fixtureDirectory: URL,
        initializationResourceIdentifier: String = "init.mp4",
        playlistTargetDuration: TimeInterval = 6,
        retainedSegmentLimit: Int = 12,
        maximumMediaBytes: Int = 64 * 1_024 * 1_024,
        requestLimits: RelayHTTPParsingLimits = .default,
        requestValidationPolicy: RelayRequestValidationPolicy? = nil
    ) throws {
        self.fixtureDirectory = fixtureDirectory
        self.initializationResourceIdentifier = initializationResourceIdentifier
        self.playlistTargetDuration = playlistTargetDuration
        self.retainedSegmentLimit = retainedSegmentLimit
        self.maximumMediaBytes = min(max(maximumMediaBytes, 1), 512 * 1_024 * 1_024)
        self.requestLimits = requestLimits
        self.requestValidationPolicy = try requestValidationPolicy ?? RelayRequestValidationPolicy()
    }
}

struct RelayBonjourAdvertisement: Sendable, Equatable {
    let serviceType: String
    let txtRecord: Data

    init(sessionID: RelaySessionIdentifier) {
        serviceType = RelayWireContract.bonjourServiceType
        txtRecord = NetService.data(fromTXTRecord: [
            "sid": Data(sessionID.rawValue.prefix(8).utf8),
            "v": Data(String(RelayWireContract.protocolVersion).utf8),
        ])
    }
}

actor RelayHost {
    private static let challengePath = RelayWireContract.challengePath
    private static let pairingPath = RelayWireContract.pairingPath
    private static let playlistPath = RelayWireContract.playlistPath
    private static let playlistSnapshotPath = RelayWireContract.playlistSnapshotPath
    private static let finishPath = RelayWireContract.finishPath
    private static let cancelPath = RelayWireContract.cancelPath
    private static let mediaPathPrefix = RelayWireContract.mediaPathPrefix
    private static let authenticationHeader = RelayWireContract.authenticationHeader
    private static let mediaCapabilityHeader = RelayWireContract.mediaCapabilityHeader

    private let configuration: RelayHostConfiguration
    private let fixtureRoot: URL
    private let replayStore: RelayReplayNonceStore
    private let now: @Sendable () -> Date

    private var pairingContext: RelayServerPairingContext?
    private var establishedSession: RelayEstablishedSession?
    private var playlist: RelayEventPlaylist
    private var lifecycle: RelayHostLifecycle = .pairing

    init(
        pairingContext: RelayServerPairingContext,
        configuration: RelayHostConfiguration,
        replayStore: RelayReplayNonceStore? = nil,
        now: @escaping @Sendable () -> Date = { Date() }
    ) throws {
        let normalizedRoot = configuration.fixtureDirectory.standardizedFileURL.resolvingSymlinksInPath()
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: normalizedRoot.path, isDirectory: &isDirectory), isDirectory.boolValue else {
            throw RelayHostError.invalidFixtureDirectory
        }
        fixtureRoot = normalizedRoot
        self.configuration = configuration
        if let replayStore {
            self.replayStore = replayStore
        } else {
            self.replayStore = try RelayReplayNonceStore()
        }
        self.now = now
        self.pairingContext = pairingContext
        playlist = try RelayEventPlaylist(
            targetDuration: configuration.playlistTargetDuration,
            retainedSegmentLimit: configuration.retainedSegmentLimit
        )
    }

    static func start(
        configuration: RelayHostConfiguration,
        pairingCode: RelayPairingCode = .random(),
        now: @escaping @Sendable () -> Date = { Date() },
        challengeTTL: TimeInterval = 120,
        sessionTTL: TimeInterval = 7_200
    ) throws -> RelayHost {
        let pairingContext = try RelayServerPairingContext(
            pairingCode: pairingCode,
            now: now(),
            challengeTTL: challengeTTL,
            sessionTTL: sessionTTL
        )
        return try RelayHost(pairingContext: pairingContext, configuration: configuration, now: now)
    }

    func advertisedBonjourService() -> RelayBonjourAdvertisement? {
        guard lifecycle == .pairing, let pairingContext else {
            return nil
        }
        return RelayBonjourAdvertisement(sessionID: pairingContext.challenge.sessionID)
    }

    func currentLifecycle() -> RelayHostLifecycle {
        _ = try? expireIfNeeded()
        return lifecycle
    }

    func pairingCode() -> RelayPairingCode? {
        _ = try? expireIfNeeded()
        guard lifecycle == .pairing else { return nil }
        return pairingContext?.pairingCode
    }

    func currentPlaylistSnapshot() throws -> RelayPlaylistSnapshot {
        try snapshot()
    }

    func ingestEventHLSFixture() throws {
        guard lifecycle == .pairing else {
            throw RelayHostError.unavailable
        }
        let fixture = try RelayEventHLSFixture.load(
            directory: fixtureRoot,
            initializationResourceIdentifier: configuration.initializationResourceIdentifier
        )
        var ingestedPlaylist = try RelayEventPlaylist(
            targetDuration: fixture.targetDuration,
            retainedSegmentLimit: configuration.retainedSegmentLimit
        )
        for segment in fixture.segments {
            _ = try resourceURL(for: segment.resourceIdentifier)
            _ = try ingestedPlaylist.append(resourceIdentifier: segment.resourceIdentifier, duration: segment.duration)
        }
        playlist = ingestedPlaylist
    }

    func needsMoreRequestBytes(_ requestData: Data) -> Bool {
        do {
            _ = try RelayHTTPParser.parse(requestData, limits: configuration.requestLimits)
            return false
        } catch RelayHTTPParseError.incomplete {
            return true
        } catch {
            return false
        }
    }

    func handle(_ requestData: Data, peer: RelayHostPeer) async -> RelayHTTPResponse {
        guard peer.isAllowed else {
            return .empty(statusCode: 403)
        }
        do {
            let request = try RelayHTTPParser.parse(requestData, limits: configuration.requestLimits)
            return try await route(request)
        } catch let error as RelayHTTPParseError {
            switch error {
            case .headersTooLarge:
                return .empty(statusCode: 431)
            case .bodyTooLarge:
                return .empty(statusCode: 413)
            case .incomplete, .malformedRequest, .unsupportedTransferEncoding:
                return .empty(statusCode: 400)
            }
        } catch let error as RelayHostError {
            return response(for: error)
        } catch let error as RelaySessionError {
            if error == .pairingAttemptsExhausted {
                cleanUpAsExpired()
            }
            return response(for: error)
        } catch {
            return .empty(statusCode: 500)
        }
    }

    func appendSegment(resourceIdentifier: String, duration: TimeInterval) throws -> RelayPlaylistSegment {
        try ensurePairedSession()
        _ = try resourceURL(for: resourceIdentifier)
        return try playlist.append(resourceIdentifier: resourceIdentifier, duration: duration)
    }

    func finalizePlaylist() throws {
        try ensurePairedSession()
        playlist.finalize()
        lifecycle = .finished
    }

    func cancel() {
        cleanUp(reason: .cancelled)
    }

    func networkLost() {
        cleanUp(reason: .networkLoss)
    }

    func stopForAppQuit() {
        cleanUp(reason: .appQuit)
    }

    func stop() {
        cleanUp(reason: .stopped)
    }

    private func route(_ request: RelayHTTPRequest) async throws -> RelayHTTPResponse {
        try expireIfNeeded()
        switch request.requestTarget {
        case Self.challengePath:
            return try await challengeResponse(for: request)
        case Self.pairingPath:
            return try await pairingResponse(for: request)
        case Self.playlistPath:
            try await authenticate(request)
            return .text(renderPlaylist(), contentType: "application/vnd.apple.mpegurl")
        case Self.playlistSnapshotPath:
            try await authenticate(request)
            return .json(try snapshot())
        case Self.finishPath:
            try await authenticate(request)
            guard request.method == "POST", request.body.isEmpty else {
                return .empty(statusCode: 405)
            }
            playlist.finalize()
            lifecycle = .finished
            return .empty(statusCode: 204)
        case Self.cancelPath:
            try await authenticate(request)
            guard request.method == "POST", request.body.isEmpty else {
                return .empty(statusCode: 405)
            }
            cleanUp(reason: .cancelled)
            return .empty(statusCode: 204)
        default:
            guard request.requestTarget.hasPrefix(Self.mediaPathPrefix) else {
                return .empty(statusCode: 404)
            }
            try await authenticate(request)
            guard request.method == "GET" else {
                return .empty(statusCode: 405)
            }
            return try serveMedia(for: request)
        }
    }

    private func challengeResponse(for request: RelayHTTPRequest) async throws -> RelayHTTPResponse {
        guard request.method == "GET", request.body.isEmpty else {
            return .empty(statusCode: 405)
        }
        guard lifecycle == .pairing, let pairingContext else {
            throw RelayHostError.unavailable
        }
        return .json(RelayChallengeEnvelope(challenge: pairingContext.challenge))
    }

    private func pairingResponse(for request: RelayHTTPRequest) async throws -> RelayHTTPResponse {
        guard request.method == "POST" else {
            return .empty(statusCode: 405)
        }
        guard lifecycle == .pairing, let pairingContext else {
            throw RelayHostError.unavailable
        }
        let pairingRequest: RelayPairingRequest
        do {
            pairingRequest = try JSONDecoder().decode(RelayPairingRequest.self, from: request.body)
        } catch {
            return .empty(statusCode: 400)
        }
        let result = try await pairingContext.accept(pairingRequest, now: now())
        establishedSession = result.session
        self.pairingContext = nil
        lifecycle = .paired
        return .json(RelayPairingEnvelope(acceptance: result.acceptance), statusCode: 201)
    }

    private func authenticate(_ request: RelayHTTPRequest) async throws {
        try ensurePairedSession()
        guard let encodedAuthentication = request.header(named: Self.authenticationHeader),
              let authenticationData = Data(base64Encoded: encodedAuthentication)
        else {
            throw RelaySessionError.invalidRequest
        }
        let authentication: RelayAuthenticatedRequest
        do {
            authentication = try JSONDecoder().decode(RelayAuthenticatedRequest.self, from: authenticationData)
        } catch {
            throw RelaySessionError.invalidRequest
        }
        guard let establishedSession else {
            throw RelayHostError.notPaired
        }
        try await establishedSession.verify(
            authentication,
            actualMethod: request.method,
            actualRequestTarget: request.requestTarget,
            body: request.body,
            now: now(),
            policy: configuration.requestValidationPolicy,
            replayStore: replayStore
        )
    }

    private func serveMedia(for request: RelayHTTPRequest) throws -> RelayHTTPResponse {
        guard let establishedSession,
              let capability = request.header(named: Self.mediaCapabilityHeader),
              establishedSession.mediaCapability.matches(capability)
        else {
            return .empty(statusCode: 403)
        }
        let resourceIdentifier = String(request.requestTarget.dropFirst(Self.mediaPathPrefix.count))
        let fileURL = try resourceURL(for: resourceIdentifier)
        let attributes = try FileManager.default.attributesOfItem(atPath: fileURL.path)
        guard let fileSize = attributes[.size] as? NSNumber else {
            throw RelayHostError.resourceNotFound
        }
        guard fileSize.intValue <= configuration.maximumMediaBytes else {
            throw RelayHostError.resourceTooLarge
        }
        let data = try Data(contentsOf: fileURL, options: .mappedIfSafe)
        guard data.count <= configuration.maximumMediaBytes else {
            throw RelayHostError.resourceTooLarge
        }
        return RelayHTTPResponse(statusCode: 200, headers: ["content-type": contentType(for: fileURL)], body: data)
    }

    private func resourceURL(for identifier: String) throws -> URL {
        guard isSafeResourceIdentifier(identifier) else {
            throw RelayHostError.invalidResourceIdentifier
        }
        let candidate = fixtureRoot.appendingPathComponent(identifier).standardizedFileURL.resolvingSymlinksInPath()
        guard candidate.path.hasPrefix(fixtureRoot.path + "/"),
              FileManager.default.fileExists(atPath: candidate.path)
        else {
            throw RelayHostError.resourceNotFound
        }
        var isDirectory: ObjCBool = false
        guard !FileManager.default.fileExists(atPath: candidate.path, isDirectory: &isDirectory) || !isDirectory.boolValue else {
            throw RelayHostError.resourceNotFound
        }
        return candidate
    }

    private func ensurePairedSession() throws {
        try expireIfNeeded()
        guard lifecycle == .paired || lifecycle == .finished, establishedSession != nil else {
            throw RelayHostError.notPaired
        }
    }

    private func expireIfNeeded() throws {
        let currentDate = now()
        if lifecycle == .pairing, let pairingContext,
           currentDate > Date(timeIntervalSince1970: TimeInterval(pairingContext.challenge.expiresAtUnixMilliseconds) / 1_000) {
            cleanUpAsExpired()
            throw RelayHostError.expired
        }
        if (lifecycle == .paired || lifecycle == .finished), let establishedSession,
           currentDate > establishedSession.expirationDate {
            cleanUpAsExpired()
            throw RelayHostError.expired
        }
    }

    private func cleanUpAsExpired() {
        pairingContext = nil
        establishedSession = nil
        playlist.finalize()
        lifecycle = .expired
    }

    private func cleanUp(reason: RelayHostStopReason) {
        guard lifecycle != .cancelled, lifecycle != .expired, lifecycle != .stopped else {
            return
        }
        pairingContext = nil
        establishedSession = nil
        playlist.finalize()
        switch reason {
        case .cancelled:
            lifecycle = .cancelled
        case .networkLoss:
            lifecycle = .stopped
        case .appQuit, .stopped:
            lifecycle = .stopped
        }
    }

    private func renderPlaylist() -> String {
        let targetDurationSeconds = Int(ceil(playlist.targetDuration))
        let mediaSequence = playlist.segments.first?.sequenceNumber ?? playlist.nextSequenceNumber
        var lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            "#EXT-X-PLAYLIST-TYPE:EVENT",
            "#EXT-X-TARGETDURATION:\(targetDurationSeconds)",
            "#EXT-X-MEDIA-SEQUENCE:\(mediaSequence)",
            "#EXT-X-MAP:URI=\"\(RelayWireContract.mediaPathPrefix)\(configuration.initializationResourceIdentifier)\"",
        ]
        for segment in playlist.segments {
            lines.append(String(format: "#EXTINF:%.3f,", segment.duration))
            lines.append("\(RelayWireContract.mediaPathPrefix)\(segment.resourceIdentifier)")
        }
        if playlist.hasEndList {
            lines.append("#EXT-X-ENDLIST")
        }
        return lines.joined(separator: "\n") + "\n"
    }

    private func snapshot() throws -> RelayPlaylistSnapshot {
        try RelayPlaylistSnapshot(
            earliestPlayableTimeMilliseconds: playlist.earliestPlayableTimeMilliseconds,
            totalDurationMilliseconds: playlist.nextStartTimeMilliseconds,
            isFinalized: playlist.isFinalized,
            segments: playlist.segments
        )
    }

    private func response(for error: RelayHostError) -> RelayHTTPResponse {
        switch error {
        case .invalidFixtureDirectory, .unavailable, .notPaired:
            .empty(statusCode: 503)
        case .expired:
            .empty(statusCode: 410)
        case .invalidResourceIdentifier:
            .empty(statusCode: 400)
        case .resourceNotFound:
            .empty(statusCode: 404)
        case .resourceTooLarge:
            .empty(statusCode: 413)
        }
    }

    private func response(for error: RelaySessionError) -> RelayHTTPResponse {
        switch error {
        case .requestExpired, .requestTimestampTooFarInFuture:
            .empty(statusCode: 401)
        case .replayDetected:
            .empty(statusCode: 409)
        case .pairingAlreadyCompleted, .pairingAttemptsExhausted:
            .empty(statusCode: 409)
        case .expiredChallenge:
            .empty(statusCode: 410)
        default:
            .empty(statusCode: 401)
        }
    }

    private func isSafeResourceIdentifier(_ value: String) -> Bool {
        let bytes = value.utf8
        guard (1 ... 256).contains(bytes.count),
              !value.contains("%"),
              bytes.allSatisfy({ byte in
                  (byte >= 65 && byte <= 90)
                      || (byte >= 97 && byte <= 122)
                      || (byte >= 48 && byte <= 57)
                      || "-._/".utf8.contains(byte)
              })
        else {
            return false
        }
        return value.split(separator: "/", omittingEmptySubsequences: false).allSatisfy {
            !$0.isEmpty && $0 != "." && $0 != ".."
        }
    }

    private func contentType(for url: URL) -> String {
        switch url.pathExtension.lowercased() {
        case "m4s": "video/iso.segment"
        case "mp4", "m4v": "video/mp4"
        case "ts": "video/mp2t"
        case "json": "application/json"
        default: "application/octet-stream"
        }
    }
}

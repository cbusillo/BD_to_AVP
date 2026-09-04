import Foundation

enum RelayHostLifecycle: String, Codable, Equatable, Sendable {
    case pairing
    case awaitingConfirmation
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
    private struct AuthenticatedExchange: Sendable {
        let request: RelayAuthenticatedRequest
        let session: RelayEstablishedSession
    }

    private static let challengePath = RelayWireContract.challengePath
    private static let pairingPath = RelayWireContract.pairingPath
    private static let pairingConfirmPath = RelayWireContract.pairingConfirmPath
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
    private let pairingExpiresAt: Date
    private let initializationResourceIdentifier: String
    private var allowedMediaResourceIdentifiers: Set<String>

    private var pairingContext: RelayServerPairingContext?
    private var provisionalSession: RelayEstablishedSession?
    private var establishedSession: RelayEstablishedSession?
    private var playlist: RelayEventPlaylist
    private var lifecycle: RelayHostLifecycle = .pairing

    init(
        pairingContext: RelayServerPairingContext,
        configuration: RelayHostConfiguration,
        fixture: RelayEventHLSFixture,
        replayStore: RelayReplayNonceStore? = nil,
        pairingSessionTTL: TimeInterval = 600,
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
        guard let pairingStart = RelayTime.unixMilliseconds(for: now()),
              let pairingSessionTTLMilliseconds = RelayTime.milliseconds(
                  from: pairingSessionTTL,
                  maximum: 60 * 60 * 1_000
              ), pairingSessionTTLMilliseconds > 0,
              let pairingExpiration = RelayTime.adding(pairingSessionTTLMilliseconds, to: pairingStart)
        else {
            throw RelaySessionError.invalidValidationPolicy
        }
        pairingExpiresAt = RelayTime.date(fromUnixMilliseconds: pairingExpiration)
        self.pairingContext = pairingContext
        initializationResourceIdentifier = fixture.initializationResourceIdentifier
        allowedMediaResourceIdentifiers = Set(
            [fixture.initializationResourceIdentifier] + fixture.segments.map(\.resourceIdentifier)
        )
        var ingestedPlaylist = try RelayEventPlaylist(
            targetDuration: fixture.targetDuration,
            retainedSegmentLimit: configuration.retainedSegmentLimit
        )
        for segment in fixture.segments {
            _ = try ingestedPlaylist.append(resourceIdentifier: segment.resourceIdentifier, duration: segment.duration)
        }
        playlist = ingestedPlaylist
    }

    static func start(
        configuration: RelayHostConfiguration,
        fixture: RelayEventHLSFixture,
        now: @escaping @Sendable () -> Date = { Date() },
        challengeTTL: TimeInterval = 120,
        candidateTTL: TimeInterval = 60,
        pairingSessionTTL: TimeInterval = 600,
        sessionTTL: TimeInterval = 7_200
    ) throws -> RelayHost {
        let pairingContext = try RelayServerPairingContext(
            now: now(),
            challengeTTL: challengeTTL,
            candidateTTL: candidateTTL,
            sessionTTL: sessionTTL
        )
        return try RelayHost(
            pairingContext: pairingContext,
            configuration: configuration,
            fixture: fixture,
            pairingSessionTTL: pairingSessionTTL,
            now: now
        )
    }

    func advertisedBonjourService() -> RelayBonjourAdvertisement? {
        guard lifecycle == .pairing, let pairingContext else {
            return nil
        }
        return RelayBonjourAdvertisement(sessionID: pairingContext.sessionID)
    }

    func currentLifecycle() async -> RelayHostLifecycle {
        _ = try? expireIfNeeded()
        if lifecycle == .awaitingConfirmation {
            _ = await currentPairingCandidate()
        } else if lifecycle == .pairing, let pairingContext {
            do {
                _ = try await pairingContext.currentChallenge(now: now())
            } catch RelaySessionError.pairingAttemptsExhausted {
                cleanUpAsExpired()
            } catch {}
        }
        return lifecycle
    }

    func currentPairingCandidate() async -> RelayPendingPairingCandidate? {
        _ = try? expireIfNeeded()
        guard lifecycle == .awaitingConfirmation else { return nil }
        guard let pairingContext else { return nil }
        do {
            let candidate = try await pairingContext.pendingCandidateSummary(now: now())
            if candidate == nil {
                provisionalSession = nil
                lifecycle = .pairing
            }
            return candidate
        } catch RelaySessionError.pairingAttemptsExhausted {
            provisionalSession = nil
            cleanUpAsExpired()
        } catch {
            provisionalSession = nil
            lifecycle = .pairing
        }
        return nil
    }

    func approvePairingCandidate(_ candidateID: RelayPairingCandidateIdentifier) async throws {
        try expireIfNeeded()
        guard lifecycle == .awaitingConfirmation, let pairingContext else { throw RelayHostError.unavailable }
        try await pairingContext.approve(candidateID: candidateID, now: now())
    }

    func rejectPairingCandidate(_ candidateID: RelayPairingCandidateIdentifier) async throws {
        try expireIfNeeded()
        guard lifecycle == .awaitingConfirmation, let pairingContext else { throw RelayHostError.unavailable }
        try await pairingContext.reject(candidateID: candidateID, now: now())
        provisionalSession = nil
        lifecycle = .pairing
    }

    func currentPlaylistSnapshot() throws -> RelayPlaylistSnapshot {
        try snapshot()
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
        _ = try resourceURL(for: resourceIdentifier, requireAllowlisted: false)
        let segment = try playlist.append(resourceIdentifier: resourceIdentifier, duration: duration)
        allowedMediaResourceIdentifiers.insert(resourceIdentifier)
        return segment
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
        case Self.pairingConfirmPath:
            return try await pairingConfirmationResponse(for: request)
        case Self.playlistPath:
            return try await authenticatedResponse(for: request) { _ in
                .text(renderPlaylist(), contentType: "application/vnd.apple.mpegurl")
            }
        case Self.playlistSnapshotPath:
            return try await authenticatedResponse(for: request) { _ in
                .json(try snapshot())
            }
        case Self.finishPath:
            return try await authenticatedResponse(for: request) { _ in
                guard request.method == "POST", request.body.isEmpty else {
                    return .empty(statusCode: 405)
                }
                playlist.finalize()
                lifecycle = .finished
                return .empty(statusCode: 204)
            }
        case Self.cancelPath:
            return try await authenticatedResponse(for: request) { _ in
                guard request.method == "POST", request.body.isEmpty else {
                    return .empty(statusCode: 405)
                }
                cleanUp(reason: .cancelled)
                return .empty(statusCode: 204)
            }
        default:
            guard request.requestTarget.hasPrefix(Self.mediaPathPrefix) else {
                return .empty(statusCode: 404)
            }
            return try await authenticatedResponse(for: request) { session in
                guard request.method == "GET" else {
                    return .empty(statusCode: 405)
                }
                return try serveMedia(for: request, session: session)
            }
        }
    }

    private func challengeResponse(for request: RelayHTTPRequest) async throws -> RelayHTTPResponse {
        guard request.method == "GET", request.body.isEmpty else {
            return .empty(statusCode: 405)
        }
        guard lifecycle == .pairing || lifecycle == .awaitingConfirmation, let pairingContext else {
            throw RelayHostError.unavailable
        }
        do {
            return .json(RelayChallengeEnvelope(challenge: try await pairingContext.currentChallenge(now: now())))
        } catch RelaySessionError.pairingAttemptsExhausted {
            cleanUpAsExpired()
            throw RelaySessionError.pairingAttemptsExhausted
        }
    }

    private func pairingResponse(for request: RelayHTTPRequest) async throws -> RelayHTTPResponse {
        guard request.method == "POST" else {
            return .empty(statusCode: 405)
        }
        guard lifecycle == .pairing || lifecycle == .awaitingConfirmation, let pairingContext else {
            throw RelayHostError.unavailable
        }
        let pairingRequest: RelayPairingRequest
        do {
            pairingRequest = try JSONDecoder().decode(RelayPairingRequest.self, from: request.body)
        } catch {
            return .empty(statusCode: 400)
        }
        let result: RelayServerPairingResult
        do {
            result = try await pairingContext.accept(pairingRequest, now: now())
        } catch RelaySessionError.pairingAttemptsExhausted {
            cleanUpAsExpired()
            throw RelaySessionError.pairingAttemptsExhausted
        }
        provisionalSession = result.provisionalSession
        lifecycle = .awaitingConfirmation
        return .json(RelayPairingCandidateEnvelope(candidate: result.candidate), statusCode: 201)
    }

    private func pairingConfirmationResponse(for request: RelayHTTPRequest) async throws -> RelayHTTPResponse {
        guard request.method == "POST" else { return .empty(statusCode: 405) }
        let exchange = try await authenticateProvisional(request)
        let confirmation: RelayPairingConfirmation
        do {
            confirmation = try JSONDecoder().decode(RelayPairingConfirmation.self, from: request.body)
        } catch {
            return try authenticate(.empty(statusCode: 400), for: exchange)
        }
        guard let pairingContext else { throw RelayHostError.unavailable }
        let result = try await pairingContext.confirm(confirmation, now: now())
        if confirmation.decision == .notMyMac {
            provisionalSession = nil
            lifecycle = .pairing
        }
        if let session = result.session {
            establishedSession = session
            provisionalSession = nil
            self.pairingContext = nil
            lifecycle = .paired
        }
        let statusCode = result.response.state == .waitingForMac ? 202 : 200
        return try authenticate(
            .json(RelayPairingConfirmationEnvelope(confirmation: result.response), statusCode: statusCode),
            for: exchange
        )
    }

    private func authenticate(_ request: RelayHTTPRequest) async throws -> AuthenticatedExchange {
        try ensurePairedSession()
        return try await authenticate(request, with: establishedSession)
    }

    private func authenticateProvisional(_ request: RelayHTTPRequest) async throws -> AuthenticatedExchange {
        try expireIfNeeded()
        guard lifecycle == .awaitingConfirmation, let pairingContext else { throw RelayHostError.notPaired }
        guard try await pairingContext.pendingCandidateSummary(now: now()) != nil else {
            provisionalSession = nil
            throw RelaySessionError.confirmationExpired
        }
        return try await authenticate(request, with: provisionalSession)
    }

    private func authenticate(
        _ request: RelayHTTPRequest,
        with session: RelayEstablishedSession?
    ) async throws -> AuthenticatedExchange {
        guard let encodedAuthentication = request.header(named: Self.authenticationHeader),
              encodedAuthentication.utf8.count <= RelayWireContract.maximumAuthenticationHeaderBytes,
              let authenticationData = Data(base64Encoded: encodedAuthentication),
              authenticationData.count <= RelayWireContract.maximumAuthenticationHeaderBytes
        else {
            throw RelaySessionError.invalidRequest
        }
        let authentication: RelayAuthenticatedRequest
        do {
            authentication = try JSONDecoder().decode(RelayAuthenticatedRequest.self, from: authenticationData)
        } catch {
            throw RelaySessionError.invalidRequest
        }
        guard let session else {
            throw RelayHostError.notPaired
        }
        try await session.verify(
            authentication,
            actualMethod: request.method,
            actualRequestTarget: request.requestTarget,
            body: request.body,
            now: now(),
            policy: configuration.requestValidationPolicy,
            replayStore: replayStore
        )
        return AuthenticatedExchange(request: authentication, session: session)
    }

    private func authenticatedResponse(
        for request: RelayHTTPRequest,
        makeResponse: (RelayEstablishedSession) throws -> RelayHTTPResponse
    ) async throws -> RelayHTTPResponse {
        let exchange = try await authenticate(request)
        let response: RelayHTTPResponse
        do {
            response = try makeResponse(exchange.session)
        } catch let error as RelayHostError {
            response = self.response(for: error)
        } catch let error as RelaySessionError {
            response = self.response(for: error)
        } catch {
            response = .empty(statusCode: 500)
        }
        return try authenticate(response, for: exchange)
    }

    private func authenticate(
        _ response: RelayHTTPResponse,
        for exchange: AuthenticatedExchange
    ) throws -> RelayHTTPResponse {
        let authentication = try exchange.session.authenticateResponse(
            requestNonce: exchange.request.nonce,
            statusCode: response.statusCode,
            body: response.body
        )
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let encoded = try encoder.encode(authentication).base64EncodedString()
        guard encoded.utf8.count <= RelayWireContract.maximumAuthenticationHeaderBytes else {
            throw RelaySessionError.invalidResponse
        }
        var headers = response.headers
        headers[RelayWireContract.responseAuthenticationHeader] = encoded
        return RelayHTTPResponse(statusCode: response.statusCode, headers: headers, body: response.body)
    }

    private func serveMedia(
        for request: RelayHTTPRequest,
        session: RelayEstablishedSession
    ) throws -> RelayHTTPResponse {
        guard let capability = request.header(named: Self.mediaCapabilityHeader),
              session.mediaCapability.matches(capability)
        else {
            return .empty(statusCode: 403)
        }
        let resourceIdentifier = String(request.requestTarget.dropFirst(Self.mediaPathPrefix.count))
        let fileURL = try resourceURL(for: resourceIdentifier)
        let attributes = try FileManager.default.attributesOfItem(atPath: fileURL.path)
        guard let fileSize = attributes[.size] as? NSNumber else {
            throw RelayHostError.resourceNotFound
        }
        guard fileSize.int64Value >= 0,
              fileSize.int64Value <= Int64(configuration.maximumMediaBytes)
        else {
            throw RelayHostError.resourceTooLarge
        }
        let data = try Data(contentsOf: fileURL, options: .mappedIfSafe)
        guard data.count <= configuration.maximumMediaBytes else {
            throw RelayHostError.resourceTooLarge
        }
        return RelayHTTPResponse(statusCode: 200, headers: ["content-type": contentType(for: fileURL)], body: data)
    }

    private func resourceURL(for identifier: String, requireAllowlisted: Bool = true) throws -> URL {
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
        guard !requireAllowlisted || allowedMediaResourceIdentifiers.contains(identifier) else {
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
        if (lifecycle == .pairing || lifecycle == .awaitingConfirmation),
           currentDate > pairingExpiresAt {
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
        provisionalSession = nil
        establishedSession = nil
        playlist.finalize()
        lifecycle = .expired
    }

    private func cleanUp(reason: RelayHostStopReason) {
        guard lifecycle != .cancelled, lifecycle != .expired, lifecycle != .stopped else {
            return
        }
        pairingContext = nil
        provisionalSession = nil
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
            "#EXT-X-MAP:URI=\"\(RelayWireContract.mediaPathPrefix)\(initializationResourceIdentifier)\"",
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
        case .pairingAlreadyCompleted, .pairingCandidateInProgress, .pairingCandidateNotFound, .pairingAttemptsExhausted:
            .empty(statusCode: 409)
        case .expiredChallenge, .confirmationExpired:
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

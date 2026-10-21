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

enum RelayHostPairingSuspensionPoint: Equatable, Sendable {
    case currentChallenge
    case pendingCandidateSummary
    case approve
    case reject
    case accept
    case confirm
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

/// What a developer-probe host serves live instead of a fixture: one stream, started once the
/// headset pairs.
struct RelayHostLiveSource: Sendable {
    let starter: any LiveRelayStreamStarting
    let request: LiveStreamRequest
}

struct RelayBonjourAdvertisement: Sendable, Equatable {
    let serviceType: String
    let txtRecord: Data

    init(sessionID: RelaySessionIdentifier, serviceType: String = RelayWireContract.bonjourServiceType) {
        self.serviceType = serviceType
        txtRecord = NetService.data(fromTXTRecord: [
            "sid": Data(sessionID.rawValue.prefix(8).utf8),
            "v": Data(String(RelayWireContract.protocolVersion).utf8),
        ])
    }
}

actor RelayHost: RelayNetworkHosting {
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
    private let pairingSuspensionHook: (@Sendable (RelayHostPairingSuspensionPoint) async -> Void)?
    private var pairingExpiresAt: Date
    private let initializationResourceIdentifier: String
    private var allowedMediaResourceIdentifiers: Set<String>

    private var pairingContext: RelayServerPairingContext?
    private var provisionalSession: RelayEstablishedSession?
    private var establishedSession: RelayEstablishedSession?
    private var playlist: RelayEventPlaylist
    private var lifecycle: RelayHostLifecycle = .pairing

    // Live mode. The stream is reachable only under the session derived from the established
    // session for `liveStreamID`; see `liveResponse(for:)`.
    private let liveSource: RelayHostLiveSource?
    private var liveStreamID: UUID?
    private var liveStartTask: Task<any LiveRelayStreaming, any Error>?
    private var liveStream: (any LiveRelayStreaming)?
    private var liveStopTask: Task<Void, Never>?
    private var liveStopReason: LiveRelayStopReason?
    /// Set when the start threw: every request for the stream then gets this signed failure.
    private var liveStartFailure: RelayLiveStreamEnded?

    init(
        pairingContext: RelayServerPairingContext,
        configuration: RelayHostConfiguration,
        fixture: RelayEventHLSFixture,
        replayStore: RelayReplayNonceStore? = nil,
        pairingSessionTTL: TimeInterval = 600,
        now: @escaping @Sendable () -> Date = { Date() },
        pairingSuspensionHook: (@Sendable (RelayHostPairingSuspensionPoint) async -> Void)? = nil
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
        self.pairingSuspensionHook = pairingSuspensionHook
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
        if fixture.isFinalized { ingestedPlaylist.finalize() }
        playlist = ingestedPlaylist
        liveSource = nil
    }

    /// A developer-probe host that serves one live stream. `configuration` supplies the request
    /// limits and validation policy; its fixture settings are unused.
    init(
        pairingContext: RelayServerPairingContext,
        configuration: RelayHostConfiguration,
        liveSource: RelayHostLiveSource,
        replayStore: RelayReplayNonceStore? = nil,
        pairingSessionTTL: TimeInterval = 600,
        now: @escaping @Sendable () -> Date = { Date() },
        pairingSuspensionHook: (@Sendable (RelayHostPairingSuspensionPoint) async -> Void)? = nil
    ) throws {
        fixtureRoot = configuration.fixtureDirectory.standardizedFileURL
        self.configuration = configuration
        self.replayStore = try replayStore ?? RelayReplayNonceStore()
        self.now = now
        self.pairingSuspensionHook = pairingSuspensionHook
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
        initializationResourceIdentifier = configuration.initializationResourceIdentifier
        allowedMediaResourceIdentifiers = []
        playlist = try RelayEventPlaylist(targetDuration: 2, retainedSegmentLimit: 1)
        self.liveSource = liveSource
    }

    static func startLive(
        configuration: RelayHostConfiguration,
        liveSource: RelayHostLiveSource,
        now: @escaping @Sendable () -> Date = { Date() },
        challengeTTL: TimeInterval = 120,
        candidateTTL: TimeInterval = 300,
        pairingSessionTTL: TimeInterval = 600,
        sessionTTL: TimeInterval = 7_200
    ) throws -> RelayHost {
        try RelayHost(
            pairingContext: RelayServerPairingContext(
                now: now(),
                challengeTTL: challengeTTL,
                candidateTTL: candidateTTL,
                sessionTTL: sessionTTL
            ),
            configuration: configuration,
            liveSource: liveSource,
            pairingSessionTTL: pairingSessionTTL,
            now: now
        )
    }

    /// The live stream's ID once the headset has paired. A probe client derives its stream session
    /// from it.
    func currentLiveStreamID() -> UUID? {
        liveStreamID
    }

    /// The live stream once its start has returned.
    func currentLiveStream() -> (any LiveRelayStreaming)? {
        liveStream
    }

    static func start(
        configuration: RelayHostConfiguration,
        fixture: RelayEventHLSFixture,
        now: @escaping @Sendable () -> Date = { Date() },
        challengeTTL: TimeInterval = 120,
        candidateTTL: TimeInterval = 300,
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
                await pairingSuspensionHook?(.currentChallenge)
            } catch RelaySessionError.pairingAttemptsExhausted {
                await pairingSuspensionHook?(.currentChallenge)
                if isCurrentPairingContext(pairingContext, lifecycle: .pairing) {
                    cleanUpAsExpired()
                }
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
            await pairingSuspensionHook?(.pendingCandidateSummary)
            guard isCurrentPairingContext(pairingContext, lifecycle: .awaitingConfirmation) else { return nil }
            if candidate == nil {
                provisionalSession = nil
                lifecycle = .pairing
            }
            return candidate
        } catch RelaySessionError.pairingAttemptsExhausted {
            await pairingSuspensionHook?(.pendingCandidateSummary)
            if isCurrentPairingContext(pairingContext, lifecycle: .awaitingConfirmation) {
                provisionalSession = nil
                cleanUpAsExpired()
            }
        } catch {
            await pairingSuspensionHook?(.pendingCandidateSummary)
            if isCurrentPairingContext(pairingContext, lifecycle: .awaitingConfirmation) {
                provisionalSession = nil
                lifecycle = .pairing
            }
        }
        return nil
    }

    func approvePairingCandidate(_ candidateID: RelayPairingCandidateIdentifier) async throws {
        try expireIfNeeded()
        guard lifecycle == .awaitingConfirmation, let pairingContext else { throw RelayHostError.unavailable }
        try await pairingContext.approve(candidateID: candidateID, now: now())
        await pairingSuspensionHook?(.approve)
        guard isCurrentPairingContext(pairingContext, lifecycle: .awaitingConfirmation) else {
            throw RelayHostError.unavailable
        }
    }

    func rejectPairingCandidate(_ candidateID: RelayPairingCandidateIdentifier) async throws {
        try expireIfNeeded()
        guard lifecycle == .awaitingConfirmation, let pairingContext else { throw RelayHostError.unavailable }
        try await pairingContext.reject(candidateID: candidateID, now: now())
        await pairingSuspensionHook?(.reject)
        guard isCurrentPairingContext(pairingContext, lifecycle: .awaitingConfirmation) else {
            throw RelayHostError.unavailable
        }
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
        let pairingContextAtStart = pairingContext
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
                cleanUpAsExpiredIfCurrent(pairingContextAtStart)
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

    /// Each of these invalidates synchronously, then returns once a live stream's worker has
    /// exited and its workspace is gone.
    func cancel() async {
        cleanUp(reason: .cancelled)
        await liveStopTask?.value
    }

    func networkLost() async {
        cleanUp(reason: .networkLoss)
        await liveStopTask?.value
    }

    func stopForAppQuit() async {
        cleanUp(reason: .appQuit)
        await liveStopTask?.value
    }

    func stop() async {
        cleanUp(reason: .stopped)
        await liveStopTask?.value
    }

    private func route(_ request: RelayHTTPRequest) async throws -> RelayHTTPResponse {
        try expireIfNeeded()
        if liveSource != nil, Self.isLiveRelayRoute(request.requestTarget) {
            return try await liveResponse(for: request)
        }
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
        let expectedLifecycle = lifecycle
        do {
            let challenge = try await pairingContext.currentChallenge(now: now())
            await pairingSuspensionHook?(.currentChallenge)
            guard isCurrentPairingContext(pairingContext, lifecycle: expectedLifecycle) else {
                throw RelayHostError.unavailable
            }
            return .json(RelayChallengeEnvelope(challenge: challenge))
        } catch RelaySessionError.pairingAttemptsExhausted {
            await pairingSuspensionHook?(.currentChallenge)
            guard isCurrentPairingContext(pairingContext, lifecycle: expectedLifecycle) else {
                throw RelayHostError.unavailable
            }
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
            await pairingSuspensionHook?(.accept)
        } catch RelaySessionError.pairingAttemptsExhausted {
            await pairingSuspensionHook?(.accept)
            guard pairingContext === self.pairingContext,
                  lifecycle == .pairing || lifecycle == .awaitingConfirmation
            else {
                throw RelayHostError.unavailable
            }
            cleanUpAsExpired()
            throw RelaySessionError.pairingAttemptsExhausted
        }
        guard pairingContext === self.pairingContext,
              lifecycle == .pairing || lifecycle == .awaitingConfirmation
        else { throw RelayHostError.unavailable }
        // A comparison started before the advertising deadline gets its full window.
        pairingExpiresAt = max(pairingExpiresAt, result.candidate.expirationDate)
        provisionalSession = result.provisionalSession
        lifecycle = .awaitingConfirmation
        return .json(RelayPairingCandidateEnvelope(candidate: result.candidate), statusCode: 201)
    }

    private func pairingConfirmationResponse(for request: RelayHTTPRequest) async throws -> RelayHTTPResponse {
        guard request.method == "POST" else { return .empty(statusCode: 405) }
        let exchange = try await authenticateProvisional(request)
        let pairingContextAtStart = pairingContext
        let confirmation: RelayPairingConfirmation
        do {
            confirmation = try JSONDecoder().decode(RelayPairingConfirmation.self, from: request.body)
        } catch {
            return try authenticate(.empty(statusCode: 400), for: exchange)
        }
        let result: RelayServerConfirmationResult
        do {
            guard lifecycle == .awaitingConfirmation, let pairingContext else {
                throw RelayHostError.unavailable
            }
            result = try await pairingContext.confirm(confirmation, now: now())
            await pairingSuspensionHook?(.confirm)
            guard isCurrentPairingContext(pairingContext, lifecycle: .awaitingConfirmation) else {
                throw RelayHostError.unavailable
            }
        } catch let error as RelayHostError {
            return try authenticate(response(for: error), for: exchange)
        } catch let error as RelaySessionError {
            if error == .pairingAttemptsExhausted {
                cleanUpAsExpiredIfCurrent(pairingContextAtStart)
            }
            return try authenticate(response(for: error), for: exchange)
        }
        if confirmation.decision == .notMyMac {
            provisionalSession = nil
            lifecycle = .pairing
        }
        if let session = result.session {
            establishedSession = session
            provisionalSession = nil
            self.pairingContext = nil
            lifecycle = playlist.isFinalized && liveSource == nil ? .finished : .paired
            startLiveStreamIfNeeded(for: session)
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
        let candidate = try await pairingContext.pendingCandidateSummary(now: now())
        await pairingSuspensionHook?(.pendingCandidateSummary)
        guard isCurrentPairingContext(pairingContext, lifecycle: .awaitingConfirmation) else {
            throw RelayHostError.unavailable
        }
        guard candidate != nil else {
            provisionalSession = nil
            throw RelaySessionError.confirmationExpired
        }
        // The confirmation route revalidates host state after authentication and
        // can still sign an unavailable response with this authenticated exchange.
        return try await authenticate(request, with: provisionalSession)
    }

    private func isCurrentPairingContext(
        _ context: RelayServerPairingContext,
        lifecycle expectedLifecycle: RelayHostLifecycle
    ) -> Bool {
        pairingContext === context && lifecycle == expectedLifecycle
    }

    private func cleanUpAsExpiredIfCurrent(_ context: RelayServerPairingContext?) {
        guard let context,
              lifecycle == .pairing || lifecycle == .awaitingConfirmation,
              pairingContext === context
        else { return }
        cleanUpAsExpired()
    }

    private func decodeAuthentication(_ request: RelayHTTPRequest) throws -> RelayAuthenticatedRequest {
        guard let encodedAuthentication = request.header(named: Self.authenticationHeader),
              encodedAuthentication.utf8.count <= RelayWireContract.maximumAuthenticationHeaderBytes,
              let authenticationData = Data(base64Encoded: encodedAuthentication),
              authenticationData.count <= RelayWireContract.maximumAuthenticationHeaderBytes
        else {
            throw RelaySessionError.invalidRequest
        }
        do {
            return try JSONDecoder().decode(RelayAuthenticatedRequest.self, from: authenticationData)
        } catch {
            throw RelaySessionError.invalidRequest
        }
    }

    private func authenticate(
        _ request: RelayHTTPRequest,
        with session: RelayEstablishedSession?
    ) async throws -> AuthenticatedExchange {
        let authentication = try decodeAuthentication(request)
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
        stopLiveStream(.sessionEnded)
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
            stopLiveStream(.cancelled)
        case .networkLoss, .stopped:
            lifecycle = .stopped
            stopLiveStream(.sharingStopped)
        case .appQuit:
            lifecycle = .stopped
            stopLiveStream(.appQuit)
        }
    }

    // MARK: - Live mode

    private static func isLiveRelayRoute(_ target: String) -> Bool {
        target == playlistPath || target == playlistSnapshotPath || target == finishPath
            || target == cancelPath || target == RelayWireContract.positionPath || target.hasPrefix(mediaPathPrefix)
    }

    private func startLiveStreamIfNeeded(for session: RelayEstablishedSession) {
        guard let liveSource, liveStreamID == nil, liveStopTask == nil else { return }
        let streamID = LiveStreamIdentifier.make(excluding: [session.sessionID])
        liveStreamID = streamID
        let startTask = Task { try await liveSource.starter.start(liveSource.request, id: streamID) }
        liveStartTask = startTask
        Task { await self.installLiveStream(from: startTask) }
    }

    private func installLiveStream(from startTask: Task<any LiveRelayStreaming, any Error>) async {
        let result = await startTask.result
        // After a stop, the stop task owns whatever the start returned.
        guard liveStopTask == nil else { return }
        switch result {
        case let .success(stream):
            liveStream = stream
        case let .failure(error):
            liveStartTask = nil
            liveStartFailure = RelayLiveStreamEnded(
                error: .streamFailed,
                code: Self.liveStartFailureCode(for: error),
                message: "The live stream could not start on the Mac."
            )
        }
    }

    private static func liveStartFailureCode(for error: any Error) -> String {
        switch error {
        case LiveRelayStreamStartError.workspaceUnavailable:
            "live_workspace_unavailable"
        case is WorkerLaunchConfigurationError:
            "live_worker_unavailable"
        default:
            "live_start_failed"
        }
    }

    /// Invalidates synchronously and records the one cleanup every stop awaits: cancel a start
    /// still in progress and stop whatever it returned, or stop the running stream.
    private func stopLiveStream(_ reason: LiveRelayStopReason) {
        guard liveSource != nil, liveStopTask == nil else { return }
        liveStopReason = reason
        let startTask = liveStartTask
        let stream = liveStream
        liveStartTask = nil
        liveStopTask = Task {
            if let stream {
                await stream.stop(reason)
            } else if let startTask {
                startTask.cancel()
                if let started = try? await startTask.value {
                    await started.stop(reason)
                }
            }
        }
    }

    /// Live routes are signed under the session derived for the stream named by the request's
    /// session ID. The current stream answers through `relayResponse`; any other stream ID gets a
    /// signed 410 `not_current`. Requests signed by the parent session itself get a signed 404,
    /// because the probe serves no fixture. A request that verifies under neither is unsigned.
    private func liveResponse(for request: RelayHTTPRequest) async throws -> RelayHTTPResponse {
        try ensurePairedSession()
        guard let parent = establishedSession else { throw RelayHostError.notPaired }
        let authentication = try decodeAuthentication(request)
        let session: RelayEstablishedSession
        var streamID: UUID?
        if authentication.sessionID == parent.sessionID {
            session = parent
        } else {
            guard let candidate = UUID(uuidString: authentication.sessionID.rawValue) else {
                throw RelaySessionError.invalidRequest
            }
            session = try parent.liveStreamSession(streamID: candidate)
            streamID = candidate
        }
        let exchange = try await authenticate(request, with: session)
        let response: RelayHTTPResponse
        if request.requestTarget == Self.finishPath {
            response = .empty(statusCode: 405)
        } else if streamID == nil {
            response = .empty(statusCode: 404)
        } else if streamID != liveStreamID {
            response = .liveStreamEnded(RelayLiveStreamEnded(
                error: .streamEnded,
                code: RelayLiveStreamEnded.notCurrentCode,
                message: nil
            ))
        } else if request.requestTarget.hasPrefix(Self.mediaPathPrefix),
                  !(request.header(named: Self.mediaCapabilityHeader).map(session.mediaCapability.matches) ?? false) {
            response = .empty(statusCode: 403)
        } else if let stream = liveStream {
            await stream.renewLease()
            response = await stream.relayResponse(for: request)
        } else if let liveStartFailure {
            response = .liveStreamEnded(liveStartFailure)
        } else if let liveStopReason {
            response = .liveStreamEnded(RelayLiveStreamEnded(error: .streamEnded, code: liveStopReason.rawValue, message: nil))
        } else {
            // Paired, but the start has not returned yet.
            response = RelayHTTPResponse(statusCode: 503, headers: ["retry-after": "1"])
        }
        return try authenticate(response, for: exchange)
    }

    private func renderPlaylist() -> String {
        let targetDurationSeconds = Int(ceil(playlist.targetDuration))
        let mediaSequence = playlist.segments.first?.sequenceNumber ?? playlist.nextSequenceNumber
        // No EXT-X-PLAYLIST-TYPE: the retained window evicts history, which an EVENT playlist
        // must never do.
        var lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
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

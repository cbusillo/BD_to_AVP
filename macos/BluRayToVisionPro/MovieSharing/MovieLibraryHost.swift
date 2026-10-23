import Foundation

/// What a movie host needs to serve live ISO playback. Without it, live routes answer 404 and the
/// catalog lists no live titles.
struct MovieLibraryLiveServices: Sendable {
    let admission: LiveStartAdmission
    let index: LiveTitleIndex
    let authorizer: LiveSourceAuthorizer
    /// True while the conversion queue has an active worker. Live playback still runs.
    let isBusyConverting: @Sendable () async -> Bool

    static func automatic(
        isBusyConverting: @escaping @Sendable () async -> Bool,
        policy: RelayRequestValidationPolicy
    ) throws -> MovieLibraryLiveServices {
        let authorizer = LiveSourceAuthorizer()
        return MovieLibraryLiveServices(
            admission: LiveStartAdmission(starter: try LiveRelayStreamStarter.automatic(), policy: policy),
            index: LiveTitleIndex(
                storeURL: LiveTitleIndex.defaultStoreURL(),
                authorizer: authorizer,
                inspector: WorkerLiveTitleInspector.automatic()
            ),
            authorizer: authorizer,
            isBusyConverting: isBusyConverting
        )
    }
}

/// The paired device a live stream is playing to, for the Mac's sharing panel.
struct MovieLiveActivity: Equatable, Sendable {
    let peerName: String
    let streamID: UUID
}

actor MovieLibraryHost: RelayNetworkHosting {
    private struct Pending {
        let context: RelayServerPairingContext
        let provisional: RelayEstablishedSession
        let peer: MovieTrustedPeer
    }
    private struct Access {
        let session: RelayEstablishedSession
        let peerID: String
        let peerName: String
    }
    private let catalog: MovieCatalog
    private let trust: MovieLibraryTrustStore
    private let privateKey: Data
    private let replay: RelayReplayNonceStore
    private let now: @Sendable () -> Date
    private let live: MovieLibraryLiveServices?
    private let advertisementID = RelaySessionIdentifier.random()
    private let limits = RelayHTTPParsingLimits(maximumBodyBytes: 8_192)
    private var context: RelayServerPairingContext?
    private var pending: Pending?
    private var sessions: [RelaySessionIdentifier: Access] = [:]
    private var contextStarts: [Date] = []
    private var stopped = false
    private var epoch = UUID()

    init(
        catalog: MovieCatalog,
        trust: MovieLibraryTrustStore,
        now: @escaping @Sendable () -> Date = { Date() },
        live: MovieLibraryLiveServices? = nil
    ) throws {
        self.catalog = catalog
        self.trust = trust
        self.now = now
        self.live = live
        privateKey = try trust.hostPrivateKey()
        replay = try RelayReplayNonceStore(capacity: 4_096)
    }

    func advertisedBonjourService() -> RelayBonjourAdvertisement? {
        stopped ? nil : RelayBonjourAdvertisement(sessionID: advertisementID, serviceType: MovieLibraryContract.serviceType)
    }

    func currentLifecycle() -> RelayHostLifecycle { stopped ? .stopped : .pairing }
    func stopForAppQuit() async { await stop(reason: .appQuit) }
    func cancel() async { await stop() }
    func networkLost() async { await stop() }
    /// Invalidates synchronously, then returns once any live stream's worker has exited.
    func stop() async { await stop(reason: .sharingStopped) }

    private func stop(reason: LiveRelayStopReason) async {
        stopped = true
        epoch = UUID()
        pending = nil
        context = nil
        let ended = Array(sessions.keys)
        sessions.removeAll()
        await catalog.stop()
        if let live {
            await live.index.stop()
            await live.admission.releaseAll(reason: reason)
            for sessionID in ended { await live.admission.endSession(sessionID, reason: reason) }
        }
    }

    /// The device a live stream is playing to, if one is.
    func liveActivity() async -> MovieLiveActivity? {
        guard let live else { return nil }
        for (sessionID, access) in sessions {
            if let current = await live.admission.current(for: sessionID) {
                return MovieLiveActivity(peerName: access.peerName, streamID: current.streamID)
            }
        }
        return nil
    }

    /// The Mac's Stop button: drains the live slot and waits for the worker to exit.
    func stopLivePlayback() async {
        await live?.admission.releaseAll(reason: .sharingStopped)
    }

    func pendingCandidate() async throws -> RelayPendingPairingCandidate? {
        guard !stopped, let context else { return nil }
        let candidate = try await context.pendingCandidateSummary(now: now())
        guard !stopped, self.context === context else { return nil }
        return candidate
    }

    func approve(_ id: RelayPairingCandidateIdentifier) async throws {
        guard !stopped, let context else { throw MovieLibraryError.unavailable }
        try await context.approve(candidateID: id, now: now())
        guard !stopped, self.context === context else { throw MovieLibraryError.unavailable }
    }

    func reject(_ id: RelayPairingCandidateIdentifier) async throws {
        guard !stopped, let context else { throw MovieLibraryError.unavailable }
        try await context.reject(candidateID: id, now: now())
        guard !stopped, self.context === context else { return }
        pending = nil
    }

    func forget(_ peerID: String) async throws {
        try trust.forgetClient(id: peerID)
        let ended = sessions.filter { $0.value.peerID == peerID }.map(\.key)
        sessions = sessions.filter { $0.value.peerID != peerID }
        if pending?.peer.id == peerID { pending = nil; context = nil }
        for sessionID in ended { await live?.admission.endSession(sessionID) }
    }

    func needsMoreRequestBytes(_ data: Data) -> Bool {
        do { _ = try RelayHTTPParser.parse(data, limits: limits); return false }
        catch RelayHTTPParseError.incomplete { return true }
        catch { return false }
    }

    func handle(_ data: Data, peer: RelayHostPeer) async -> RelayHTTPResponse {
        guard peer.isAllowed else { return .empty(statusCode: 403) }
        guard !stopped else { return .empty(statusCode: 503) }
        do {
            let request = try RelayHTTPParser.parse(data, limits: limits)
            return try await route(request)
        } catch is RelayHTTPParseError { return .empty(statusCode: 400) }
        catch is RelaySessionError { return .empty(statusCode: 401) }
        catch MovieLibraryError.revoked { return .empty(statusCode: 401) }
        catch { return .empty(statusCode: 503) }
    }

    private func newContext() throws -> RelayServerPairingContext {
        let date = now()
        contextStarts.removeAll { date.timeIntervalSince($0) >= 60 }
        guard contextStarts.count < 6 else { throw MovieLibraryError.unavailable }
        contextStarts.append(date)
        let value = try RelayServerPairingContext(serverPrivateKeyData: privateKey, retainsServerIdentity: true, now: date, sessionTTL: 24 * 60 * 60)
        context = value
        pending = nil
        return value
    }

    private func route(_ request: RelayHTTPRequest) async throws -> RelayHTTPResponse {
        let currentEpoch = epoch
        let expired = sessions.filter { now() > $0.value.session.expirationDate }.map(\.key)
        if !expired.isEmpty {
            sessions = sessions.filter { now() <= $0.value.session.expirationDate }
            for sessionID in expired { await live?.admission.endSession(sessionID) }
        }
        switch request.requestTarget {
        case MovieLibraryContract.challengePath:
            guard request.method == "GET", request.body.isEmpty else { return .empty(statusCode: 405) }
            var value = try context ?? newContext()
            let challenge: RelaySessionChallenge
            do { challenge = try await value.currentChallenge(now: now()) }
            catch RelaySessionError.pairingAttemptsExhausted {
                guard !stopped, epoch == currentEpoch, context === value else { throw MovieLibraryError.unavailable }
                value = try newContext()
                challenge = try await value.currentChallenge(now: now())
            }
            guard !stopped, epoch == currentEpoch, context === value else { throw MovieLibraryError.unavailable }
            return .json(MovieLibraryChallenge(challenge: challenge))
        case MovieLibraryContract.pairingPath:
            guard request.method == "POST", let context else { return .empty(statusCode: 405) }
            guard sessions.count < 4 else { return .empty(statusCode: 503) }
            let message = try JSONDecoder().decode(MoviePairingRequest.self, from: request.body)
            try message.validate()
            let result = try await context.accept(message.request, now: now())
            guard !stopped, epoch == currentEpoch, self.context === context else { throw MovieLibraryError.unavailable }
            let peer = MovieTrustedPeer(publicKey: message.request.clientPublicKey, name: message.name)
            pending = Pending(context: context, provisional: result.provisionalSession, peer: peer)
            // Recognition grants only the Mac's half of approval. The client must still
            // prove possession against this fresh challenge in the confirmation route.
            if try trust.trustedClients().contains(where: { $0.id == peer.id }) {
                try await context.approve(candidateID: result.candidate.candidateID, now: now())
                guard !stopped, epoch == currentEpoch, self.context === context else { throw MovieLibraryError.unavailable }
            }
            return .json(result.candidate)
        case MovieLibraryContract.confirmationPath:
            guard request.method == "POST", let pending else { return .empty(statusCode: 401) }
            let authentication = try await authenticate(request, session: pending.provisional)
            guard !stopped, epoch == currentEpoch, self.pending?.context === pending.context else { throw MovieLibraryError.unavailable }
            do {
                let confirmation = try JSONDecoder().decode(RelayPairingConfirmation.self, from: request.body)
                let result = try await pending.context.confirm(confirmation, now: now())
                guard !stopped, epoch == currentEpoch, self.pending?.context === pending.context else { throw MovieLibraryError.unavailable }
                if let session = result.session {
                    self.pending = nil
                    context = nil
                    try trust.trustClient(publicKey: pending.peer.publicKey, name: pending.peer.name)
                    sessions[session.sessionID] = Access(session: session, peerID: pending.peer.id, peerName: pending.peer.name)
                } else if result.response.state == .rejected {
                    self.pending = nil
                }
                return try signed(.json(result.response, statusCode: result.response.state == .waitingForMac ? 202 : 200), authentication, pending.provisional)
            } catch {
                return try signed(.empty(statusCode: 409), authentication, pending.provisional)
            }
        default:
            let authentication = try decodeAuthentication(request)
            guard let access = sessions[authentication.sessionID] else {
                return try await streamResponse(request, authentication: authentication, epoch: currentEpoch)
            }
            _ = try await authenticate(request, session: access.session)
            guard !stopped, epoch == currentEpoch, sessions[authentication.sessionID] != nil else { throw MovieLibraryError.revoked }
            let response: RelayHTTPResponse
            do {
                if request.requestTarget == MovieLibraryContract.catalogPath, request.method == "GET", request.body.isEmpty {
                    response = .json(try await catalogSnapshot())
                } else if request.requestTarget == MovieLibraryContract.forgetPath, request.method == "POST", request.body.isEmpty {
                    try await forget(access.peerID)
                    return try signed(.empty(statusCode: 204), authentication, access.session)
                } else if request.method == "GET", request.body.isEmpty, request.requestTarget.hasPrefix(MovieLibraryContract.bytesPrefix) {
                    let range = try MovieByteRequest(requestTarget: request.requestTarget)
                    response = RelayHTTPResponse(statusCode: 206, headers: ["content-type": "application/octet-stream"], body: try await catalog.read(range))
                } else if let live, request.requestTarget.hasPrefix("/movies/v1/live/") {
                    guard let liveResponse = try await liveRoute(request, authentication: authentication, live: live) else {
                        // The parent session ended while the request was in flight.
                        return .empty(statusCode: 401)
                    }
                    response = liveResponse
                } else { response = .empty(statusCode: 404) }
            } catch MovieLibraryError.sourceChanged { response = .empty(statusCode: 409) }
            catch MovieLibraryError.invalidRange { response = .empty(statusCode: 416) }
            catch is DecodingError { response = .empty(statusCode: 400) }
            catch MovieLibraryError.invalidResponse { response = .empty(statusCode: 400) }
            catch { response = .empty(statusCode: 503) }
            guard !stopped, epoch == currentEpoch, sessions[authentication.sessionID] != nil else { throw MovieLibraryError.revoked }
            return try signed(response, authentication, access.session)
        }
    }

    // MARK: - Catalog

    /// Reads the shared folders now and queues ISO checks, so the first headset request is fast
    /// and any macOS folder-access prompt appears while someone is at the Mac.
    func prepareCatalog() async throws {
        if let live {
            // Every read, including background rereads, queues checks for ISOs it finds.
            await catalog.observeReads { listing in
                _ = await live.index.titles(for: listing.liveCandidates)
            }
        }
        try await catalog.read()
    }

    /// Shared folders the last read could not open.
    func unreadableFolderIDs() async -> Set<String> {
        await catalog.unreadableRootIDs()
    }

    private func catalogSnapshot() async throws -> SharedMovieCatalog {
        let listing = try await catalog.listing()
        guard let live, !listing.liveCandidates.isEmpty else { return listing.catalog }
        let titles = await live.index.titles(for: listing.liveCandidates)
        let base = listing.catalog
        let combined = SharedMovieCatalog(roots: base.roots, movies: base.movies, truncated: base.truncated, liveTitles: titles)
        try combined.validate()
        guard try JSONEncoder().encode(combined).count < MovieLibraryContract.maximumCatalogBytes else {
            return SharedMovieCatalog(roots: base.roots, movies: base.movies, truncated: true)
        }
        return combined
    }

    // MARK: - Live routes on the parent session

    /// Returns nil when the parent session ended while the request was in flight; the caller
    /// then answers an unsigned 401.
    private func liveRoute(
        _ request: RelayHTTPRequest,
        authentication: RelayAuthenticatedRequest,
        live: MovieLibraryLiveServices
    ) async throws -> RelayHTTPResponse? {
        let parentID = authentication.sessionID
        switch request.requestTarget {
        case MovieLibraryContract.liveStartPath:
            guard request.method == "POST" else { return .empty(statusCode: 405) }
            let body = try JSONDecoder().decode(LiveStartRequest.self, from: request.body)
            try body.validate()
            let key = LiveStartKey(
                parentSessionID: parentID, startID: body.startID, issuedAtUnixMilliseconds: body.issuedAt,
                titleID: body.titleID, revision: body.revision, audioPID: body.audioPID
            )
            let catalog = catalog
            let decision = await live.admission.start(
                key,
                signedAt: authentication.timestampUnixMilliseconds,
                liveParentSessionIDs: Set(sessions.keys)
            ) {
                try await Self.prepareStart(body, catalog: catalog, live: live)
            }
            guard let decision else { return nil }
            return Self.response(for: decision)
        case MovieLibraryContract.liveCancelPath:
            guard request.method == "POST" else { return .empty(statusCode: 405) }
            let body = try JSONDecoder().decode(LiveStartReference.self, from: request.body)
            let outcome = await live.admission.cancel(
                parentSessionID: parentID, startID: body.startID,
                issuedAtUnixMilliseconds: body.issuedAt, signedAt: authentication.timestampUnixMilliseconds
            )
            switch outcome {
            case nil: return nil
            case .acknowledged?: return .empty(statusCode: LiveCancelOutcome.acknowledged.statusCode)
            case .retryLater?: return .json(LiveRequestRefusal(retryable: true), statusCode: LiveCancelOutcome.retryLater.statusCode)
            case .expired?: return .json(LiveRequestRefusal(retryable: false), statusCode: LiveCancelOutcome.expired.statusCode)
            }
        case MovieLibraryContract.liveReleasePath:
            guard request.method == "POST" else { return .empty(statusCode: 405) }
            let body = try JSONDecoder().decode(LiveStartReference.self, from: request.body)
            let drained = await live.admission.release(
                parentSessionID: parentID, startID: body.startID, issuedAtUnixMilliseconds: body.issuedAt
            )
            return .json(LiveReleaseResponse(drained: drained))
        default:
            guard request.requestTarget.hasPrefix(MovieLibraryContract.liveStatusPrefix),
                  request.method == "GET", request.body.isEmpty
            else { return .empty(statusCode: 404) }
            let raw = String(request.requestTarget.dropFirst(MovieLibraryContract.liveStatusPrefix.count))
            guard let streamID = UUID(uuidString: raw), streamID.uuidString.lowercased() == raw else {
                return .empty(statusCode: 404)
            }
            return .json(await status(of: streamID, parentID: parentID, live: live))
        }
    }

    private static func prepareStart(
        _ body: LiveStartRequest,
        catalog: MovieCatalog,
        live: MovieLibraryLiveServices
    ) async throws -> LiveStreamRequest {
        let unavailable = LiveStartPreparationRefusal(
            message: await live.index.failureReason(id: body.titleID, revision: body.revision)
                ?? "This title can't play live right now. Refresh the library and choose it again."
        )
        guard let candidate = await catalog.liveCandidate(id: body.titleID), candidate.revision == body.revision,
              let record = await live.index.record(id: body.titleID, revision: body.revision),
              record.eligible, let playlist = record.playlist,
              record.audio.contains(where: { $0.pid == body.audioPID })
        else { throw unavailable }
        do {
            let lease = try await live.authorizer.authorize(candidate, revision: body.revision)
            return LiveStreamRequest(source: lease, playlist: playlist, audioPID: body.audioPID)
        } catch let error as LiveSourceAuthorizationError {
            throw LiveStartPreparationRefusal(message: error.message)
        }
    }

    private static func response(for decision: LiveStartDecision) -> RelayHTTPResponse {
        let status = decision.outcome.statusCode
        switch decision.outcome {
        case let .started(streamID):
            return .json(LiveStartResponse(streamID: streamID), statusCode: status)
        case .busy:
            return .json(LiveStartRefusal(outcome: .busy, message: "The Mac is already streaming to another device."), statusCode: status)
        case .cancelled:
            return .json(LiveStartRefusal(outcome: .cancelled), statusCode: status)
        case .conflict:
            return .json(LiveStartRefusal(outcome: .conflict), statusCode: status)
        case .sourceChanged:
            return .json(LiveStartRefusal(outcome: .sourceChanged, message: decision.message), statusCode: status)
        case .rateLimited:
            return .json(LiveStartRefusal(outcome: .rateLimited), statusCode: status)
        case .expired:
            return .json(LiveRequestRefusal(retryable: false), statusCode: status)
        }
    }

    private func status(of streamID: UUID, parentID: RelaySessionIdentifier, live: MovieLibraryLiveServices) async -> LiveStreamStatus {
        let busy = await live.isBusyConverting()
        if let current = await live.admission.current(for: parentID), current.streamID == streamID {
            guard let stream = current.stream else { return LiveStreamStatus(state: .starting, macBusyConverting: busy) }
            await stream.renewLease()
            switch await stream.state() {
            case .starting:
                return LiveStreamStatus(state: .starting, macBusyConverting: busy)
            case let .live(earliest, latest):
                return LiveStreamStatus(state: .live, earliestMilliseconds: earliest, latestMilliseconds: latest, macBusyConverting: busy)
            case let .complete(earliest, latest):
                return LiveStreamStatus(state: .complete, earliestMilliseconds: earliest, latestMilliseconds: latest, macBusyConverting: busy)
            case let .failed(code, message):
                return LiveStreamStatus(state: .failed, failure: .init(code: code, message: message), macBusyConverting: busy)
            case let .stopped(reason):
                return LiveStreamStatus(state: .stopped, stopReason: reason.rawValue, macBusyConverting: busy)
            }
        }
        if let ended = await live.admission.ended(streamID, for: parentID) {
            switch ended.body.error {
            case .streamFailed:
                return LiveStreamStatus(
                    state: .failed,
                    failure: .init(code: ended.body.code ?? "live_hls_failed", message: ended.body.message ?? "The live stream stopped on the Mac."),
                    macBusyConverting: busy
                )
            case .streamEnded:
                return LiveStreamStatus(state: .stopped, stopReason: ended.body.code, macBusyConverting: busy)
            }
        }
        return LiveStreamStatus(state: .unknown, macBusyConverting: busy)
    }

    // MARK: - Live routes on a derived stream session

    /// A request whose session ID names no parent session is a candidate stream ID: the derived
    /// stream session's ID is the stream ID. Re-derive under each live parent session (at most
    /// four) and let the first that verifies decide. The current stream answers through
    /// `relayResponse`; any other stream ID gets a signed 410. When nothing verifies, the parent
    /// session is gone or the request is forged, and the answer is an unsigned 401.
    private func streamResponse(
        _ request: RelayHTTPRequest,
        authentication: RelayAuthenticatedRequest,
        epoch currentEpoch: UUID
    ) async throws -> RelayHTTPResponse {
        guard let live, let streamID = UUID(uuidString: authentication.sessionID.rawValue) else {
            return .empty(statusCode: 401)
        }
        var verified: (parentID: RelaySessionIdentifier, session: RelayEstablishedSession)?
        for (parentID, access) in sessions {
            guard let derived = try? access.session.liveStreamSession(streamID: streamID) else { continue }
            if (try? await authenticate(request, session: derived)) != nil {
                verified = (parentID, derived)
                break
            }
        }
        guard let verified else { return .empty(statusCode: 401) }
        guard !stopped, epoch == currentEpoch, sessions[verified.parentID] != nil else { throw MovieLibraryError.revoked }
        let response: RelayHTTPResponse
        if !LiveRelayRouting.isStreamRoute(request.requestTarget) {
            response = .empty(statusCode: 404)
        } else if !LiveRelayRouting.carriesCapability(request, of: verified.session) {
            response = .empty(statusCode: 403)
        } else if let current = await live.admission.current(for: verified.parentID), current.streamID == streamID {
            if let stream = current.stream {
                await stream.renewLease()
                response = await stream.relayResponse(for: request)
            } else {
                // Admitted; the start has not returned yet.
                response = RelayHTTPResponse(statusCode: 503, headers: ["retry-after": "1"])
            }
        } else {
            response = LiveRelayRouting.notCurrent(await live.admission.ended(streamID, for: verified.parentID))
        }
        guard !stopped, epoch == currentEpoch, sessions[verified.parentID] != nil else { throw MovieLibraryError.revoked }
        return try signed(response, authentication, verified.session)
    }

    private func decodeAuthentication(_ request: RelayHTTPRequest) throws -> RelayAuthenticatedRequest {
        guard let header = request.header(named: RelayWireContract.authenticationHeader),
              header.utf8.count <= RelayWireContract.maximumAuthenticationHeaderBytes,
              let data = Data(base64Encoded: header) else { throw RelaySessionError.invalidRequest }
        return try JSONDecoder().decode(RelayAuthenticatedRequest.self, from: data)
    }

    private func authenticate(_ request: RelayHTTPRequest, session: RelayEstablishedSession) async throws -> RelayAuthenticatedRequest {
        let authentication = try decodeAuthentication(request)
        try await session.verify(authentication, actualMethod: request.method, actualRequestTarget: request.requestTarget, body: request.body, now: now(), replayStore: replay)
        return authentication
    }

    private func signed(_ response: RelayHTTPResponse, _ request: RelayAuthenticatedRequest, _ session: RelayEstablishedSession) throws -> RelayHTTPResponse {
        let proof = try session.authenticateResponse(requestNonce: request.nonce, statusCode: response.statusCode, body: response.body)
        let header = try JSONEncoder().encode(proof).base64EncodedString()
        guard header.utf8.count <= RelayWireContract.maximumAuthenticationHeaderBytes else { throw RelaySessionError.invalidResponse }
        return RelayHTTPResponse(statusCode: response.statusCode, headers: response.headers.merging([RelayWireContract.responseAuthenticationHeader: header]) { _, new in new }, body: response.body)
    }
}

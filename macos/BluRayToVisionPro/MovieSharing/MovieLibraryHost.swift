import Foundation

actor MovieLibraryHost: RelayNetworkHosting {
    private struct Pending {
        let context: RelayServerPairingContext
        let provisional: RelayEstablishedSession
        let peer: MovieTrustedPeer
    }
    private struct Access {
        let session: RelayEstablishedSession
        let peerID: String
    }
    private let catalog: MovieCatalog
    private let trust: MovieLibraryTrustStore
    private let privateKey: Data
    private let replay: RelayReplayNonceStore
    private let now: @Sendable () -> Date
    private let advertisementID = RelaySessionIdentifier.random()
    private let limits = RelayHTTPParsingLimits(maximumBodyBytes: 8_192)
    private var context: RelayServerPairingContext?
    private var pending: Pending?
    private var sessions: [RelaySessionIdentifier: Access] = [:]
    private var contextStarts: [Date] = []
    private var stopped = false
    private var epoch = UUID()

    init(catalog: MovieCatalog, trust: MovieLibraryTrustStore, now: @escaping @Sendable () -> Date = Date.init) throws {
        self.catalog = catalog
        self.trust = trust
        self.now = now
        privateKey = try trust.hostPrivateKey()
        replay = try RelayReplayNonceStore(capacity: 4_096)
    }

    func advertisedBonjourService() -> RelayBonjourAdvertisement? {
        stopped ? nil : RelayBonjourAdvertisement(sessionID: advertisementID, serviceType: MovieLibraryContract.serviceType)
    }

    func currentLifecycle() -> RelayHostLifecycle { stopped ? .stopped : .pairing }
    func stopForAppQuit() async { await stop() }
    func cancel() async { await stop() }
    func networkLost() async { await stop() }
    func stop() async {
        stopped = true
        epoch = UUID()
        pending = nil
        context = nil
        sessions.removeAll()
        await catalog.stop()
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

    func forget(_ peerID: String) throws {
        try trust.forgetClient(id: peerID)
        sessions = sessions.filter { $0.value.peerID != peerID }
        if pending?.peer.id == peerID { pending = nil; context = nil }
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
        sessions = sessions.filter { now() <= $0.value.session.expirationDate }
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
                    sessions[session.sessionID] = Access(session: session, peerID: pending.peer.id)
                } else if result.response.state == .rejected {
                    self.pending = nil
                }
                return try signed(.json(result.response, statusCode: result.response.state == .waitingForMac ? 202 : 200), authentication, pending.provisional)
            } catch {
                return try signed(.empty(statusCode: 409), authentication, pending.provisional)
            }
        default:
            let authentication = try decodeAuthentication(request)
            guard let access = sessions[authentication.sessionID] else { return .empty(statusCode: 401) }
            _ = try await authenticate(request, session: access.session)
            guard !stopped, epoch == currentEpoch, sessions[authentication.sessionID] != nil else { throw MovieLibraryError.revoked }
            let response: RelayHTTPResponse
            do {
                if request.requestTarget == MovieLibraryContract.catalogPath, request.method == "GET", request.body.isEmpty {
                    response = .json(try await catalog.snapshot())
                } else if request.requestTarget == MovieLibraryContract.forgetPath, request.method == "POST", request.body.isEmpty {
                    try forget(access.peerID)
                    return try signed(.empty(statusCode: 204), authentication, access.session)
                } else if request.method == "GET", request.body.isEmpty, request.requestTarget.hasPrefix(MovieLibraryContract.bytesPrefix) {
                    let range = try MovieByteRequest(requestTarget: request.requestTarget)
                    response = RelayHTTPResponse(statusCode: 206, headers: ["content-type": "application/octet-stream"], body: try await catalog.read(range))
                } else { response = .empty(statusCode: 404) }
            } catch MovieLibraryError.sourceChanged { response = .empty(statusCode: 409) }
            catch MovieLibraryError.invalidRange { response = .empty(statusCode: 416) }
            catch { response = .empty(statusCode: 503) }
            guard !stopped, epoch == currentEpoch, sessions[authentication.sessionID] != nil else { throw MovieLibraryError.revoked }
            return try signed(response, authentication, access.session)
        }
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

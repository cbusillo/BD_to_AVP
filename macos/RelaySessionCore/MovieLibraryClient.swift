import CryptoKit
import Foundation

struct MoviePairingPrompt: Sendable {
    let code: String
    let isRemembered: Bool
}

/// What the Mac answered to a live start.
enum LiveStartResult: Equatable, Sendable {
    case started(LiveStartResponse)
    case refused(LiveStartRefusal)
    /// Signed 422: the request was signed outside its start identity's window. Not retryable.
    case expired
}

actor MovieLibraryClient {
    private struct Pending {
        let provisional: RelayProvisionalSession
        let serverPublicKey: Data
        let clientPrivateKey: Data
    }
    private let baseURL: URL
    private let name: String
    private let transport: any MovieHTTPTransport
    private let trust: MovieLibraryTrustStore
    private let clock: @Sendable () -> Date
    private var pending: Pending?
    private var session: RelayEstablishedSession?
    private var serverPublicKey: Data?
    private var epoch = UUID()

    init(
        baseURL: URL, name: String, transport: any MovieHTTPTransport = BoundedMovieHTTPTransport(),
        trust: MovieLibraryTrustStore = MovieLibraryTrustStore(), clock: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.baseURL = baseURL
        self.name = name
        self.transport = transport
        self.trust = trust
        self.clock = clock
    }

    /// The Mac's base URL, which also serves a live stream's relay routes.
    nonisolated var serverBaseURL: URL { baseURL }

    /// For tests: install an established session without pairing.
    func installSession(_ established: RelayEstablishedSession) {
        session = established
    }

    func disconnect() {
        epoch = UUID()
        pending = nil
        session = nil
    }

    func beginPairing() async throws -> MoviePairingPrompt {
        disconnect()
        let attemptEpoch = epoch
        let (challengeData, _) = try await exchange(path: MovieLibraryContract.challengePath)
        try ensureCurrent(attemptEpoch)
        let envelope = try JSONDecoder().decode(MovieLibraryChallenge.self, from: challengeData)
        guard envelope.version == MovieLibraryContract.version else { throw MovieLibraryError.incompatible }
        let remembered = try trust.clientPrivateKey(for: envelope.challenge.serverPublicKey)
        let clientKey = remembered ?? Curve25519.KeyAgreement.PrivateKey().rawRepresentation
        let attempt = try RelayClientPairingAttempt(challenge: envelope.challenge, clientPrivateKeyData: clientKey, clientNonce: RelayCrypto.randomBytes(count: 32), now: Date())
        let request = MoviePairingRequest(request: attempt.request, name: "Vision Pro")
        let (candidateData, _) = try await exchange(path: MovieLibraryContract.pairingPath, method: "POST", body: JSONEncoder().encode(request))
        try ensureCurrent(attemptEpoch)
        let candidate = try JSONDecoder().decode(RelayPairingCandidate.self, from: candidateData)
        let provisional = try attempt.complete(with: candidate, now: Date())
        pending = Pending(provisional: provisional, serverPublicKey: envelope.challenge.serverPublicKey, clientPrivateKey: clientKey)
        return MoviePairingPrompt(code: provisional.shortAuthenticationString.formattedDigits, isRemembered: remembered != nil)
    }

    /// True only after both devices approved and the server acceptance was verified.
    func confirm(matches: Bool) async throws -> Bool {
        guard let pending else { throw MovieLibraryError.needsPairing }
        let attemptEpoch = epoch
        let confirmation = try pending.provisional.confirmation(decision: matches ? .codesMatch : .notMyMac)
        let (data, _) = try await exchange(path: MovieLibraryContract.confirmationPath, method: "POST", body: JSONEncoder().encode(confirmation), authentication: pending.provisional.authenticationSession)
        try ensureCurrent(attemptEpoch)
        let result = try JSONDecoder().decode(RelayPairingConfirmationResponse.self, from: data)
        guard result.candidateID == pending.provisional.candidateID else { throw MovieLibraryError.invalidResponse }
        if result.state == .rejected { disconnect(); return false }
        guard result.state == .established, let acceptance = result.acceptance else { return false }
        let established = try pending.provisional.complete(with: acceptance, now: Date())
        try trust.trustServer(publicKey: pending.serverPublicKey, name: name, clientPrivateKey: pending.clientPrivateKey)
        session = established
        serverPublicKey = pending.serverPublicKey
        self.pending = nil
        return true
    }

    func catalog() async throws -> SharedMovieCatalog {
        guard let session else { throw MovieLibraryError.needsPairing }
        let attemptEpoch = epoch
        let (data, _) = try await exchange(path: MovieLibraryContract.catalogPath, authentication: session)
        try ensureCurrent(attemptEpoch)
        let catalog = try JSONDecoder().decode(SharedMovieCatalog.self, from: data)
        try catalog.validate()
        return catalog
    }

    func bytes(movie: SharedMovie, offset: Int64, count: Int) async throws -> Data {
        guard let session else { throw MovieLibraryError.needsPairing }
        let attemptEpoch = epoch
        let request = try MovieByteRequest(movie: movie, offset: offset, count: count)
        let (data, status) = try await exchange(path: request.requestTarget, authentication: session)
        try ensureCurrent(attemptEpoch)
        guard status == 206, data.count == count else { throw MovieLibraryError.invalidResponse }
        return data
    }

    func forget() async throws {
        let key = serverPublicKey ?? pending?.serverPublicKey
        guard let key else { disconnect(); return }
        // Local revocation works offline. A successful request also removes the Mac's grant.
        try trust.forgetServer(publicKey: key)
        let active = session
        disconnect()
        if let active {
            _ = try? await exchange(path: MovieLibraryContract.forgetPath, method: "POST", authentication: active)
        }
    }

    // MARK: - Live playback

    /// Sends a live start signed at `signedAt`. The first request for a start ID is signed at
    /// `request.issuedAt`; a retry repeats the same body with a fresh signature.
    func startLive(_ request: LiveStartRequest, signedAt: Date) async throws -> LiveStartResult {
        guard let session else { throw MovieLibraryError.needsPairing }
        let attemptEpoch = epoch
        let (data, status) = try await verifiedExchange(
            path: MovieLibraryContract.liveStartPath, method: "POST", body: JSONEncoder().encode(request),
            authentication: session, timestamp: signedAt
        )
        try ensureCurrent(attemptEpoch)
        switch status {
        case 202:
            let response = try JSONDecoder().decode(LiveStartResponse.self, from: data)
            guard response.liveVersion == MovieLibraryContract.liveVersion else { throw MovieLibraryError.incompatible }
            return .started(response)
        case 409, 429:
            return .refused(try JSONDecoder().decode(LiveStartRefusal.self, from: data))
        case 422:
            return .expired
        default:
            throw Self.error(forStatus: status)
        }
    }

    func cancelLive(_ reference: LiveStartReference) async throws -> LiveCancelOutcome {
        guard let session else { throw MovieLibraryError.needsPairing }
        let (data, status) = try await verifiedExchange(
            path: MovieLibraryContract.liveCancelPath, method: "POST", body: JSONEncoder().encode(reference),
            authentication: session
        )
        switch status {
        case 204:
            return .acknowledged
        case 422, 429:
            let refusal = try JSONDecoder().decode(LiveRequestRefusal.self, from: data)
            return refusal.retryable ? .retryLater : .expired
        default:
            throw Self.error(forStatus: status)
        }
    }

    /// True when the Mac released a slot held by exactly this start identity.
    func releaseLive(_ reference: LiveStartReference) async throws -> Bool {
        guard let session else { throw MovieLibraryError.needsPairing }
        let (data, status) = try await verifiedExchange(
            path: MovieLibraryContract.liveReleasePath, method: "POST", body: JSONEncoder().encode(reference),
            authentication: session
        )
        guard status == 200 else { throw Self.error(forStatus: status) }
        return try JSONDecoder().decode(LiveReleaseResponse.self, from: data).drained
    }

    func liveStatus(streamID: UUID) async throws -> LiveStreamStatus {
        guard let session else { throw MovieLibraryError.needsPairing }
        let (data, status) = try await verifiedExchange(
            path: MovieLibraryContract.liveStatusPrefix + streamID.uuidString.lowercased(), authentication: session
        )
        guard status == 200 else { throw Self.error(forStatus: status) }
        return try JSONDecoder().decode(LiveStreamStatus.self, from: data)
    }

    /// The session that signs every relay request for one live stream, and the URL that serves
    /// them. The parent session signs only the `/movies/v1/...` routes.
    func liveStreamSession(streamID: UUID) throws -> (session: RelayEstablishedSession, baseURL: URL) {
        guard let session else { throw MovieLibraryError.needsPairing }
        return (try session.liveStreamSession(streamID: streamID), baseURL)
    }

    private static func error(forStatus status: Int) -> MovieLibraryError {
        switch status {
        case 401: .needsPairing
        case 409: .sourceChanged
        case 416: .invalidRange
        default: .unavailable
        }
    }

    private func ensureCurrent(_ value: UUID) throws {
        try Task.checkCancellation()
        guard epoch == value else { throw CancellationError() }
    }

    private func exchange(path: String, method: String = "GET", body: Data = Data(), authentication: RelayEstablishedSession? = nil) async throws -> (Data, Int) {
        let (data, status) = try await verifiedExchange(path: path, method: method, body: body, authentication: authentication)
        switch status {
        case 200, 202, 204, 206: return (data, status)
        default: throw Self.error(forStatus: status)
        }
    }

    /// Sends a request and verifies a signed response, returning any status. An unsigned 401
    /// means the session ended (`needsPairing`); any other unsigned answer is invalid.
    private func verifiedExchange(
        path: String, method: String = "GET", body: Data = Data(),
        authentication: RelayEstablishedSession? = nil, timestamp: Date? = nil
    ) async throws -> (Data, Int) {
        guard baseURL.scheme == "http", baseURL.user == nil, baseURL.password == nil,
              baseURL.query == nil, baseURL.fragment == nil,
              let url = URL(string: path, relativeTo: baseURL)?.absoluteURL,
              url.host == baseURL.host, url.port == baseURL.port
        else { throw MovieLibraryError.invalidResponse }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 15)
        request.httpMethod = method
        request.httpBody = body.isEmpty ? nil : body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        let nonce = UUID().uuidString.lowercased()
        if let authentication {
            let proof = try authentication.signRequest(method: method, requestTarget: path, timestamp: timestamp ?? clock(), nonce: nonce, body: body)
            request.setValue(try JSONEncoder().encode(proof).base64EncodedString(), forHTTPHeaderField: RelayWireContract.authenticationHeader)
        }
        let (data, response) = try await transport.data(for: request, maximumBytes: MovieLibraryContract.maximumChunkBytes)
        try Task.checkCancellation()
        if let authentication {
            guard let header = response.value(forHTTPHeaderField: RelayWireContract.responseAuthenticationHeader),
                  header.utf8.count <= RelayWireContract.maximumAuthenticationHeaderBytes,
                  let proofData = Data(base64Encoded: header) else {
                if response.statusCode == 401 { throw MovieLibraryError.needsPairing }
                throw MovieLibraryError.invalidResponse
            }
            let proof = try JSONDecoder().decode(RelayAuthenticatedResponse.self, from: proofData)
            try authentication.verifyResponse(proof, requestNonce: nonce, actualStatusCode: response.statusCode, body: data, now: clock())
        }
        return (data, response.statusCode)
    }
}

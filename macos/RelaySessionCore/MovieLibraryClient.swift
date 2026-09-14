import CryptoKit
import Foundation

struct MoviePairingPrompt: Sendable {
    let code: String
    let isRemembered: Bool
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
    private var pending: Pending?
    private var session: RelayEstablishedSession?
    private var serverPublicKey: Data?
    private var epoch = UUID()

    init(baseURL: URL, name: String, transport: any MovieHTTPTransport = BoundedMovieHTTPTransport(), trust: MovieLibraryTrustStore = MovieLibraryTrustStore()) {
        self.baseURL = baseURL
        self.name = name
        self.transport = transport
        self.trust = trust
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

    private func ensureCurrent(_ value: UUID) throws {
        try Task.checkCancellation()
        guard epoch == value else { throw CancellationError() }
    }

    private func exchange(path: String, method: String = "GET", body: Data = Data(), authentication: RelayEstablishedSession? = nil) async throws -> (Data, Int) {
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
            let proof = try authentication.signRequest(method: method, requestTarget: path, timestamp: Date(), nonce: nonce, body: body)
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
            try authentication.verifyResponse(proof, requestNonce: nonce, actualStatusCode: response.statusCode, body: data, now: Date())
        }
        switch response.statusCode {
        case 200, 202, 204, 206: return (data, response.statusCode)
        case 401: throw MovieLibraryError.needsPairing
        case 409: throw MovieLibraryError.sourceChanged
        case 416: throw MovieLibraryError.invalidRange
        default: throw MovieLibraryError.unavailable
        }
    }
}

import Combine
import Foundation
import Network

enum RelayCoordinatorState: Equatable {
    case idle
    case discovery
    case confirming(serverID: String, candidateID: String, expiresAt: Date)
    case connected(sessionID: String, expiresAt: Date)
    case reconnecting(attempt: Int)
    case networkUnavailable
    case sessionExpired
    case failed(String)
}

enum RelayNetworkAvailability: Sendable { case available, unavailable }

struct RelayRemotePlaybackConfiguration {
    let session: RelayEstablishedSession
    let serverBaseURL: URL
    let serverName: String
    let transport: any RelayTransport
}

enum RelayBackoff {
    static let maximumAttempts = 3
    static let base: TimeInterval = 0.25
    static let maximum: TimeInterval = 2
    static func delay(for attempt: Int) -> TimeInterval {
        guard attempt > 0 else { return 0 }
        return min(base * pow(2, Double(attempt - 1)), maximum)
    }
}

@MainActor
final class RelaySessionCoordinator: ObservableObject {
    @Published private(set) var state: RelayCoordinatorState = .idle
    @Published private(set) var discoveredServers: [RelayDiscoveredEndpoint] = []
    @Published private(set) var connectedServer: RelayDiscoveredEndpoint?
    @Published private(set) var shortAuthenticationString: RelayShortAuthenticationString?
    @Published private(set) var isWaitingForMacConfirmation = false

    private(set) var session: RelayEstablishedSession?
    private(set) var connectedServerBaseURL: URL?

    private let transport: any RelayTransport
    private let browserFactory: @Sendable () -> any RelayEndpointBrowsing
    private let clock: @Sendable () -> Date
    private let nonce: @Sendable () -> String
    private var browser: (any RelayEndpointBrowsing)?
    private var browsingTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?
    private var confirmationPollingTask: Task<Void, Never>?
    private var pathMonitor: NWPathMonitor?
    private var provisionalSession: RelayProvisionalSession?

    init(
        browserFactory: @escaping @Sendable () -> any RelayEndpointBrowsing = { RelayBonjourBrowser() },
        transport: any RelayTransport = URLSessionRelayTransport(),
        clock: @escaping @Sendable () -> Date = { Date() },
        nonce: @escaping @Sendable () -> String = { UUID().uuidString }
    ) {
        self.browserFactory = browserFactory
        self.transport = transport
        self.clock = clock
        self.nonce = nonce
    }

    func startDiscovery() {
        guard state == .idle || state == .sessionExpired || isFailed else { return }
        cleanUp()
        let newBrowser = browserFactory()
        browser = newBrowser
        state = .discovery
        browsingTask = Task { @MainActor [weak self, newBrowser] in
            for await endpoints in newBrowser.discoveryStream where !Task.isCancelled {
                self?.discoveredServers = endpoints
            }
        }
        newBrowser.startBrowsing()
    }

    func connect(to endpoint: RelayDiscoveredEndpoint) async {
        guard state == .discovery else { return }
        do {
            let challengeRequest = URLRequest(relayURL: endpoint.baseURL, path: RelayWireContract.challengePath)
            let (challengeData, challengeResponse) = try await transport.data(for: challengeRequest)
            guard challengeResponse.statusCode == 200 else {
                try handleSessionStatus(challengeResponse.statusCode)
                throw RelayTransportError.unexpectedStatusCode(challengeResponse.statusCode)
            }
            let challenge = try JSONDecoder().decode(RelayChallengeEnvelope.self, from: challengeData).challenge
            guard challenge.expirationDate > clock() else { state = .sessionExpired; return }
            let attempt = try RelayClientPairingAttempt(challenge: challenge, now: clock())
            var pairingRequest = URLRequest(relayURL: endpoint.baseURL, path: RelayWireContract.pairingPath)
            pairingRequest.httpMethod = "POST"
            pairingRequest.httpBody = try JSONEncoder().encode(attempt.request)
            pairingRequest.setValue(RelayWireContract.jsonContentType, forHTTPHeaderField: "content-type")
            let (candidateData, candidateResponse) = try await transport.data(for: pairingRequest)
            guard candidateResponse.statusCode == 201 else {
                try handleSessionStatus(candidateResponse.statusCode)
                throw RelayTransportError.unexpectedStatusCode(candidateResponse.statusCode)
            }
            let candidate = try JSONDecoder().decode(RelayPairingCandidateEnvelope.self, from: candidateData).candidate
            let provisionalSession = try attempt.complete(with: candidate, now: clock())
            self.provisionalSession = provisionalSession
            shortAuthenticationString = provisionalSession.shortAuthenticationString
            isWaitingForMacConfirmation = false
            connectedServerBaseURL = endpoint.baseURL
            connectedServer = endpoint
            state = .confirming(
                serverID: endpoint.id,
                candidateID: provisionalSession.candidateID.rawValue,
                expiresAt: candidate.expirationDate
            )
        } catch RelayTransportError.sessionExpired {
            state = .sessionExpired
        } catch {
            state = .failed("Unable to start numeric comparison: \(error.localizedDescription)")
        }
    }

    func confirmCodesMatch() async {
        await submitConfirmation(.codesMatch, beginPolling: true)
    }

    func rejectCandidate() async {
        await submitConfirmation(.notMyMac, beginPolling: false)
    }

    func disconnect() {
        cleanUp()
        state = .idle
    }

    func handleNetworkAvailability(_ availability: RelayNetworkAvailability) {
        switch availability {
        case .unavailable:
            reconnectTask?.cancel()
            reconnectTask = nil
            guard hasLiveSession else {
                state = .sessionExpired
                return
            }
            state = .networkUnavailable
        case .available:
            guard state == .networkUnavailable || isReconnecting else { return }
            startReconnect(attempt: 0)
        }
    }

    func remotePlaybackConfiguration() -> RelayRemotePlaybackConfiguration? {
        guard let session, let connectedServerBaseURL, let connectedServer else { return nil }
        guard session.expirationDate > clock() else {
            self.session = nil
            state = .sessionExpired
            return nil
        }
        return RelayRemotePlaybackConfiguration(
            session: session,
            serverBaseURL: connectedServerBaseURL,
            serverName: connectedServer.displayName,
            transport: transport
        )
    }

    private func submitConfirmation(_ decision: RelayPairingConfirmationDecision, beginPolling: Bool) async {
        guard case let .confirming(_, _, expiresAt) = state else { return }
        guard expiresAt > clock() else {
            clearPendingPairingSelection()
            state = .sessionExpired
            return
        }
        guard
              let provisionalSession,
              let baseURL = connectedServerBaseURL
        else { return }
        do {
            let confirmation = try provisionalSession.confirmation(decision: decision)
            let body = try JSONEncoder().encode(confirmation)
            let prepared = try RelayAuthenticatedRequestFactory.makeRequest(
                baseURL: baseURL,
                path: RelayWireContract.pairingConfirmPath,
                method: "POST",
                body: body,
                signer: provisionalSession.authenticationSession,
                clock: clock,
                nonce: nonce
            )
            let (data, response) = try await transport.data(for: prepared.request)
            try RelayAuthenticatedResponseVerifier.verify(
                data: data,
                response: response,
                request: prepared,
                signer: provisionalSession.authenticationSession,
                now: clock()
            )
            guard response.statusCode == 200 || response.statusCode == 202 else {
                try handleSessionStatus(response.statusCode)
                throw RelayTransportError.unexpectedStatusCode(response.statusCode)
            }
            let result = try JSONDecoder().decode(RelayPairingConfirmationEnvelope.self, from: data).confirmation
            guard result.candidateID == provisionalSession.candidateID else { throw RelaySessionError.pairingCandidateNotFound }
            switch result.state {
            case .waitingForMac:
                isWaitingForMacConfirmation = true
                if beginPolling { beginConfirmationPolling() }
            case .rejected:
                clearPendingPairingSelection()
                state = .discovery
            case .established:
                guard let acceptance = result.acceptance else { throw RelaySessionError.invalidResponse }
                session = try provisionalSession.complete(with: acceptance, now: clock())
                clearPendingPairing()
                stopBrowser()
                startPathMonitor()
                guard let session else { return }
                state = .connected(sessionID: session.sessionID.rawValue, expiresAt: session.expirationDate)
            }
        } catch RelayTransportError.sessionExpired {
            clearPendingPairing()
            state = .sessionExpired
        } catch RelayTransportError.unpaired {
            clearPendingPairingSelection()
            state = .discovery
        } catch {
            state = .failed("Unable to confirm numeric comparison: \(error.localizedDescription)")
        }
    }

    private func beginConfirmationPolling() {
        guard confirmationPollingTask == nil else { return }
        confirmationPollingTask = Task { @MainActor [weak self] in
            defer { self?.confirmationPollingTask = nil }
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(500))
                guard !Task.isCancelled, let self, case .confirming = self.state else { return }
                await self.submitConfirmation(.codesMatch, beginPolling: false)
                if self.state == .sessionExpired || self.isFailed { return }
            }
        }
    }

    private var isFailed: Bool { if case .failed = state { return true }; return false }
    private var isReconnecting: Bool { if case .reconnecting = state { return true }; return false }
    private var hasLiveSession: Bool { session?.expirationDate ?? .distantPast > clock() }

    private func startReconnect(attempt: Int) {
        guard hasLiveSession else { state = .sessionExpired; return }
        guard attempt <= RelayBackoff.maximumAttempts else {
            state = .failed("Relay did not reconnect after \(RelayBackoff.maximumAttempts) attempts.")
            return
        }
        reconnectTask?.cancel()
        state = .reconnecting(attempt: attempt)
        reconnectTask = Task { @MainActor [weak self] in
            guard let self else { return }
            try? await Task.sleep(for: .seconds(RelayBackoff.delay(for: attempt)))
            guard !Task.isCancelled, self.isReconnecting, self.hasLiveSession else { return }
            do {
                try await self.probeExistingSession()
                if let session = self.session { self.state = .connected(sessionID: session.sessionID.rawValue, expiresAt: session.expirationDate) }
            } catch RelayTransportError.sessionExpired, RelayTransportError.unpaired {
                self.session = nil
                self.state = .sessionExpired
            } catch {
                self.startReconnect(attempt: attempt + 1)
            }
        }
    }

    private func probeExistingSession() async throws {
        guard let session, let baseURL = connectedServerBaseURL else { throw RelayTransportError.unpaired }
        let prepared = try RelayAuthenticatedRequestFactory.makeRequest(
            baseURL: baseURL, path: RelayWireContract.playlistSnapshotPath, signer: session, clock: clock, nonce: nonce
        )
        var request = prepared.request
        request.setValue(session.mediaCapability.value, forHTTPHeaderField: RelayWireContract.mediaCapabilityHeader)
        let (data, response) = try await transport.data(for: request)
        try RelayAuthenticatedResponseVerifier.verify(
            data: data, response: response,
            request: RelayPreparedRequest(request: request, authentication: prepared.authentication), signer: session, now: clock()
        )
        guard response.statusCode == 200 else { try handleSessionStatus(response.statusCode); throw RelayTransportError.unexpectedStatusCode(response.statusCode) }
    }

    private func handleSessionStatus(_ statusCode: Int) throws {
        switch statusCode {
        case 410: throw RelayTransportError.sessionExpired
        case 503: throw RelayTransportError.unpaired
        default: return
        }
    }

    private func startPathMonitor() {
        pathMonitor?.cancel()
        let monitor = NWPathMonitor()
        pathMonitor = monitor
        monitor.pathUpdateHandler = { [weak self] path in
            Task { @MainActor in self?.handleNetworkAvailability(path.status == .satisfied ? .available : .unavailable) }
        }
        monitor.start(queue: .global(qos: .utility))
    }

    private func clearPendingPairing() {
        confirmationPollingTask?.cancel()
        confirmationPollingTask = nil
        provisionalSession = nil
        shortAuthenticationString = nil
        isWaitingForMacConfirmation = false
    }

    private func clearPendingPairingSelection() {
        clearPendingPairing()
        connectedServerBaseURL = nil
        connectedServer = nil
    }

    private func stopBrowser() {
        browsingTask?.cancel(); browsingTask = nil
        browser?.stopBrowsing(); browser = nil
        discoveredServers = []
    }

    private func cleanUp() {
        reconnectTask?.cancel(); reconnectTask = nil
        pathMonitor?.cancel(); pathMonitor = nil
        clearPendingPairing()
        stopBrowser()
        session = nil
        connectedServerBaseURL = nil
        connectedServer = nil
    }

    deinit {
        browsingTask?.cancel()
        reconnectTask?.cancel()
        confirmationPollingTask?.cancel()
        pathMonitor?.cancel()
    }
}

private extension URLRequest {
    init(relayURL baseURL: URL, path: String) {
        self.init(url: baseURL.appendingPathComponent(path.dropFirst().description))
    }
}

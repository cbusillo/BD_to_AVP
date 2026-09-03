import Combine
import Foundation
import Network

enum RelayCoordinatorState: Equatable {
    case idle
    case discovery
    case pairing(serverID: String, expiresAt: Date)
    case connected(sessionID: String, expiresAt: Date)
    case reconnecting(attempt: Int)
    case networkUnavailable
    case sessionExpired
    case failed(String)
}

enum RelayNetworkAvailability: Sendable {
    case available
    case unavailable
}

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
    @Published private(set) var pairingErrorMessage: String?
    @Published private(set) var connectedServer: RelayDiscoveredEndpoint?

    private(set) var session: RelayEstablishedSession?
    private(set) var connectedServerBaseURL: URL?

    private let transport: any RelayTransport
    private let browserFactory: @Sendable () -> any RelayEndpointBrowsing
    private let clock: @Sendable () -> Date
    private let nonce: @Sendable () -> String

    private var browser: (any RelayEndpointBrowsing)?
    private var browsingTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?
    private var pathMonitor: NWPathMonitor?
    private var pendingChallenge: RelaySessionChallenge?

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
            for await endpoints in newBrowser.discoveryStream {
                guard !Task.isCancelled else { return }
                self?.receiveDiscoveredEndpoints(endpoints)
            }
        }
        newBrowser.startBrowsing()
    }

    func connect(to endpoint: RelayDiscoveredEndpoint) async {
        guard state == .discovery else { return }
        pairingErrorMessage = nil
        do {
            let request = URLRequest(relayURL: endpoint.baseURL, path: RelayWireContract.challengePath)
            let (data, response) = try await transport.data(for: request)
            guard response.statusCode == 200 else {
                try handleSessionStatus(response.statusCode)
                throw RelayTransportError.unexpectedStatusCode(response.statusCode)
            }
            let envelope = try JSONDecoder().decode(RelayChallengeEnvelope.self, from: data)
            guard envelope.challenge.expirationDate > clock() else {
                state = .sessionExpired
                return
            }
            pendingChallenge = envelope.challenge
            connectedServerBaseURL = endpoint.baseURL
            connectedServer = endpoint
            state = .pairing(serverID: endpoint.id, expiresAt: envelope.challenge.expirationDate)
        } catch RelayTransportError.sessionExpired {
            state = .sessionExpired
        } catch {
            state = .failed("Unable to fetch pairing challenge: \(error.localizedDescription)")
        }
    }

    func submitPairingCode(_ rawValue: String) async {
        guard case .pairing = state,
              let challenge = pendingChallenge,
              let baseURL = connectedServerBaseURL
        else { return }
        pairingErrorMessage = nil
        guard challenge.expirationDate > clock() else {
            clearPendingPairing()
            state = .sessionExpired
            return
        }

        do {
            let pairingCode = try RelayPairingCode(rawValue)
            let attempt = try RelayClientPairingAttempt(
                challenge: challenge,
                pairingCode: pairingCode,
                now: clock()
            )
            var request = URLRequest(relayURL: baseURL, path: RelayWireContract.pairingPath)
            request.httpMethod = "POST"
            request.httpBody = try JSONEncoder().encode(attempt.request)
            request.setValue(RelayWireContract.jsonContentType, forHTTPHeaderField: "content-type")

            let (data, response) = try await transport.data(for: request)
            switch response.statusCode {
            case 201:
                break
            case 401:
                pairingErrorMessage = "That pairing code did not match. Try again."
                return
            case 409:
                clearPendingPairing()
                state = .sessionExpired
                return
            default:
                try handleSessionStatus(response.statusCode)
                throw RelayTransportError.unexpectedStatusCode(response.statusCode)
            }
            let envelope = try JSONDecoder().decode(RelayPairingEnvelope.self, from: data)
            let established = try attempt.complete(with: envelope.acceptance, now: clock())
            session = established
            pendingChallenge = nil
            pairingErrorMessage = nil
            state = .connected(sessionID: established.sessionID.rawValue, expiresAt: established.expirationDate)
            startPathMonitor()
        } catch RelaySessionError.invalidPairingCode {
            pairingErrorMessage = "Enter the 16-character code shown on your Mac."
        } catch RelayTransportError.sessionExpired {
            clearPendingPairing()
            state = .sessionExpired
        } catch {
            state = .failed("Pairing failed: \(error.localizedDescription)")
        }
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

    func disconnect() {
        cleanUp()
        state = .idle
    }

    func remotePlaybackConfiguration() -> RelayRemotePlaybackConfiguration? {
        guard hasLiveSession,
              let session,
              let connectedServerBaseURL,
              let connectedServer
        else {
            if self.session != nil {
                self.session = nil
                state = .sessionExpired
            }
            return nil
        }
        return RelayRemotePlaybackConfiguration(
            session: session,
            serverBaseURL: connectedServerBaseURL,
            serverName: connectedServer.displayName,
            transport: transport
        )
    }

    private var isFailed: Bool {
        if case .failed = state { return true }
        return false
    }

    private var isReconnecting: Bool {
        if case .reconnecting = state { return true }
        return false
    }

    private var hasLiveSession: Bool {
        guard let session else { return false }
        return session.expirationDate > clock()
    }

    private func receiveDiscoveredEndpoints(_ endpoints: [RelayDiscoveredEndpoint]) {
        discoveredServers = endpoints
    }

    private func startReconnect(attempt: Int) {
        guard hasLiveSession else {
            state = .sessionExpired
            return
        }
        guard attempt <= RelayBackoff.maximumAttempts else {
            state = .failed("Relay did not reconnect after \(RelayBackoff.maximumAttempts) attempts.")
            return
        }
        reconnectTask?.cancel()
        state = .reconnecting(attempt: attempt)
        reconnectTask = Task { @MainActor [weak self] in
            guard let self else { return }
            let delay = RelayBackoff.delay(for: attempt)
            if delay > 0 {
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            }
            guard !Task.isCancelled, self.isReconnecting, self.hasLiveSession else {
                return
            }
            do {
                try await self.probeExistingSession()
                guard let session = self.session else { return }
                self.state = .connected(sessionID: session.sessionID.rawValue, expiresAt: session.expirationDate)
            } catch RelayTransportError.sessionExpired, RelayTransportError.unpaired {
                self.clearPendingPairing()
                self.session = nil
                self.state = .sessionExpired
            } catch {
                self.startReconnect(attempt: attempt + 1)
            }
        }
    }

    private func probeExistingSession() async throws {
        guard let session, let baseURL = connectedServerBaseURL else {
            throw RelayTransportError.unpaired
        }
        var request = try RelayAuthenticatedRequestFactory.makeRequest(
            baseURL: baseURL,
            path: RelayWireContract.playlistSnapshotPath,
            signer: session,
            clock: clock,
            nonce: nonce
        )
        request.setValue(session.mediaCapability.value, forHTTPHeaderField: RelayWireContract.mediaCapabilityHeader)
        let (_, response) = try await transport.data(for: request)
        guard response.statusCode == 200 else {
            try handleSessionStatus(response.statusCode)
            throw RelayTransportError.unexpectedStatusCode(response.statusCode)
        }
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
            Task { @MainActor [weak self] in
                self?.handleNetworkAvailability(path.status == .satisfied ? .available : .unavailable)
            }
        }
        monitor.start(queue: .global(qos: .utility))
    }

    private func clearPendingPairing() {
        pendingChallenge = nil
        pairingErrorMessage = nil
    }

    private func stopBrowser() {
        browsingTask?.cancel()
        browsingTask = nil
        browser?.stopBrowsing()
        browser = nil
        discoveredServers = []
    }

    private func cleanUp() {
        reconnectTask?.cancel()
        reconnectTask = nil
        pathMonitor?.cancel()
        pathMonitor = nil
        clearPendingPairing()
        stopBrowser()
        session = nil
        connectedServerBaseURL = nil
        connectedServer = nil
    }

    deinit {
        browsingTask?.cancel()
        reconnectTask?.cancel()
        pathMonitor?.cancel()
    }
}

private extension URLRequest {
    init(relayURL baseURL: URL, path: String) {
        self.init(url: baseURL.appendingPathComponent(path.dropFirst().description))
    }
}

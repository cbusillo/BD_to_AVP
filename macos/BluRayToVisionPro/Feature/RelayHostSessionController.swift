import Foundation

@MainActor
protocol RelayHostSessionControlling: AnyObject {
    func cancel() async
    func stop() async
    func stopForAppQuit() async
    func currentLifecycle() async -> RelayHostLifecycle
}

@MainActor
final class RelayHostSessionController: ObservableObject {
    enum Lifecycle: Equatable {
        case idle
        case starting
        case advertising
        case paired
        case finished
        case cancelled
        case stopped
        case failed(String)
    }

    typealias FixtureLoader = @MainActor (URL) throws -> RelayEventHLSFixture
    typealias SessionStarter = @MainActor (URL, RelayEventHLSFixture, RelayPairingCode) async throws -> any RelayHostSessionControlling

    @Published private(set) var lifecycle: Lifecycle = .idle
    @Published private(set) var fixtureDirectory: URL?
    @Published private(set) var formattedPairingCode: String?
    @Published private(set) var segmentCount = 0

    private let loadFixture: FixtureLoader
    private let startSession: SessionStarter
    private var session: (any RelayHostSessionControlling)?
    private var lifecycleMonitoringTask: Task<Void, Never>?

    init(
        loadFixture: @escaping FixtureLoader = { try RelayEventHLSFixture.load(directory: $0) },
        startSession: @escaping SessionStarter = RelayHostSession.start
    ) {
        self.loadFixture = loadFixture
        self.startSession = startSession
    }

    var isSessionActive: Bool {
        session != nil
    }

    var statusText: String {
        switch lifecycle {
        case .idle:
            "Choose an EVENT-HLS fixture to start a private relay session."
        case .starting:
            "Checking the fixture and starting the local relay…"
        case .advertising:
            "Advertising on your local network. Enter this code once on Vision Pro."
        case .paired:
            "Vision Pro is paired. The relay remains available for this session."
        case .finished:
            "The relay playlist is finished."
        case .cancelled:
            "Relay cancelled. The pairing code is no longer valid."
        case .stopped:
            "Relay stopped."
        case let .failed(message):
            message
        }
    }

    var lifecycleText: String {
        switch lifecycle {
        case .idle: "Not started"
        case .starting: "Starting"
        case .advertising: "Pairing available"
        case .paired: "Paired"
        case .finished: "Finished"
        case .cancelled: "Cancelled"
        case .stopped: "Stopped"
        case .failed: "Needs attention"
        }
    }

    func start(directory: URL) async {
        guard session == nil else { return }
        lifecycle = .starting
        let pairingCode = RelayPairingCode.random()
        do {
            let fixture = try loadFixture(directory)
            let session = try await startSession(directory, fixture, pairingCode)
            self.session = session
            fixtureDirectory = directory
            formattedPairingCode = pairingCode.formattedValue
            segmentCount = fixture.segments.count
            lifecycle = .advertising
            monitorLifecycle()
        } catch {
            lifecycle = .failed(Self.message(for: error))
        }
    }

    func cancel() async {
        guard let session else { return }
        await session.cancel()
        lifecycleMonitoringTask?.cancel()
        lifecycleMonitoringTask = nil
        self.session = nil
        formattedPairingCode = nil
        lifecycle = .cancelled
    }

    func stop() async {
        guard let session else { return }
        await session.stop()
        lifecycleMonitoringTask?.cancel()
        lifecycleMonitoringTask = nil
        self.session = nil
        formattedPairingCode = nil
        lifecycle = .stopped
    }

    func stopForAppQuit() async {
        guard let session else { return }
        await session.stopForAppQuit()
        lifecycleMonitoringTask?.cancel()
        lifecycleMonitoringTask = nil
        self.session = nil
        formattedPairingCode = nil
        lifecycle = .stopped
    }

    private static func message(for error: Error) -> String {
        switch error {
        case RelayEventHLSFixtureError.missingInitializationSegment:
            "The selected folder needs an init.mp4 initialization segment."
        case RelayEventHLSFixtureError.missingPlaylist:
            "The selected folder needs a media.m3u8 EVENT-HLS playlist."
        case RelayEventHLSFixtureError.invalidPlaylist:
            "The selected media.m3u8 playlist is not a supported EVENT-HLS fixture."
        case let RelayEventHLSFixtureError.missingSegment(resourceIdentifier):
            "The playlist references a missing segment: \(resourceIdentifier)."
        default:
            "The relay could not start. Choose a complete EVENT-HLS fixture and try again."
        }
    }

    private func monitorLifecycle() {
        lifecycleMonitoringTask?.cancel()
        lifecycleMonitoringTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshLifecycle()
                try? await Task.sleep(for: .seconds(1))
            }
        }
    }

    private func refreshLifecycle() async {
        guard let session else { return }
        switch await session.currentLifecycle() {
        case .pairing:
            lifecycle = .advertising
        case .paired:
            formattedPairingCode = nil
            lifecycle = .paired
        case .finished:
            formattedPairingCode = nil
            lifecycle = .finished
        case .cancelled:
            formattedPairingCode = nil
            lifecycle = .cancelled
            clearTerminalSession()
        case .expired:
            formattedPairingCode = nil
            lifecycle = .failed("The pairing session expired. Start a new relay to continue.")
            clearTerminalSession()
        case .stopped:
            formattedPairingCode = nil
            lifecycle = .stopped
            clearTerminalSession()
        }
    }

    private func clearTerminalSession() {
        lifecycleMonitoringTask?.cancel()
        lifecycleMonitoringTask = nil
        session = nil
    }
}

@MainActor
private final class RelayHostSession: RelayHostSessionControlling {
    private let host: RelayHost
    private let server: RelayNetworkServer

    private init(host: RelayHost, server: RelayNetworkServer) {
        self.host = host
        self.server = server
    }

    static func start(
        directory: URL,
        fixture: RelayEventHLSFixture,
        pairingCode: RelayPairingCode
    ) async throws -> any RelayHostSessionControlling {
        let host = try RelayHost.start(
            configuration: try RelayHostConfiguration(fixtureDirectory: directory),
            fixture: fixture,
            pairingCode: pairingCode
        )
        let server = try await RelayNetworkServer.start(host: host)
        return RelayHostSession(host: host, server: server)
    }

    func cancel() async {
        await server.cancel()
    }

    func stop() async {
        await server.stop()
    }

    func stopForAppQuit() async {
        await server.stopForAppQuit()
    }

    func currentLifecycle() async -> RelayHostLifecycle {
        await host.currentLifecycle()
    }
}

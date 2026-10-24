import Foundation

struct SharedMoviePlayback: Sendable {
    let movie: SharedMovie
    let client: MovieLibraryClient
    var mediaItem: MediaItem {
        MediaItem(id: movie.resumeID, title: movie.title, fileName: movie.fileName, format: .mvHEVC)
    }
}

@MainActor
final class MovieLibraryModel: ObservableObject {
    @Published private(set) var endpoints: [RelayDiscoveredEndpoint] = []
    @Published private(set) var catalog: SharedMovieCatalog?
    @Published private(set) var prompt: MoviePairingPrompt?
    @Published private(set) var savedMacs: [MovieTrustedPeer] = []
    @Published private(set) var selectedName = ""
    @Published private(set) var isBusy = false
    @Published private(set) var isSearching = false
    @Published private(set) var status = "Find a Mac sharing completed movies."
    @Published private(set) var errorMessage: String?
    /// The live title being started, until it plays or fails.
    @Published private(set) var startingLiveTitle: SharedLiveTitle?
    /// "The Mac is busy converting; playback may pause." while the Mac converts.
    @Published private(set) var liveNotice: String?

    private let trust = MovieLibraryTrustStore()
    private let relayTransport: any RelayTransport
    private let clock: any LiveClock
    private var browser: RelayBonjourBrowser?
    private var discoveryTask: Task<Void, Never>?
    private var connectionTask: Task<Void, Never>?
    private var client: MovieLibraryClient?
    private var connection: (any MovieLibraryConnection)?
    private var checkingRefreshTask: Task<Void, Never>?
    private var liveStartTask: Task<Void, Never>?
    private(set) var liveOperation: LiveStartOperation?
    private(set) var livePlayback: LivePlaybackControl?

    static let checkingRefreshInterval: TimeInterval = 3
    static let checkingRefreshLimit: TimeInterval = 120

    private let preferredLanguages: @Sendable () -> [String]

    init(
        relayTransport: any RelayTransport = URLSessionRelayTransport(),
        clock: any LiveClock = SystemLiveClock(),
        preferredLanguages: @escaping @Sendable () -> [String] = { Locale.preferredLanguages }
    ) {
        self.relayTransport = relayTransport
        self.clock = clock
        self.preferredLanguages = preferredLanguages
    }

    /// The audio a live title starts with: the viewer's language when the title has it.
    func startingAudio(for title: SharedLiveTitle) -> SharedLiveAudio? {
        title.startingAudio(preferredLanguages: preferredLanguages())
    }

    var liveTitles: [SharedLiveTitle] { catalog?.liveTitles ?? [] }

    /// For tests and qualification: use an already paired connection.
    func attach(_ connection: any MovieLibraryConnection, name: String, catalog: SharedMovieCatalog) {
        endLive()
        self.connection = connection
        client = connection as? MovieLibraryClient
        selectedName = name
        show(catalog)
    }

    func discover() {
        connectionTask?.cancel()
        endLive()
        let old = client
        client = nil
        connection = nil
        Task { await old?.disconnect() }
        stopDiscovery()
        catalog = nil
        prompt = nil
        isBusy = false
        errorMessage = nil
        refreshSavedMacs()
        endpoints = []
        isSearching = true
        status = "Looking for Macs on your local network…"
        let browser = RelayBonjourBrowser(serviceType: MovieLibraryContract.serviceType)
        self.browser = browser
        discoveryTask = Task { [weak self] in
            for await endpoints in browser.discoveryStream {
                guard let self, !Task.isCancelled else { return }
                self.endpoints = endpoints
                self.status = endpoints.isEmpty ? "Turn on Movie Sharing in the Mac app, then keep both devices on the same network." : "Choose a Mac to browse its movies."
            }
        }
        browser.startBrowsing()
    }

    func stopDiscovery() {
        stopCheckingRefresh()
        browser?.stopBrowsing()
        browser = nil
        discoveryTask?.cancel()
        discoveryTask = nil
        isSearching = false
    }

    func connect(_ endpoint: RelayDiscoveredEndpoint) {
        connectionTask?.cancel()
        endLive()
        stopDiscovery()
        selectedName = endpoint.displayName
        let client = MovieLibraryClient(baseURL: endpoint.baseURL, name: endpoint.displayName, trust: trust)
        self.client = client
        connection = client
        catalog = nil
        prompt = nil
        errorMessage = nil
        isBusy = true
        status = "Connecting to \(endpoint.displayName)…"
        connectionTask = Task {
            do {
                let prompt = try await client.beginPairing()
                guard self.client === client, !Task.isCancelled else { return }
                self.prompt = prompt
                self.isBusy = false
                // The wearer compares the codes and clicks Allow on the Mac; the Mac's
                // approval is the human check, so this side confirms without a button.
                try await completePairing(client)
            } catch { show(error, for: client) }
        }
    }

    func cancelPairing() {
        guard let client else { discover(); return }
        connectionTask?.cancel()
        connectionTask = Task {
            _ = try? await client.confirm(matches: false)
            guard self.client === client, !Task.isCancelled else { return }
            discover()
        }
    }

    func refresh() {
        guard let connection, !isBusy else { return }
        connectionTask?.cancel()
        isBusy = true
        errorMessage = nil
        connectionTask = Task {
            do {
                let catalog = try await connection.catalog()
                guard self.connection === connection, !Task.isCancelled else { return }
                isBusy = false
                show(catalog)
            } catch { show(error, for: connection) }
        }
    }

    private func show(_ catalog: SharedMovieCatalog) {
        self.catalog = catalog
        let live = catalog.liveTitles.count
        status = live == 0
            ? "\(catalog.movies.count) movies from \(selectedName)"
            : "\(catalog.movies.count) movies and \(live) live titles from \(selectedName)"
        scheduleCheckingRefresh()
    }

    /// While any live title is still being checked on the Mac, refreshes the catalog every 3 s
    /// for up to 2 minutes. Disconnecting, rediscovering, or leaving the view stops it.
    private func scheduleCheckingRefresh() {
        guard checkingRefreshTask == nil, liveTitles.contains(where: { $0.state == .checking }),
              let connection else { return }
        let clock = clock
        let deadline = clock.now().addingTimeInterval(Self.checkingRefreshLimit)
        checkingRefreshTask = Task { [weak self] in
            defer { if let self, self.connection === connection { self.checkingRefreshTask = nil } }
            while clock.now() < deadline {
                try? await clock.sleep(seconds: Self.checkingRefreshInterval)
                guard let self, !Task.isCancelled, self.connection === connection else { return }
                guard let catalog = try? await connection.catalog() else { continue }
                guard !Task.isCancelled, self.connection === connection else { return }
                self.catalog = catalog
                if !catalog.liveTitles.contains(where: { $0.state == .checking }) { return }
            }
        }
    }

    func stopCheckingRefresh() {
        checkingRefreshTask?.cancel()
        checkingRefreshTask = nil
    }

    var isCheckingRefreshActive: Bool { checkingRefreshTask != nil }

    // MARK: - Live playback

    /// Starts a live title with its starting audio. `prepare` installs the stream in the player
    /// and reports whether it did; only an installed stream hands cleanup to the playback.
    func startLive(
        _ title: SharedLiveTitle,
        prepare: @escaping @MainActor (RelayRemotePlaybackConfiguration, RelayLivePlaybackContext) async
            -> RelayPlaybackPreparationOutcome
    ) {
        guard let connection, title.state == .eligible, let audio = startingAudio(for: title),
              catalog?.liveTitles.contains(where: { $0.id == title.id }) == true else { return }
        endLive()
        stopCheckingRefresh()
        errorMessage = nil
        liveNotice = nil
        startingLiveTitle = title
        let operation = LiveStartOperation(
            title: title, audioPID: audio.pid, client: connection,
            relayTransport: relayTransport, clock: clock
        ) { [weak self] status in
            self?.liveNotice = status.macBusyConverting ? MVHEVCPlayerSession.liveBusyNotice : nil
        }
        liveOperation = operation
        liveStartTask = Task { [weak self] in
            let result = await operation.run()
            guard let self, self.liveOperation === operation else {
                operation.cleanUp()
                return
            }
            switch result {
            case let .failure(failure):
                // `run` already began cleanup if the start may have been admitted.
                self.startingLiveTitle = nil
                self.liveOperation = nil
                if failure != .cancelled { self.errorMessage = failure.message }
            case let .success(ready):
                let owner = ObjectIdentifier(operation)
                let context = RelayLivePlaybackContext(
                    title: title.title,
                    streamID: ready.streamID,
                    durationSeconds: title.durationSeconds,
                    status: { try? await connection.liveStatus(streamID: ready.streamID) },
                    ended: { [weak self] in self?.installedLivePlaybackEnded(owner) }
                )
                let outcome = await prepare(ready.configuration, context)
                guard self.liveOperation === operation else {
                    operation.cleanUp()
                    return
                }
                self.startingLiveTitle = nil
                self.liveNotice = nil
                if outcome == .installed {
                    self.livePlayback = LivePlaybackControl(operation: operation)
                } else {
                    // Failed or superseded: nothing owns the stream, so stop it now.
                    operation.cleanUp()
                }
            }
        }
    }

    /// Installed live playback went away in the player (a decode failure, an ended stream, a
    /// failed refresh, Done, or replacement): stop its stream now rather than leaving the Mac to
    /// produce until its idle lease expires. The title stays with the caller for Retry.
    private func installedLivePlaybackEnded(_ owner: ObjectIdentifier) {
        guard let livePlayback, ObjectIdentifier(livePlayback.operation) == owner else { return }
        livePlayback.stop()
        self.livePlayback = nil
        liveOperation = nil
    }

    /// Cancels a start in progress. Its cleanup still runs after the start task is cancelled.
    func cancelLiveStart() {
        endLive()
    }

    /// Done or teardown of live playback.
    func finishLivePlayback() {
        endLive()
    }

    private func endLive() {
        liveStartTask?.cancel()
        liveStartTask = nil
        if let livePlayback {
            livePlayback.stop()
        } else {
            liveOperation?.cleanUp()
        }
        livePlayback = nil
        liveOperation = nil
        startingLiveTitle = nil
        liveNotice = nil
    }

    func playback(_ movie: SharedMovie) -> SharedMoviePlayback? {
        guard let client, catalog?.movies.contains(movie) == true else { return nil }
        return SharedMoviePlayback(movie: movie, client: client)
    }

    func forgetConnected() {
        guard let client else { return }
        connectionTask?.cancel()
        endLive()
        connectionTask = Task {
            do {
                try await client.forget()
                guard self.client === client, !Task.isCancelled else { return }
                discover()
            } catch { show(error, for: client) }
        }
    }

    func forgetSaved(_ peer: MovieTrustedPeer) {
        do { try trust.forgetServer(publicKey: peer.publicKey); refreshSavedMacs() }
        catch { errorMessage = error.localizedDescription }
    }

    private func completePairing(_ client: MovieLibraryClient) async throws {
        status = "Click Allow on your Mac."
        let deadline = Date().addingTimeInterval(300)
        while Date() < deadline {
            let complete = try await client.confirm(matches: true)
            guard self.client === client, !Task.isCancelled else { return }
            if complete {
                prompt = nil
                let catalog = try await client.catalog()
                guard self.client === client, !Task.isCancelled else { return }
                isBusy = false
                refreshSavedMacs()
                show(catalog)
                return
            }
            try await Task.sleep(for: .seconds(1))
        }
        throw MovieLibraryError.needsPairing
    }

    func refreshSavedMacs() {
        do { savedMacs = try trust.trustedServers() }
        catch { errorMessage = error.localizedDescription }
    }

    private func show(_ error: Error, for client: any MovieLibraryConnection) {
        guard self.connection === client, !Task.isCancelled else { return }
        errorMessage = error.localizedDescription
        isBusy = false
        prompt = nil
        status = "Reconnect to the Mac to try again."
    }
}

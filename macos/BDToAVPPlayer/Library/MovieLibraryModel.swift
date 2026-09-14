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
    @Published private(set) var isWaitingForMac = false
    @Published private(set) var status = "Find a Mac sharing completed movies."
    @Published private(set) var errorMessage: String?

    private let trust = MovieLibraryTrustStore()
    private var browser: RelayBonjourBrowser?
    private var discoveryTask: Task<Void, Never>?
    private var connectionTask: Task<Void, Never>?
    private var client: MovieLibraryClient?

    func discover() {
        connectionTask?.cancel()
        let old = client
        client = nil
        Task { await old?.disconnect() }
        stopDiscovery()
        catalog = nil
        prompt = nil
        isWaitingForMac = false
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
        browser?.stopBrowsing()
        browser = nil
        discoveryTask?.cancel()
        discoveryTask = nil
        isSearching = false
    }

    func connect(_ endpoint: RelayDiscoveredEndpoint) {
        connectionTask?.cancel()
        stopDiscovery()
        selectedName = endpoint.displayName
        let client = MovieLibraryClient(baseURL: endpoint.baseURL, name: endpoint.displayName, trust: trust)
        self.client = client
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
                self.status = "Compare the code with Movie Sharing on your Mac."
                if prompt.isRemembered { try await completePairing(client) }
            } catch { show(error, for: client) }
        }
    }

    func confirm() {
        guard let client, !isWaitingForMac else { return }
        connectionTask?.cancel()
        connectionTask = Task {
            do { try await completePairing(client) }
            catch { show(error, for: client) }
        }
    }

    func reject() {
        guard let client else { discover(); return }
        connectionTask?.cancel()
        connectionTask = Task {
            _ = try? await client.confirm(matches: false)
            guard self.client === client, !Task.isCancelled else { return }
            discover()
        }
    }

    func refresh() {
        guard let client, !isBusy else { return }
        connectionTask?.cancel()
        isBusy = true
        errorMessage = nil
        connectionTask = Task {
            do {
                let catalog = try await client.catalog()
                guard self.client === client, !Task.isCancelled else { return }
                self.catalog = catalog
                isBusy = false
                status = "\(catalog.movies.count) movies from \(selectedName)"
            } catch { show(error, for: client) }
        }
    }

    func playback(_ movie: SharedMovie) -> SharedMoviePlayback? {
        guard let client, catalog?.movies.contains(movie) == true else { return nil }
        return SharedMoviePlayback(movie: movie, client: client)
    }

    func forgetConnected() {
        guard let client else { return }
        connectionTask?.cancel()
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
        isWaitingForMac = true
        status = "Waiting for confirmation on the Mac…"
        let deadline = Date().addingTimeInterval(300)
        while Date() < deadline {
            let complete = try await client.confirm(matches: true)
            guard self.client === client, !Task.isCancelled else { return }
            if complete {
                prompt = nil
                isWaitingForMac = false
                let catalog = try await client.catalog()
                guard self.client === client, !Task.isCancelled else { return }
                self.catalog = catalog
                isBusy = false
                refreshSavedMacs()
                status = "\(catalog.movies.count) movies from \(selectedName)"
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

    private func show(_ error: Error, for client: MovieLibraryClient) {
        guard self.client === client, !Task.isCancelled else { return }
        errorMessage = error.localizedDescription
        isBusy = false
        isWaitingForMac = false
        prompt = nil
        status = "Reconnect to the Mac to try again."
    }
}

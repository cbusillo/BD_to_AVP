import AppKit
import Foundation

@MainActor
final class MovieSharingController: ObservableObject {
    @Published private(set) var sources: [MovieSourceBookmark] = []
    @Published private(set) var peers: [MovieTrustedPeer] = []
    @Published private(set) var pairingCandidate: RelayPendingPairingCandidate?
    @Published private(set) var isSharing = false
    @Published private(set) var isBusy = false
    @Published private(set) var status = "Add a folder of completed movies to share with Vision Pro."
    @Published var errorMessage: String?

    private let defaults: UserDefaults
    private let trust: MovieLibraryTrustStore
    private var server: RelayNetworkServer?
    private var host: MovieLibraryHost?
    private var monitor: Task<Void, Never>?
    private var epoch = UUID()
    private var hasRestored = false
    private static let foldersKey = "movieSharing.approvedFolders.v1"
    private static let enabledKey = "movieSharing.enabled.v1"

    init(defaults: UserDefaults = .standard, trust: MovieLibraryTrustStore = .forRunningApp()) {
        self.defaults = defaults
        self.trust = trust
        do {
            if let data = defaults.data(forKey: Self.foldersKey) {
                guard data.count <= 1_048_576 else { throw MovieLibraryError.invalidResponse }
                sources = try JSONDecoder().decode([MovieSourceBookmark].self, from: data)
                guard sources.count <= MovieLibraryContract.maximumRoots else { throw MovieLibraryError.invalidResponse }
            }
        } catch { errorMessage = "Saved sharing folders could not be read. Remove and add the folders again." }
    }

    func restoreIfNeeded() async {
        guard !hasRestored else { return }
        hasRestored = true
        refreshPeers()
        if defaults.bool(forKey: Self.enabledKey), errorMessage == nil { await setSharing(true) }
    }

    func addFolders() async {
        guard !isBusy else { return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = true
        panel.prompt = "Share Folder"
        panel.message = "Share completed MP4, MOV, and M4V movies with your paired Vision Pro."
        guard panel.runModal() == .OK else { return }
        do {
            var updated = sources
            for url in panel.urls {
                guard updated.count < MovieLibraryContract.maximumRoots else { throw MovieLibraryError.unavailable }
                let data = try url.bookmarkData(options: .withSecurityScope, includingResourceValuesForKeys: nil, relativeTo: nil)
                updated.append(MovieSourceBookmark(id: UUID().uuidString, name: url.lastPathComponent, bookmark: data))
            }
            let encoded = try JSONEncoder().encode(updated)
            let wasSharing = isSharing
            let operation = UUID()
            epoch = operation
            isBusy = true
            await endSession()
            guard epoch == operation else { return }
            sources = updated
            defaults.set(encoded, forKey: Self.foldersKey)
            isBusy = false
            if wasSharing { await setSharing(true) }
        } catch { errorMessage = error.localizedDescription }
    }

    func removeFolder(_ id: String) async {
        guard !isBusy else { return }
        do {
            let updated = sources.filter { $0.id != id }
            let data = try JSONEncoder().encode(updated)
            let wasSharing = isSharing
            let operation = UUID()
            epoch = operation
            isBusy = true
            await endSession()
            guard epoch == operation else { return }
            sources = updated
            defaults.set(data, forKey: Self.foldersKey)
            isBusy = false
            if wasSharing, !sources.isEmpty { await setSharing(true) }
        } catch { errorMessage = error.localizedDescription }
    }

    func setSharing(_ enabled: Bool) async {
        guard !isBusy else { return }
        if !enabled {
            defaults.set(false, forKey: Self.enabledKey)
            await stopForAppQuit()
            return
        }
        guard !isSharing, !sources.isEmpty else { return }
        isBusy = true
        errorMessage = nil
        let attempt = epoch
        status = "Starting movie sharing…"
        do {
            let catalog = try MovieCatalog(sources: sources)
            let host = try MovieLibraryHost(catalog: catalog, trust: trust)
            let server = try await RelayNetworkServer.start(host: host)
            guard epoch == attempt else { await server.stop(); return }
            self.host = host
            self.server = server
            isSharing = true
            defaults.set(true, forKey: Self.enabledKey)
            status = "Available on your local network. Open Mac Movies on Vision Pro."
            monitor = Task { [weak self, host] in
                while !Task.isCancelled {
                    let candidate = try? await host.pendingCandidate()
                    let lifecycle = await host.currentLifecycle()
                    guard let self, self.host === host, !Task.isCancelled else { return }
                    if self.pairingCandidate?.candidateID != candidate?.candidateID { self.refreshPeers() }
                    self.pairingCandidate = candidate
                    if lifecycle == .stopped {
                        await self.stopForAppQuit()
                        self.errorMessage = "Movie sharing lost its network connection. Turn sharing on again."
                        return
                    }
                    try? await Task.sleep(for: .seconds(1))
                }
            }
        } catch {
            guard epoch == attempt else { return }
            status = "Movie sharing needs attention."
            errorMessage = error.localizedDescription
        }
        if epoch == attempt { isBusy = false }
    }

    func stopForAppQuit() async {
        epoch = UUID()
        isBusy = false
        await endSession()
    }

    private func endSession() async {
        monitor?.cancel()
        monitor = nil
        let server = self.server
        self.server = nil
        host = nil
        pairingCandidate = nil
        isSharing = false
        status = "Movie sharing is off."
        await server?.stopForAppQuit()
    }

    func approve() async {
        guard let host, let pairingCandidate else { return }
        do { try await host.approve(pairingCandidate.candidateID) }
        catch { errorMessage = error.localizedDescription }
    }

    func reject() async {
        guard let host, let pairingCandidate else { return }
        do { try await host.reject(pairingCandidate.candidateID) }
        catch { errorMessage = error.localizedDescription }
    }

    func forget(_ peer: MovieTrustedPeer) async {
        do {
            if let host { try await host.forget(peer.id) }
            else { try trust.forgetClient(id: peer.id) }
            refreshPeers()
        } catch { errorMessage = error.localizedDescription }
    }

    private func refreshPeers() {
        do { peers = try trust.trustedClients() }
        catch { errorMessage = error.localizedDescription }
    }
}

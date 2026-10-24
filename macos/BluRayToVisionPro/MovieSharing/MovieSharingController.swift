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
    @Published private(set) var liveActivity: MovieLiveActivity?
    /// Shared folders the Mac could not read the last time it looked.
    @Published private(set) var unreadableFolderIDs: Set<String> = []
    @Published var errorMessage: String?
    /// True while the conversion queue has an active worker. Live playback still runs, and the
    /// Vision Pro shows that the Mac is busy converting.
    var isConversionActive: @MainActor () -> Bool = { false }

    typealias LiveServicesFactory = @MainActor (_ isBusyConverting: @escaping @Sendable () async -> Bool) throws -> MovieLibraryLiveServices?
    private let makeLiveServices: LiveServicesFactory

    private let defaults: UserDefaults
    private let trust: MovieLibraryTrustStore
    private var server: RelayNetworkServer?
    private var host: MovieLibraryHost?
    private var monitor: Task<Void, Never>?
    private var preparation: Task<Void, Never>?
    private var epoch = UUID()
    private var peersRefresh = 0
    private var hasRestored = false
    private static let foldersKey = "movieSharing.approvedFolders.v1"
    private static let enabledKey = "movieSharing.enabled.v1"

    init(
        defaults: UserDefaults = AppPreferences.current,
        trust: MovieLibraryTrustStore? = nil,
        makeLiveServices: LiveServicesFactory? = nil
    ) {
        self.defaults = defaults
        self.trust = trust ?? Self.trustStoreForRunningProcess()
        self.makeLiveServices = makeLiveServices ?? { isBusyConverting in
            try MovieLibraryLiveServices.automatic(isBusyConverting: isBusyConverting, policy: RelayRequestValidationPolicy())
        }
        do {
            if let data = defaults.data(forKey: Self.foldersKey) {
                guard data.count <= 1_048_576 else { throw MovieLibraryError.invalidResponse }
                sources = try JSONDecoder().decode([MovieSourceBookmark].self, from: data)
                guard sources.count <= MovieLibraryContract.maximumRoots else { throw MovieLibraryError.invalidResponse }
            }
        } catch { errorMessage = "Saved sharing folders could not be read. Remove and add the folders again." }
    }

    private static func trustStoreForRunningProcess() -> MovieLibraryTrustStore {
        MovieLibraryTrustStore.forRunningApp(isTeamSigned: usesKeychain())
    }

    static func usesKeychain(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        isTeamSigned: () -> Bool = MovieLibraryTrustStore.runningCodeHasTeamIdentifier
    ) -> Bool {
        !AppDelegate.isUnitTestHost(environment: environment) && isTeamSigned()
    }

    func restoreIfNeeded() async {
        guard !hasRestored else { return }
        hasRestored = true
        await refreshPeers()
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
            if wasSharing { await setSharing(true) } else { await checkFolders() }
        } catch { errorMessage = error.localizedDescription }
    }

    /// Reads the folders once while sharing is off, so macOS asks for access now, at the Mac.
    private func checkFolders() async {
        let attempt = epoch
        guard let catalog = try? await MovieCatalog.open(sources: sources) else { return }
        _ = try? await catalog.read()
        let unreadable = await catalog.unreadableRootIDs()
        await catalog.stop()
        guard epoch == attempt, !isSharing else { return }
        unreadableFolderIDs = unreadable
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
            // Resolving and opening the roots may wait on a network share; never on the main actor.
            let catalog = try await MovieCatalog.open(sources: sources)
            guard epoch == attempt else { await catalog.stop(); return }
            let isBusyConverting: @Sendable () async -> Bool = { @MainActor [weak self] in
                self?.isConversionActive() ?? false
            }
            // The launch sweep finishes before any live workspace or handoff socket of this run
            // can exist. Unit-test hosts never sweep the real caches.
            if !AppDelegate.isUnitTestHost(environment: ProcessInfo.processInfo.environment) {
                await LiveRelayStreamStarter.leftoverSweep.value
            }
            guard epoch == attempt else { await catalog.stop(); return }
            // Live ISO playback is optional: without a worker, sharing still serves movies.
            let live = try? makeLiveServices(isBusyConverting)
            let trust = trust
            let host = try await Self.offMainActor { try MovieLibraryHost(catalog: catalog, trust: trust, live: live) }
            let server = try await RelayNetworkServer.start(host: host)
            guard epoch == attempt else { await server.stop(); return }
            self.host = host
            self.server = server
            isSharing = true
            defaults.set(true, forKey: Self.enabledKey)
            status = "Available on your local network. Open Mac Movies on Vision Pro."
            // Read the folders now rather than on the first headset request. A folder that
            // cannot be read yet, for example while macOS waits for consent, is tried again.
            // A share that never answers leaves its file call running, so retries back off.
            preparation = Task { [host] in
                var delay: TimeInterval = 10
                while !Task.isCancelled {
                    let prepared = (try? await host.prepareCatalog()) != nil
                    if prepared, await host.unreadableFolderIDs().isEmpty { return }
                    try? await Task.sleep(for: .seconds(delay))
                    delay = min(delay * 2, 300)
                }
            }
            monitor = Task { [weak self, host] in
                while !Task.isCancelled {
                    let candidate = try? await host.pendingCandidate()
                    let lifecycle = await host.currentLifecycle()
                    let activity = await host.liveActivity()
                    let unreadable = await host.unreadableFolderIDs()
                    guard let self, self.host === host, !Task.isCancelled else { return }
                    if self.pairingCandidate?.candidateID != candidate?.candidateID { await self.refreshPeers() }
                    self.pairingCandidate = candidate
                    if self.liveActivity != activity { self.liveActivity = activity }
                    if self.unreadableFolderIDs != unreadable { self.unreadableFolderIDs = unreadable }
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
        preparation?.cancel()
        preparation = nil
        let server = self.server
        self.server = nil
        host = nil
        pairingCandidate = nil
        liveActivity = nil
        unreadableFolderIDs = []
        isSharing = false
        status = "Movie sharing is off."
        await server?.stopForAppQuit()
    }

    /// Stops live playback and waits for its worker to exit. Sharing stays on.
    func stopLivePlayback() async {
        guard let host else { return }
        await host.stopLivePlayback()
        liveActivity = await host.liveActivity()
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
            else {
                let trust = trust, id = peer.id
                try await Self.offMainActor { try trust.forgetClient(id: id) }
            }
            await refreshPeers()
        } catch { errorMessage = error.localizedDescription }
    }

    private func refreshPeers() async {
        peersRefresh += 1
        let refresh = peersRefresh
        let trust = trust
        do {
            let clients = try await Self.offMainActor { try trust.trustedClients() }
            // A later refresh may have finished first; keep the newest list.
            guard refresh == peersRefresh else { return }
            peers = clients
        } catch { errorMessage = error.localizedDescription }
    }

    /// Runs a trust store call away from the main actor. A Keychain read can wait on a macOS
    /// access prompt, and the window must keep responding while it does.
    private nonisolated static func offMainActor<T: Sendable>(
        _ work: @escaping @Sendable () throws -> T
    ) async throws -> T {
        try await Task.detached(operation: work).value
    }
}

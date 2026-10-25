import SwiftUI
import UniformTypeIdentifiers

struct AppShellView: View {
    @ObservedObject var model: PlayerAppModel
    @ObservedObject var playerSession: MVHEVCPlayerSession
    @ObservedObject var relayCoordinator: RelaySessionCoordinator
    let resumeStore: ResumeStore

    @Environment(\.scenePhase) private var scenePhase
    @State private var preparationTask: Task<Void, Never>?
    @StateObject private var movieLibrary = MovieLibraryModel()
    @State private var selection: LibrarySelection = .onMyVisionPro
    @State private var sharedPlayback: SharedMoviePlayback?
    @State private var liveTitle: SharedLiveTitle?
    @State private var filePickerPurpose: FilePickerPurpose = .addLocation
    @State private var isFilePickerPresented = false
    #if BD_TO_AVP_QUALIFICATION
    @State private var hasStartedRelayQualification = false
    #endif

    var body: some View {
        Group {
            if model.playbackRequest != nil || playerSession.state != .idle {
                PlayerView(
                    session: playerSession,
                    onRetry: retryPlayback,
                    onLocate: locatePlaybackSource,
                    onDone: finishPlayback
                )
            } else {
                NavigationSplitView {
                    List {
                        Section("Library") {
                            Button {
                                selection = .onMyVisionPro
                            } label: {
                                Label("On My Vision Pro", systemImage: "visionpro")
                            }.accessibilityIdentifier("source-on-my-vision-pro")
                            Button {
                                selection = .macMovies
                            } label: {
                                Label("Mac Movies", systemImage: "desktopcomputer")
                            }.accessibilityIdentifier("source-mac-movies")
                        }
                        LocationsSidebarSection(
                            locations: model.locations,
                            select: { selection = .location($0) },
                            add: { presentFilePicker(.addLocation) }
                        )
                        #if BD_TO_AVP_QUALIFICATION
                        Section("Live Relay") {
                            Button {
                                relayCoordinator.startDiscovery()
                            } label: {
                                Label("Connect to Mac", systemImage: "dot.radiowaves.left.and.right")
                            }
                            .disabled(!canStartRelayDiscovery)
                        }
                        #endif
                        Section("About") {
                            if let privacyURL = URL(
                                string:
                                "https://github.com/cbusillo/BD_to_AVP/blob/4acaa39f320107b4a169b4dd4bcd9e00e641eae2/docs/visionos-player-privacy.md"
                            ) {
                                Link(destination: privacyURL) {
                                    Label("Privacy Policy", systemImage: "hand.raised")
                                }
                                .accessibilityIdentifier("privacy-policy")
                            }
                        }
                    }
                            .listStyle(.sidebar)
                            .navigationTitle("Library")
                } detail: {
                    VStack(spacing: 0) {
                        if relayCoordinator.state != .idle {
                            RelayConnectionView(
                                coordinator: relayCoordinator,
                                playRelay: startRelayPlayback
                            )
                            Divider()
                        }
                        switch selection {
                        case .onMyVisionPro:
                            LibraryView(model: model, addLocation: { presentFilePicker(.addLocation) })
                        case .macMovies:
                            MovieLibraryView(model: movieLibrary, play: startSharedPlayback, playLive: startLivePlayback)
                        case let .location(locationID):
                            LocationLibraryView(
                                model: model,
                                locations: model.locations,
                                locationID: locationID,
                                chooseFolderAgain: { presentFilePicker(.reconnectLocation(locationID)) },
                                removed: { selection = .onMyVisionPro }
                            )
                            .id(locationID)
                        }
                    }
                }
            }
        }
                .sheet(isPresented: detailsPresented) {
            if let itemID = model.selectedItemID {
                MediaDetailsView(model: model, itemID: itemID)
            }
        }
                .alert("Something went wrong", isPresented: errorPresented) {
            Button("OK") {
                model.clearError()
            }
        } message: {
            Text(model.errorMessage ?? "Please try again.")
        }
                .alert("Something went wrong", isPresented: locationErrorPresented) {
            Button("OK") {
                model.locations.errorMessage = nil
            }
        } message: {
            Text(model.locations.errorMessage ?? "Please try again.")
        }
                .fileImporter(
            isPresented: $isFilePickerPresented,
            allowedContentTypes: filePickerPurpose.contentTypes,
            allowsMultipleSelection: false
        ) { result in
            guard case let .success(urls) = result, let url = urls.first else {
                return
            }
            handlePickedFile(url, for: filePickerPurpose)
        }
                .onChange(of: model.playbackRequest) { _, request in
            guard let request else {
                return
            }
            prepareForPlayback(request.item)
        }
                .onChange(of: relayCoordinator.state) { _, state in
            playerSession.handleRelayCoordinatorState(state)
        }
                .onChange(of: playerSession.relayTerminationEvent) { _, event in
            if let event {
                relayCoordinator.handlePlaybackTermination(event)
            }
        }
                .onDisappear {
            preparationTask?.cancel()
            endLivePlayback()
        }
                .onChange(of: scenePhase, initial: true) { _, phase in
            if phase == .active {
                Task {
                    await model.refreshSourceStatuses()
                }
            }
            playerSession.handleScenePhase(phase)
        }
                .task {
            await model.bootstrap()
            await model.locations.refreshAll()
            #if BD_TO_AVP_QUALIFICATION
            if !hasStartedRelayQualification {
                hasStartedRelayQualification = true
                await RelayQualificationDriver.runIfRequested(
                    coordinator: relayCoordinator, player: playerSession
                )
                await RelayQualificationDriver.runLiveIfRequested(library: movieLibrary, player: playerSession)
                await SourceReadQualification.runIfRequested(model: model)
                await NativePlaybackQualification.runIfRequested(model: model, player: playerSession)
            }
            #endif
        }
    }

    /// The player's Locate: a local movie is chosen again; a Location's title is reached by
    /// choosing its Location's folder again.
    private func locatePlaybackSource() {
        guard let item = playerSession.mediaItem else { return }
        if let locationID = LocationLibraryModel.locationID(forItemID: item.id) {
            presentFilePicker(.reconnectLocation(locationID))
        } else {
            presentFilePicker(.locatePlaybackSource)
        }
    }

    private func handlePickedFile(_ url: URL, for purpose: FilePickerPurpose) {
        switch purpose {
        case .addLocation:
            Task {
                if let locationID = await model.locations.add(folderURL: url) {
                    selection = .location(locationID)
                }
            }
        case let .reconnectLocation(locationID):
            Task {
                await model.locations.reconnect(locationID, folderURL: url)
                if let item = playerSession.mediaItem, model.isLocationItem(item.id) {
                    retryPlayback()
                }
            }
        case .locatePlaybackSource:
            guard let item = playerSession.mediaItem else { return }
            preparationTask?.cancel()
            preparationTask = Task {
                guard await model.locate(itemID: item.id, at: url, shouldShowDetails: false),
                      !Task.isCancelled else {
                    return
                }
                await playerSession.prepare(
                    mediaItem: model.item(id: item.id) ?? item,
                    bookmarkStore: model.bookmarkStore,
                    resumeStore: resumeStore
                )
            }
        }
    }

    private func prepareForPlayback(_ item: MediaItem) {
        endLivePlayback()
        sharedPlayback = nil
        preparationTask?.cancel()
        preparationTask = Task {
            await playerSession.prepare(
                mediaItem: item,
                bookmarkStore: model.bookmarkStore,
                resumeStore: resumeStore
            )
        }
    }

    private func startSharedPlayback(_ selection: SharedMoviePlayback) {
        endLivePlayback()
        sharedPlayback = selection
        preparationTask?.cancel()
        preparationTask = Task {
            await playerSession.prepare(
                mediaItem: selection.mediaItem, bookmarkStore: model.bookmarkStore, resumeStore: resumeStore,
                sharedMovie: selection)
        }
    }

    /// Live titles start on the Mac first; the model installs the stream in the player once it
    /// is live. Live titles start from the beginning; the player says so when one was watched before.
    private func startLivePlayback(_ title: SharedLiveTitle) {
        preparationTask?.cancel()
        preparationTask = nil
        sharedPlayback = nil
        liveTitle = title
        movieLibrary.startLive(title) { configuration, context in
            await playerSession.prepareRelayPlayback(configuration, live: context, resumeStore: resumeStore)
        }
    }

    private func endLivePlayback() {
        guard liveTitle != nil else { return }
        liveTitle = nil
        movieLibrary.finishLivePlayback()
    }

    private func retryPlayback() {
        if let liveTitle {
            startLivePlayback(liveTitle)
            return
        }
        if let sharedPlayback {
            startSharedPlayback(sharedPlayback)
            return
        }
        if playerSession.isRelayPlayback {
            startRelayPlayback()
            return
        }
        guard let item = playerSession.mediaItem else {
            return
        }
        prepareForPlayback(item)
    }

    private func finishPlayback() {
        preparationTask?.cancel()
        preparationTask = nil
        model.clearPlaybackRequest()
        sharedPlayback = nil
        endLivePlayback()
    }

    private func startRelayPlayback() {
        guard let configuration = relayCoordinator.remotePlaybackConfiguration() else {
            playerSession.finish()
            finishPlayback()
            return
        }
        preparationTask?.cancel()
        preparationTask = Task {
            await playerSession.prepareRelayPlayback(configuration)
        }
    }

    private var canStartRelayDiscovery: Bool {
        switch relayCoordinator.state {
        case .idle, .sessionExpired, .failed:
            true
        case .discovery, .confirming, .connected, .reconnecting, .networkUnavailable:
            false
        }
    }

    /// One file picker serves adding a Location, choosing its folder again, and the player's Locate.
    private func presentFilePicker(_ purpose: FilePickerPurpose) {
        filePickerPurpose = purpose
        isFilePickerPresented = true
    }

    private var locationErrorPresented: Binding<Bool> {
        Binding(
            get: { model.locations.errorMessage != nil },
            set: { if !$0 { model.locations.errorMessage = nil } }
        )
    }

    private var errorPresented: Binding<Bool> {
        Binding(
            get: { model.errorMessage != nil },
            set: {
                if !$0 {
                    model.clearError()
                }
            }
        )
    }

    private var detailsPresented: Binding<Bool> {
        Binding(
            get: {
                model.isShowingDetails
                        && model.selectedItemID != nil
                        && model.playbackRequest == nil
            },
            set: { isPresented in
                if !isPresented && model.playbackRequest == nil {
                    model.closeDetails()
                }
            }
        )
    }
}

private enum LibrarySelection: Hashable {
    case onMyVisionPro
    case macMovies
    case location(UUID)
}

private enum FilePickerPurpose: Equatable {
    case addLocation
    case reconnectLocation(UUID)
    case locatePlaybackSource

    var contentTypes: [UTType] {
        switch self {
        case .addLocation, .reconnectLocation:
            return [.folder]
        case .locatePlaybackSource:
            return PlayerAppModel.importableContentTypes
        }
    }
}

/// The sidebar's Locations: each added share, whether it can be reached, and Add Location.
private struct LocationsSidebarSection: View {
    @ObservedObject var locations: LocationLibraryModel
    let select: (UUID) -> Void
    let add: () -> Void

    var body: some View {
        Section("Locations") {
            ForEach(locations.locations) { location in
                Button {
                    select(location.id)
                } label: {
                    Label {
                        Text(location.name)
                    } icon: {
                        Image(systemName: locations.statuses[location.id] == .unreachable
                            ? "externaldrive.badge.exclamationmark" : "server.rack")
                    }
                }
                .accessibilityIdentifier("location-\(location.id.uuidString)")
                .accessibilityValue(locations.statuses[location.id] == .unreachable ? "Can't reach" : "")
            }
            Button(action: add) {
                Label("Add Location", systemImage: "plus")
            }
            .accessibilityIdentifier("add-location")
            .accessibilityHint("Choose a network share or folder in Files.")
        }
    }
}

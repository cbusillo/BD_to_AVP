import SwiftUI
import UniformTypeIdentifiers

struct AppShellView: View {
    @ObservedObject var model: PlayerAppModel
    @ObservedObject var playerSession: MVHEVCPlayerSession
    let resumeStore: ResumeStore

    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.dismissImmersiveSpace) private var dismissImmersiveSpace
    @Environment(\.dismissWindow) private var dismissWindow
    @EnvironmentObject private var cinema: CinemaState
    @State private var selection: LibrarySelection = .onMyVisionPro
    @State private var filePickerPurpose: FilePickerPurpose = .addLocation
    @State private var isFilePickerPresented = false
    #if BD_TO_AVP_QUALIFICATION
    @State private var hasStartedQualification = false
    #endif

    var body: some View {
        ZStack {
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
                        }
                        LocationsSidebarSection(
                            locations: model.locations,
                            select: { selection = .location($0) },
                            add: { presentFilePicker(.addLocation) }
                        )
                        Section("About") {
                            if let privacyURL = URL(
                                string:
                                "https://github.com/cbusillo/BD_to_AVP/blob/3186802cd9d6b86bf6adc5a99037bb8abd2961ac/docs/visionos-player-privacy.md"
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
                    switch selection {
                    case .onMyVisionPro:
                        LibraryView(model: model, addLocation: { presentFilePicker(.addLocation) })
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
                .onDisappear {
            finishPlayback()
        }
                .onChange(of: scenePhase, initial: true) { _, phase in
            if phase == .active {
                Task {
                    await model.refreshSourceStatuses()
                }
            }
            model.handlePlaybackWindowPhase(phase, session: playerSession)
            if phase == .background {
                dismissPlaybackScenes()
            }
        }
                .task {
            await model.bootstrap()
            await model.locations.refreshAll()
            #if BD_TO_AVP_QUALIFICATION
            if !hasStartedQualification {
                hasStartedQualification = true
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
            model.locatePlaybackSource(at: url, session: playerSession, resumeStore: resumeStore)
        }
    }

    private func prepareForPlayback(_ item: MediaItem) {
        model.preparePlayback(item, session: playerSession, resumeStore: resumeStore)
    }

    private func retryPlayback() {
        guard let item = playerSession.mediaItem else {
            return
        }
        prepareForPlayback(item)
    }

    private func finishPlayback() {
        model.finishPlayback(session: playerSession)
        dismissPlaybackScenes()
    }

    private func dismissPlaybackScenes() {
        if cinema.isActive {
            Task { await dismissImmersiveSpace() }
        }
        dismissWindow(id: PlaybackStatsView.windowID, value: PlaybackStatsView.windowValue)
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

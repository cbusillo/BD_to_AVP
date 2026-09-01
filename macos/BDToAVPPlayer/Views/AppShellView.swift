import SwiftUI
import UniformTypeIdentifiers

struct AppShellView: View {
    @ObservedObject var model: PlayerAppModel
    @ObservedObject var playerSession: MVHEVCPlayerSession
    let resumeStore: ResumeStore

    @Environment(\.scenePhase) private var scenePhase
    @State private var preparationTask: Task<Void, Never>?
    @State private var isPlayerLocatorPresented = false

    var body: some View {
        Group {
            if model.playbackRequest != nil || playerSession.state != .idle {
                PlayerView(
                    session: playerSession,
                    onRetry: retryPlayback,
                    onLocate: { isPlayerLocatorPresented = true },
                    onDone: finishPlayback
                )
            } else {
                NavigationSplitView {
                    List {
                        Section("Sources") {
                            Label("On My Vision Pro", systemImage: "visionpro")
                                .font(.headline)
                        }
                    }
                    .listStyle(.sidebar)
                    .navigationTitle("Library")
                } detail: {
                    LibraryView(model: model)
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
        .fileImporter(
            isPresented: $isPlayerLocatorPresented,
            allowedContentTypes: [.movie],
            allowsMultipleSelection: false
        ) { result in
            guard case let .success(urls) = result,
                  let url = urls.first,
                  let item = playerSession.mediaItem
            else {
                return
            }
            preparationTask?.cancel()
            preparationTask = Task {
                guard await model.locate(
                    itemID: item.id,
                    at: url,
                    shouldShowDetails: false
                ), !Task.isCancelled else {
                    return
                }
                await playerSession.prepare(
                    mediaItem: model.item(id: item.id) ?? item,
                    bookmarkStore: model.bookmarkStore,
                    resumeStore: resumeStore
                )
            }
        }
        .onChange(of: model.playbackRequest) { _, request in
            guard let request else { return }
            prepareForPlayback(request.item)
        }
        .onDisappear {
            preparationTask?.cancel()
        }
        .onChange(of: scenePhase, initial: true) { _, phase in
            if phase == .active {
                Task {
                    await model.refreshSourceStatuses()
                }
                playerSession.applicationBecameActive()
            } else {
                playerSession.applicationBecameInactive()
            }
        }
        .task {
            await model.bootstrap()
        }
    }

    private func prepareForPlayback(_ item: MediaItem) {
        preparationTask?.cancel()
        preparationTask = Task {
            await playerSession.prepare(
                mediaItem: item,
                bookmarkStore: model.bookmarkStore,
                resumeStore: resumeStore
            )
        }
    }

    private func retryPlayback() {
        guard let item = playerSession.mediaItem else {
            return
        }
        prepareForPlayback(item)
    }

    private func finishPlayback() {
        preparationTask?.cancel()
        preparationTask = nil
        model.clearPlaybackRequest()
    }

    private var errorPresented: Binding<Bool> {
        Binding(
            get: { model.errorMessage != nil },
            set: { if !$0 { model.clearError() } }
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

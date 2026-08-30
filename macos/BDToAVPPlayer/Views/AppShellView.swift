import SwiftUI

struct AppShellView: View {
    @ObservedObject var model: PlayerAppModel
    @ObservedObject var playerSession: MVHEVCPlayerSession
    let resumeStore: ResumeStore

    @Environment(\.scenePhase) private var scenePhase
    @State private var preparationTask: Task<Void, Never>?

    var body: some View {
        Group {
            if model.playbackRequest != nil || playerSession.state != .idle {
                PlayerView(session: playerSession) {
                    preparationTask?.cancel()
                    preparationTask = nil
                    model.clearPlaybackRequest()
                }
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
        .onChange(of: model.playbackRequest) { _, request in
            guard let request else { return }
            preparationTask?.cancel()
            preparationTask = Task {
                await playerSession.prepare(
                    mediaItem: request.item,
                    bookmarkStore: model.bookmarkStore,
                    resumeStore: resumeStore
                )
            }
        }
        .onDisappear {
            preparationTask?.cancel()
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                model.refreshSourceStatuses()
            }
        }
        .task {
            await model.bootstrap()
        }
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

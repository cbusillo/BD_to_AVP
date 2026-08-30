import SwiftUI

struct AppShellView: View {
    @ObservedObject var model: PlayerAppModel
    @ObservedObject var playerSession: MVHEVCPlayerSession
    let resumeStore: ResumeStore

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
        .sheet(isPresented: $model.isShowingDetails) {
            if let selectedItemID = model.selectedItemID {
                MediaDetailsView(model: model, itemID: selectedItemID)
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
}

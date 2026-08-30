import SwiftUI

struct AppShellView: View {
    @ObservedObject var model: PlayerAppModel
    @ObservedObject var playerSession: MVHEVCPlayerSession
    let resumeStore: ResumeStore

    @State private var navigationPath: [String] = []
    @State private var preparationTask: Task<Void, Never>?

    var body: some View {
        NavigationStack(path: $navigationPath) {
            Group {
                if model.playbackRequest != nil || playerSession.state != .idle {
                    PlayerView(session: playerSession) {
                        preparationTask?.cancel()
                        preparationTask = nil
                        model.clearPlaybackRequest()
                    }
                } else {
                    LibraryView(model: model)
                }
            }
            .navigationDestination(for: String.self) { itemID in
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
        .onChange(of: model.selectedItemID) { _, itemID in
            guard model.isShowingDetails, let itemID else { return }
            if navigationPath.last != itemID {
                navigationPath.append(itemID)
            }
        }
        .onChange(of: model.isShowingDetails) { _, isShowingDetails in
            if !isShowingDetails {
                navigationPath.removeAll()
            }
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

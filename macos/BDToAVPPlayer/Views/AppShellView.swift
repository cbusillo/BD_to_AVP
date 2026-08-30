import SwiftUI

struct AppShellView: View {
    @ObservedObject var model: PlayerAppModel
    @ObservedObject var playerSession: MVHEVCPlayerSession
    let resumeStore: ResumeStore

    @Environment(\.scenePhase) private var scenePhase
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
            synchronizeNavigationPath(selectedItemID: itemID, isShowingDetails: model.isShowingDetails)
        }
        .onChange(of: model.isShowingDetails) { _, isShowingDetails in
            synchronizeNavigationPath(selectedItemID: model.selectedItemID, isShowingDetails: isShowingDetails)
        }
        .onChange(of: navigationPath) { _, path in
            synchronizeDetailsState(navigationPath: path)
        }
        .onChange(of: model.playbackRequest) { _, request in
            guard let request else {
                synchronizeNavigationPath(
                    selectedItemID: model.selectedItemID,
                    isShowingDetails: model.isShowingDetails,
                    animated: false
                )
                return
            }
            setNavigationPath([], animated: false)
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

    private func synchronizeNavigationPath(
        selectedItemID: String?,
        isShowingDetails: Bool,
        animated: Bool = true
    ) {
        guard model.playbackRequest == nil, isShowingDetails, let selectedItemID else {
            if !navigationPath.isEmpty {
                setNavigationPath([], animated: animated)
            }
            return
        }

        if navigationPath.last != selectedItemID {
            setNavigationPath([selectedItemID], animated: animated)
        }
    }

    private func setNavigationPath(_ path: [String], animated: Bool) {
        guard !animated else {
            navigationPath = path
            return
        }

        var transaction = Transaction()
        transaction.disablesAnimations = true
        withTransaction(transaction) {
            navigationPath = path
        }
    }

    private func synchronizeDetailsState(navigationPath: [String]) {
        guard model.playbackRequest == nil else { return }

        if let itemID = navigationPath.last {
            if model.selectedItemID != itemID || !model.isShowingDetails {
                model.selectedItemID = itemID
                model.isShowingDetails = true
            }
        } else if model.selectedItemID != nil || model.isShowingDetails {
            model.closeDetails()
        }
    }
}

import SwiftUI

struct AppShellView: View {
    @ObservedObject var model: PlayerAppModel

    var body: some View {
        NavigationSplitView {
            List {
                Section("Sources") {
                    Label("All Movies", systemImage: "film.stack")
                        .font(.headline)
                }
            }
            .listStyle(.sidebar)
            .navigationTitle("Library")
        } detail: {
            LibraryView(model: model)
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
    }

    private var errorPresented: Binding<Bool> {
        Binding(
            get: { model.errorMessage != nil },
            set: { if !$0 { model.clearError() } }
        )
    }
}

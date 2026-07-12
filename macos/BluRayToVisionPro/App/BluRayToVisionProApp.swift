import SwiftUI

enum AppWindowID {
    static let settings = "settings"
}

@main
struct BluRayToVisionProApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var viewModel = ConversionViewModel()
    @StateObject private var settings = AppSettings()
    @StateObject private var profileStore = ProfileStore()

    private let capabilities = AppCapabilities.current

    var body: some Scene {
        WindowGroup {
            ContentView(
                viewModel: viewModel,
                settings: settings,
                profileStore: profileStore,
                capabilities: capabilities
            )
                .frame(minWidth: 980, minHeight: 680)
                .background(
                    WindowAccessor { window in
                        appDelegate.attach(window: window, viewModel: viewModel)
                    }
                )
                .onAppear {
                    settings.normalize(for: capabilities)
                    settings.selectedProfileID = profileStore.normalizedProfileID(settings.selectedProfileID)
                    appDelegate.viewModel = viewModel
                }
        }
        .defaultSize(width: 1_120, height: 760)
        .windowResizability(.contentMinSize)
        .windowToolbarStyle(.unified)
        .commands {
            SettingsWindowCommands()

            CommandGroup(replacing: .newItem) {
                Button("Add Source…") {
                    chooseSource()
                }
                .keyboardShortcut("o")
                .disabled(!viewModel.canSelectSource)
            }
        }

        Window("Settings", id: AppWindowID.settings) {
            SettingsView(
                settings: settings,
                profileStore: profileStore,
                capabilities: capabilities
            )
        }
        .defaultSize(width: 900, height: 680)
        .windowResizability(.contentMinSize)
    }

    @MainActor
    private func chooseSource() {
        guard viewModel.canSelectSource else {
            return
        }
        guard let source = SourcePicker.chooseExistingSource() else {
            return
        }
        viewModel.selectSource(source)
    }
}

private struct SettingsWindowCommands: Commands {
    @Environment(\.openWindow) private var openWindow

    var body: some Commands {
        CommandGroup(replacing: .appSettings) {
            Button("Settings…") {
                openWindow(id: AppWindowID.settings)
            }
            .keyboardShortcut(",", modifiers: .command)
        }
    }
}

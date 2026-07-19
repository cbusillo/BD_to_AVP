import SwiftUI

enum AppWindowID {
    static let settings = "settings"
}

@main
struct BluRayToVisionProApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var viewModel: ConversionViewModel
    @StateObject private var previewViewModel: PreviewViewModel
    @StateObject private var diagnosticReportViewModel: DiagnosticReportViewModel
    @StateObject private var updater: UpdateController
    @StateObject private var settings = AppSettings()
    @StateObject private var profileStore = ProfileStore()

    private let capabilities = AppCapabilities.current
    private let workCoordinator: AppWorkCoordinator
    private let observabilityEventStore: any ObservabilityEventPersisting

    init() {
        let observabilityEventStore = ObservabilityEventStore.automatic()
        let viewModel = ConversionViewModel(observabilityEventStore: observabilityEventStore)
        let previewViewModel = PreviewViewModel(observabilityEventStore: observabilityEventStore)
        let diagnosticConfiguration = DiagnosticServiceConfiguration.configured()
        let diagnosticUploader = diagnosticConfiguration.map {
            DiagnosticReportClient(configuration: $0)
        }
        let diagnosticReportViewModel = DiagnosticReportViewModel(
            uploader: diagnosticUploader,
            capture: { outputDirectory in
                try await viewModel.captureDiagnosticBundle(in: outputDirectory)
            }
        )
        let workCoordinator = AppWorkCoordinator(conversion: viewModel, preview: previewViewModel)
        _viewModel = StateObject(wrappedValue: viewModel)
        _previewViewModel = StateObject(wrappedValue: previewViewModel)
        _diagnosticReportViewModel = StateObject(wrappedValue: diagnosticReportViewModel)
        _updater = StateObject(wrappedValue: UpdateController(installPostponer: workCoordinator))
        self.workCoordinator = workCoordinator
        self.observabilityEventStore = observabilityEventStore
        appDelegate.observabilityEventStore = observabilityEventStore
    }

    var body: some Scene {
        WindowGroup {
            ContentView(
                viewModel: viewModel,
                previewViewModel: previewViewModel,
                diagnosticReportViewModel: diagnosticReportViewModel,
                settings: settings,
                profileStore: profileStore,
                capabilities: capabilities
            )
                .frame(minWidth: 1_080, minHeight: 680)
                .background(
                    WindowAccessor { window in
                        appDelegate.attach(window: window, workCoordinator: workCoordinator)
                    }
                )
                .onAppear {
                    updater.startIfNeeded()
                    settings.selectedProfileID = profileStore.normalizedProfileID(settings.selectedProfileID)
                    appDelegate.workCoordinator = workCoordinator
                }
        }
        .defaultSize(width: 1_120, height: 820)
        .windowResizability(.contentMinSize)
        .windowToolbarStyle(.unified)
        .commands {
            SettingsWindowCommands()
            UpdateCommands(updater: updater)

            CommandGroup(replacing: .newItem) {
                Button("Add Source…") {
                    chooseSource()
                }
                .keyboardShortcut("o")
                .disabled(!viewModel.canSelectSource || previewViewModel.hasActiveWorker)
            }
        }

        Window("Settings", id: AppWindowID.settings) {
            SettingsView(
                settings: settings,
                profileStore: profileStore,
                updater: updater
            )
        }
        .defaultSize(width: 900, height: 680)
        .windowResizability(.contentMinSize)
    }

    @MainActor
    private func chooseSource() {
        guard viewModel.canSelectSource, !previewViewModel.hasActiveWorker else {
            return
        }
        guard let sourceURL = SourcePicker.chooseExistingSource() else {
            return
        }
        viewModel.selectSource(sourceURL)
    }
}

private struct UpdateCommands: Commands {
    @ObservedObject var updater: UpdateController

    var body: some Commands {
        CommandGroup(after: .help) {
            Divider()

            Button(updater.updateActionTitle) {
                updater.performUpdateAction()
            }
            .disabled(!updater.canPerformUpdateAction)

            if updater.supportsChannels {
                Picker("Update Channel", selection: $updater.updateChannel) {
                    ForEach(UpdateChannelPreference.allCases) { channel in
                        Text(channel.name).tag(channel)
                    }
                }
            }
        }
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

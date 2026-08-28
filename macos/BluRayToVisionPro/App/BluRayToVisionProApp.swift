import SwiftUI

enum AppWindowID {
    static let settings = "settings"
}

struct ConversionSourceSelectionAction {
    let perform: () -> Void
}

private struct ConversionSourceSelectionActionKey: FocusedValueKey {
    typealias Value = ConversionSourceSelectionAction
}

extension FocusedValues {
    var conversionSourceSelectionAction: ConversionSourceSelectionAction? {
        get { self[ConversionSourceSelectionActionKey.self] }
        set { self[ConversionSourceSelectionActionKey.self] = newValue }
    }
}

@main
struct BluRayToVisionProApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var viewModel: ConversionViewModel
    @StateObject private var previewViewModel: PreviewViewModel
    @StateObject private var diagnosticReportViewModel: DiagnosticReportViewModel
    @StateObject private var updater: UpdateController
    @StateObject private var settings = AppSettings()
    @StateObject private var profileStore: ProfileStore
    @StateObject private var resolutionMemoryStore: ResolutionMemoryStore
    @StateObject private var durableQueueStore: ConversionQueueStore

    private let capabilities = AppCapabilities.current
    private let workCoordinator: AppWorkCoordinator
    private let observabilityEventStore: any ObservabilityEventPersisting
    private let suppressDefaultLaunch: Bool

    init() {
        let observabilityEventStore = ObservabilityEventStore.automatic()
        let resolutionMemoryStore = ResolutionMemoryStore()
        let profileStore = ProfileStore(resolutionMemoryStore: resolutionMemoryStore)
        let durableQueueStore = ConversionQueueStore()
        let offPeakScheduleStore = OffPeakScheduleStore()
        let viewModel = ConversionViewModel(
            observabilityEventStore: observabilityEventStore,
            durableQueueStore: durableQueueStore,
            offPeakScheduleStore: offPeakScheduleStore
        )
        let previewViewModel = PreviewViewModel(observabilityEventStore: observabilityEventStore)
        let workCoordinator = AppWorkCoordinator(conversion: viewModel, preview: previewViewModel)
        let diagnosticConfiguration = DiagnosticServiceConfiguration.configured()
        let diagnosticUploader = diagnosticConfiguration.map {
            DiagnosticReportClient(configuration: $0)
        }
        let diagnosticReportViewModel = DiagnosticReportViewModel(
            uploader: diagnosticUploader,
            capture: { outputDirectory, userComment in
                try await workCoordinator.captureDiagnosticBundle(
                    in: outputDirectory,
                    userComment: userComment
                )
            }
        )
        _viewModel = StateObject(wrappedValue: viewModel)
        _previewViewModel = StateObject(wrappedValue: previewViewModel)
        _diagnosticReportViewModel = StateObject(wrappedValue: diagnosticReportViewModel)
        _updater = StateObject(wrappedValue: UpdateController(installPostponer: workCoordinator))
        _durableQueueStore = StateObject(wrappedValue: durableQueueStore)
        _profileStore = StateObject(wrappedValue: profileStore)
        _resolutionMemoryStore = StateObject(wrappedValue: resolutionMemoryStore)
        self.workCoordinator = workCoordinator
        self.observabilityEventStore = observabilityEventStore
        suppressDefaultLaunch = AppDelegate.isAutomationSmoke(arguments: ProcessInfo.processInfo.arguments)
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
                resolutionMemoryStore: resolutionMemoryStore,
                capabilities: capabilities
            )
                .frame(minWidth: 920, minHeight: 680)
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
        .defaultLaunchBehavior(suppressDefaultLaunch ? .suppressed : .automatic)
        .windowResizability(.contentMinSize)
        .windowToolbarStyle(.unified)
        .commands {
            SettingsWindowCommands()
            UpdateCommands(updater: updater)
            SourceCommands()
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

}

private struct SourceCommands: Commands {
    @FocusedValue(\.conversionSourceSelectionAction) private var sourceSelectionAction

    var body: some Commands {
        CommandGroup(replacing: .newItem) {
            Button("Add Source…") {
                sourceSelectionAction?.perform()
            }
            .keyboardShortcut("o")
            .disabled(sourceSelectionAction == nil)
        }
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

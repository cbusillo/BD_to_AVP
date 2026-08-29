import SwiftUI

enum AppWindowID {
    static let settings = "settings"
}

struct PersistentQueueCommandActions {
    let state: PersistentQueueCommandState
    let addSources: () -> Void
    let addSourceFolder: () -> Void
    let addDisc: (ConversionSource) -> Void
    let start: () -> Void
    let pauseAfterCurrent: () -> Void
    let stopCurrent: () -> Void
    let moveUp: () -> Void
    let moveDown: () -> Void
    let convertNext: () -> Void
    let removeSelectedItem: () -> Void
    let undoRemove: () -> Void
}

private struct PersistentQueueCommandActionsKey: FocusedValueKey {
    typealias Value = PersistentQueueCommandActions
}

extension FocusedValues {
    var persistentQueueCommandActions: PersistentQueueCommandActions? {
        get { self[PersistentQueueCommandActionsKey.self] }
        set { self[PersistentQueueCommandActionsKey.self] = newValue }
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
            PersistentQueueCommands()
        }

        Window("Settings", id: AppWindowID.settings) {
            SettingsView(
                settings: settings,
                profileStore: profileStore,
                updater: updater
            )
            .focusedSceneValue(\.persistentQueueCommandActions, nil)
        }
        .defaultSize(width: 900, height: 680)
        .windowResizability(.contentMinSize)
    }

}

private struct PersistentQueueCommands: Commands {
    @FocusedValue(\.persistentQueueCommandActions) private var actions

    var body: some Commands {
        CommandMenu("Queue") {
            Button("Add Sources…") {
                actions?.addSources()
            }
            .keyboardShortcut("o", modifiers: .command)
            .disabled(actions == nil)

            Button("Add Folder of Movies…") {
                actions?.addSourceFolder()
            }
            .disabled(actions == nil)

            if let actions, !actions.state.insertedDiscs.isEmpty {
                Divider()
                ForEach(actions.state.insertedDiscs, id: \.url) { disc in
                    Button("Add \(disc.displayName)") {
                        actions.addDisc(disc)
                    }
                }
            }

            Divider()

            Button(actions?.state.startTitle ?? "Start Queue") {
                actions?.start()
            }
            .disabled(actions?.state.canStart != true)

            Button("Pause After Current") {
                actions?.pauseAfterCurrent()
            }
            .disabled(actions?.state.canPauseAfterCurrent != true)

            Button("Stop Current", role: .destructive) {
                actions?.stopCurrent()
            }
            .disabled(actions?.state.canStopCurrent != true)

            Divider()

            Button("Move Up") {
                actions?.moveUp()
            }
            .keyboardShortcut(.upArrow, modifiers: [.command, .option])
            .disabled(actions?.state.canMoveUp != true)

            Button("Move Down") {
                actions?.moveDown()
            }
            .keyboardShortcut(.downArrow, modifiers: [.command, .option])
            .disabled(actions?.state.canMoveDown != true)

            Button("Convert Next") {
                actions?.convertNext()
            }
            .keyboardShortcut(.return, modifiers: [.command, .option])
            .disabled(actions?.state.canConvertNext != true)

            if let actions {
                if let reason = actions.state.selectedItemLockReason {
                    Text("Arrangement unavailable: \(reason)")
                        .foregroundStyle(.secondary)
                } else if actions.state.selectedItemID == nil {
                    Text("Select a waiting item to arrange it.")
                        .foregroundStyle(.secondary)
                }
            }

            Divider()

            Button("Remove", role: .destructive) {
                actions?.removeSelectedItem()
            }
            .disabled(actions?.state.canRemoveSelectedItem != true)

            if let actions {
                if let reason = actions.state.selectedItemRemovalLockReason {
                    Text("Removal unavailable: \(reason)")
                        .foregroundStyle(.secondary)
                } else if actions.state.selectedItemID == nil {
                    Text("Select an item to remove it.")
                        .foregroundStyle(.secondary)
                }
            }

            Button("Undo Remove") {
                actions?.undoRemove()
            }
            .disabled(actions?.state.canUndo != true)
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

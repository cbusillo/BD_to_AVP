import AppKit
import SwiftUI

struct ContentView: View {
    @ObservedObject var viewModel: ConversionViewModel
    @ObservedObject var previewViewModel: PreviewViewModel
    @ObservedObject var diagnosticReportViewModel: DiagnosticReportViewModel
    @ObservedObject var settings: AppSettings
    @ObservedObject var profileStore: ProfileStore
    @ObservedObject var resolutionMemoryStore: ResolutionMemoryStore
    let capabilities: AppCapabilities

    @State private var selectedProfileID: String
    @State private var options: ConversionOptions
    @State private var destinationURL: URL
    @State private var insertedDiscs: [ConversionSource] = []
    @State private var isShowingActivity = false
    @State private var isDropTargeted = false
    @State private var queueAdmissionNoticeMessage: String?
    @State private var persistentQueueErrorMessage: String?
    @State private var persistentQueueRemovalToken: PersistentQueueRemovalToken?
    @State private var queueItemBeingEdited: PersistentQueueItem?
    @State private var configuredSource: ConversionSource?
    @State private var isShowingSourceConfiguration = false
    @State private var isShowingSourceConfigurationEditor = false
    @State private var compactPersistentQueueRows = false
    @State private var preserveEncodingOnNextProfileChange = false
    @State private var titleSelection = DiscTitleSelection.main
    @State private var isShowingDiagnosticReport = false
    @State private var isRefreshingDiscs = false
    @State private var isShowingOffPeakSchedule = false
    @State private var isEditingOffPeakSchedule = false
    @State private var offPeakScheduleStartAt: Date
    @State private var offPeakScheduleEndAt: Date
    @State private var offPeakScheduleEditorError: String?
    @State private var didEvaluateOffPeakScheduleAtLaunch = false
    @State private var offPeakScheduleTimer = Timer.publish(every: 30, on: .main, in: .common).autoconnect()
    @StateObject private var routeQualityState: RouteQualityResolutionState

    init(
        viewModel: ConversionViewModel,
        previewViewModel: PreviewViewModel,
        diagnosticReportViewModel: DiagnosticReportViewModel,
        settings: AppSettings,
        profileStore: ProfileStore,
        resolutionMemoryStore: ResolutionMemoryStore,
        capabilities: AppCapabilities
    ) {
        _viewModel = ObservedObject(wrappedValue: viewModel)
        _previewViewModel = ObservedObject(wrappedValue: previewViewModel)
        _diagnosticReportViewModel = ObservedObject(wrappedValue: diagnosticReportViewModel)
        _settings = ObservedObject(wrappedValue: settings)
        _profileStore = ObservedObject(wrappedValue: profileStore)
        _resolutionMemoryStore = ObservedObject(wrappedValue: resolutionMemoryStore)
        self.capabilities = capabilities

        let profile = profileStore.profile(withID: settings.selectedProfileID)
        var initialJobOptions = Self.jobOptions(from: settings)
        if let pipelineDefaults = profile.pipelineDefaults {
            initialJobOptions.applyProfilePipelineDefaults(pipelineDefaults)
        }
        let initialOptions = ConversionOptions(
            encoding: profile.options,
            job: initialJobOptions
        )

        _selectedProfileID = State(initialValue: profile.id)
        _options = State(initialValue: initialOptions)
        _destinationURL = State(initialValue: settings.destinationURL)
        let initialScheduleStart = Date().addingTimeInterval(15 * 60)
        _offPeakScheduleStartAt = State(initialValue: initialScheduleStart)
        _offPeakScheduleEndAt = State(initialValue: initialScheduleStart.addingTimeInterval(8 * 60 * 60))
        _routeQualityState = StateObject(wrappedValue: RouteQualityResolutionState())
    }

    var body: some View {
        presentedContent
    }

    private var mainContent: some View {
        VStack(spacing: 0) {
            noticeContent

            workspaceContent

            Divider()
            statusFooter

            activityContent
        }
    }

    @ViewBuilder
    private var workspaceContent: some View {
        HSplitView {
            persistentQueueSidebar
                .frame(minWidth: 300, idealWidth: 330, maxWidth: 420)
            PersistentQueueDetailView(
                item: viewModel.selectedPersistentQueueItem,
                resolutionGroup: selectedPersistentQueueResolutionGroup,
                resolutionMemoryStore: resolutionMemoryStore,
                activeProgress: viewModel.state.progress,
                activeElapsedText: viewModel.state.elapsedText,
                edit: { queueItemBeingEdited = $0 },
                changeDestination: changePersistentQueueDestination,
                retry: retryPersistentQueueItem,
                resolveRouteQuality: resolvePersistentQueueRouteQuality
            )
            .frame(minWidth: 600, idealWidth: 760)
        }
    }

    @ViewBuilder
    private var noticeContent: some View {
        if let migrationNoticeMessage = profileStore.migrationNoticeMessage {
            Label(migrationNoticeMessage, systemImage: "exclamationmark.triangle.fill")
                .font(.callout)
                .foregroundStyle(.orange)
                .padding(.horizontal)
                .padding(.vertical, 10)
        }

    }

    @ViewBuilder
    private var activityContent: some View {
        if isShowingActivity {
            Divider()
            ActivityDrawer(
                state: viewModel.state,
                observabilityStatus: viewModel.liveObservabilityStatus,
                showTechnicalDetails: settings.showTechnicalDetails
            )
            .transition(.move(edge: .bottom).combined(with: .opacity))
        }
    }

    private var baseContent: some View {
        mainContent
        .accessibilityIdentifier("main-window-content")
        .focusedSceneValue(\.persistentQueueCommandActions, persistentQueueCommandActions)
        .toolbar { toolbarContent }
        .animation(.easeInOut(duration: 0.18), value: isShowingActivity)
        .dropDestination(for: URL.self, action: acceptDrop) { targeted in
            isDropTargeted = targeted
        }
        .overlay {
            if isDropTargeted {
                RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(Color.accentColor, style: StrokeStyle(lineWidth: 3, dash: [8, 5]))
                    .background(Color.accentColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
                    .padding(8)
                    .allowsHitTesting(false)
                    .overlay {
                        Label(
                            "Add these sources to the queue",
                            systemImage: "arrow.down.doc.fill"
                        )
                            .font(.title3.weight(.semibold))
                            .padding(14)
                            .background(.regularMaterial, in: Capsule())
                }
            }
        }
    }

    private var lifecycleObservedContent: some View {
        baseContent
        .onAppear {
            refreshDiscs()
            guard !didEvaluateOffPeakScheduleAtLaunch else { return }
            didEvaluateOffPeakScheduleAtLaunch = true
            evaluateOffPeakSchedule(appLaunched: true)
        }
        .onReceive(NSWorkspace.shared.notificationCenter.publisher(for: NSWorkspace.didMountNotification)) { _ in
            refreshDiscs()
        }
        .onReceive(NSWorkspace.shared.notificationCenter.publisher(for: NSWorkspace.didUnmountNotification)) {
            notification in
            handleVolumeUnmount(notification)
        }
        .onReceive(NSWorkspace.shared.notificationCenter.publisher(for: NSWorkspace.didWakeNotification)) { _ in
            evaluateOffPeakSchedule()
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            evaluateOffPeakSchedule()
        }
        .onReceive(offPeakScheduleTimer) { _ in
            evaluateOffPeakSchedule()
        }
        .onChange(of: previewViewModel.hasActiveWorker) { _, isActive in
            if !isActive {
                evaluateOffPeakSchedule()
            }
        }
        .onChange(of: viewModel.hasActiveWorker) { _, isActive in
            if !isActive {
                refreshDiscs()
            }
        }
        .onChange(of: viewModel.persistentQueueItems) { _, _ in
            if let token = persistentQueueRemovalToken,
               !viewModel.isPersistentQueueRemovalTokenValid(token)
            {
                persistentQueueRemovalToken = nil
            }
        }
    }

    private var settingsObservedContent: some View {
        lifecycleObservedContent
        .onChange(of: viewModel.state.jobID) { previousJobID, currentJobID in
            if currentJobID != nil, currentJobID != previousJobID {
                diagnosticReportViewModel.prepareForNewDiagnosticSession()
            }
        }
        .onChange(of: selectedProfileID) { _, _ in
            if preserveEncodingOnNextProfileChange {
                preserveEncodingOnNextProfileChange = false
                return
            }
            resetProfile()
        }
        .onChange(of: settings.selectedProfileID) { _, newValue in
            guard viewModel.source == nil else {
                return
            }
            selectedProfileID = profileStore.normalizedProfileID(newValue)
            resetProfile()
        }
        .onChange(of: settings.destinationURL) { _, newValue in
            if viewModel.source == nil {
                destinationURL = newValue
            }
        }
        .onChange(of: defaultJobOptions) { _, newValue in
            if viewModel.source == nil, !viewModel.hasActiveWork {
                routeQualityState.reset()
                if selectedProfile.pipelineDefaults == nil {
                    options.job = newValue
                } else {
                    options.job.keepAwake = newValue.keepAwake
                    options.job.playSound = newValue.playSound
                }
            }
        }
    }

    private var observedContent: some View {
        settingsObservedContent
        .onChange(of: viewModel.state.conversionResult) { _, result in
            guard viewModel.batchQueue == nil,
                  let result,
                  viewModel.queueItems.isEmpty
            else {
                return
            }
            if settings.revealOutput {
                NSWorkspace.shared.activateFileViewerSelecting([result.outputURL])
            }
            if settings.playSound {
                NSSound(named: "Glass")?.play()
            }
        }
        .onChange(of: viewModel.batchQueue?.completionID) { _, completionID in
            guard completionID != nil, let queue = viewModel.batchQueue else {
                return
            }
            if settings.revealOutput, !queue.completedOutputURLs.isEmpty {
                NSWorkspace.shared.activateFileViewerSelecting(queue.completedOutputURLs)
            }
            if settings.playSound, !queue.stopRequested, queue.completedCount > 0 {
                NSSound(named: "Glass")?.play()
            }
        }
        .onChange(of: viewModel.completedBatchResults) { _, results in
            guard let results, !results.isEmpty else {
                return
            }
            if settings.revealOutput {
                NSWorkspace.shared.activateFileViewerSelecting(results.map(\.outputURL))
            }
            if settings.playSound {
                NSSound(named: "Glass")?.play()
            }
        }
        .onChange(of: viewModel.state.result?.titles) { _, _ in
            if viewModel.source?.kind != .sourceFolder {
                titleSelection = .main
            }
        }
        .onChange(of: profileStore.customProfiles) { previousProfiles, currentProfiles in
            let normalizedIdentifier = profileStore.normalizedProfileID(selectedProfileID)
            if normalizedIdentifier != selectedProfileID {
                preserveEncodingOnNextProfileChange = viewModel.source != nil
                selectedProfileID = normalizedIdentifier
                return
            }
            guard let previousProfile = previousProfiles.first(where: { $0.id == selectedProfileID }),
                  let currentProfile = currentProfiles.first(where: { $0.id == selectedProfileID }),
                  !viewModel.hasActiveWork,
                  options.encoding == previousProfile.options,
                  options.job.profilePipelineDefaults == profilePipelineDefaults(for: previousProfile)
            else {
                return
            }
            routeQualityState.reset()
            options.encoding = currentProfile.options
            options.job.applyProfilePipelineDefaults(profilePipelineDefaults(for: currentProfile))
        }
    }

    private var presentedContent: some View {
        observedContent
        .sheet(item: $queueItemBeingEdited) { item in
            SetupEditSheet(
                initialProfile: item.draft.profile,
                initialOptions: item.draft.options,
                fallbackPipelineDefaults: defaultJobOptions.profilePipelineDefaults,
                sourceKind: item.draft.source.kind,
                profiles: profileStore.profiles,
                profileStore: profileStore,
                resolutionMemoryStore: resolutionMemoryStore,
                applyToConversion: { profileID, editedOptions in
                    updatePersistentQueueItem(item, profileID: profileID, options: editedOptions)
                },
                queueConflictForReview: { profileID, editedOptions, conflict in
                    updatePersistentQueueItem(
                        item,
                        profileID: profileID,
                        options: editedOptions,
                        routeQualityConflict: conflict
                    )
                }
            )
        }
        .sheet(isPresented: $isShowingSourceConfiguration, onDismiss: finishSourceConfiguration) {
            if let configuredSource {
                SourceConfigurationSheet(
                    source: configuredSource,
                    inspection: viewModel.state.result,
                    inspectionFailureMessage: viewModel.state.failureMessage,
                    inspectionWarningMessage: viewModel.state.warningMessage,
                    profile: selectedProfile,
                    destinationURL: destinationURL,
                    titleSelection: $titleSelection,
                    drafts: conversionDrafts,
                    previewViewModel: previewViewModel,
                    queueAdmissionNoticeMessage: $queueAdmissionNoticeMessage,
                    persistentQueueErrorMessage: $persistentQueueErrorMessage,
                    openSettings: { isShowingSourceConfigurationEditor = true },
                    addToQueue: addConfiguredDraftsToPersistentQueue,
                    startQueue: startConfiguredDrafts
                )
                .sheet(isPresented: $isShowingSourceConfigurationEditor) {
                    SetupEditSheet(
                        initialProfile: selectedProfile,
                        initialOptions: options,
                        fallbackPipelineDefaults: defaultJobOptions.profilePipelineDefaults,
                        sourceKind: configuredSource.kind,
                        profiles: profileStore.profiles,
                        profileStore: profileStore,
                        resolutionMemoryStore: resolutionMemoryStore,
                        applyToConversion: applyConfiguredOptions,
                        queueConflictForReview: queueConfiguredConflictForReview
                    )
                }
            }
        }
        .sheet(isPresented: $isShowingDiagnosticReport) {
            DiagnosticReportSheet(viewModel: diagnosticReportViewModel)
        }
        .sheet(isPresented: $isShowingOffPeakSchedule) {
            OffPeakScheduleSheet(
                startAt: $offPeakScheduleStartAt,
                endAt: $offPeakScheduleEndAt,
                isEditing: isEditingOffPeakSchedule,
                hasPhysicalDiscItems: hasEligiblePhysicalDiscItems,
                errorMessage: offPeakScheduleEditorError,
                cancel: { isShowingOffPeakSchedule = false },
                save: saveOffPeakSchedule
            )
        }
        .alert(
            "Already in Queue",
            isPresented: Binding(
                get: { queueAdmissionNoticeMessage != nil },
                set: { if !$0 { queueAdmissionNoticeMessage = nil } }
            )
        ) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(queueAdmissionNoticeMessage ?? "This movie is already in the queue.")
        }
        .alert(
            "Queue Could Not Be Updated",
            isPresented: Binding(
                get: { persistentQueueErrorMessage != nil },
                set: { if !$0 { persistentQueueErrorMessage = nil } }
            )
        ) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(persistentQueueErrorMessage ?? "The queue could not be updated.")
        }
    }

    private var persistentQueueSidebar: some View {
        let commandState = persistentQueueCommandState
        return PersistentQueueSidebarView(
            items: viewModel.persistentQueueItems,
            selectedID: Binding(
                get: { viewModel.selectedPersistentQueueItemID },
                set: viewModel.selectPersistentQueueItem
            ),
            compactRows: $compactPersistentQueueRows,
            commandState: commandState,
            makeMKVAvailable: DiscSourceDetector.makeMKVAvailable,
            activeProgress: viewModel.state.progress,
            activeElapsedText: viewModel.state.elapsedText,
            offPeakScheduleOutcome: viewModel.offPeakScheduleOutcome,
            offPeakScheduleErrorMessage: viewModel.offPeakScheduleErrorMessage,
            addSources: addSourcesToPersistentQueue,
            addSourceFolder: addSourceFolderToPersistentQueue,
            addDisc: addDiscToPersistentQueue,
            addDroppedURLs: acceptDrop,
            move: movePersistentQueueItem,
            moveRelative: movePersistentQueueItem,
            moveNext: movePersistentQueueItemNext,
            remove: removePersistentQueueItem,
            clearCompleted: clearCompletedPersistentQueueItems,
            undo: undoPersistentQueueRemoval,
            start: startPersistentQueue,
            startLater: presentNewOffPeakSchedule,
            editSchedule: presentExistingOffPeakSchedule,
            cancelSchedule: cancelOffPeakSchedule,
            dismissScheduleOutcome: clearOffPeakScheduleOutcome,
            pauseAfterCurrent: pausePersistentQueueAfterCurrent,
            stopCurrent: stopCurrentPersistentQueueItem
        )
    }

    private var selectedPersistentQueueResolutionGroup: QueueResolutionGroup? {
        guard let selectedItemID = viewModel.selectedPersistentQueueItemID else {
            return nil
        }
        return viewModel.persistentQueueResolutionGroups.first { group in
            group.candidates.contains(where: { $0.id == selectedItemID })
        }
    }

    private var persistentQueueCommandActions: PersistentQueueCommandActions {
        return PersistentQueueCommandActions(
            state: persistentQueueCommandState,
            addSources: addSourcesToPersistentQueue,
            addSourceFolder: addSourceFolderToPersistentQueue,
            addDisc: addDiscToPersistentQueue,
            start: startPersistentQueue,
            pauseAfterCurrent: pausePersistentQueueAfterCurrent,
            stopCurrent: stopCurrentPersistentQueueItem,
            moveUp: { moveSelectedPersistentQueueItem(by: -1) },
            moveDown: { moveSelectedPersistentQueueItem(by: 1) },
            convertNext: moveSelectedPersistentQueueItemNext,
            removeSelectedItem: removeSelectedPersistentQueueItem,
            undoRemove: undoPersistentQueueRemoval
        )
    }

    private var persistentQueueCommandState: PersistentQueueCommandState {
        PersistentQueueCommandState(
            items: viewModel.persistentQueueItems,
            selectedItemID: viewModel.selectedPersistentQueueItemID,
            runState: viewModel.persistentQueueRunState,
            hasActiveWorker: viewModel.hasActiveWorker,
            hasPreviewWorker: previewViewModel.hasActiveWorker,
            offPeakSchedule: viewModel.offPeakSchedule,
            insertedDiscs: insertedDiscs,
            removalTokenIsValid: viewModel.isPersistentQueueRemovalTokenValid(persistentQueueRemovalToken)
        )
    }

    private func moveSelectedPersistentQueueItem(by offset: Int) {
        let state = persistentQueueCommandState
        guard let selectedID = state.selectedItemID,
              (offset == -1 ? state.canMoveUp : offset == 1 ? state.canMoveDown : false)
        else {
            return
        }
        movePersistentQueueItem(selectedID, by: offset)
    }

    private func moveSelectedPersistentQueueItemNext() {
        let state = persistentQueueCommandState
        guard state.canConvertNext, let selectedID = state.selectedItemID else { return }
        movePersistentQueueItemNext(selectedID)
    }

    private func removeSelectedPersistentQueueItem() {
        let state = persistentQueueCommandState
        guard state.canRemoveSelectedItem, let selectedID = state.selectedItemID else { return }
        removePersistentQueueItem(selectedID)
    }

    private func resolvePersistentQueueRouteQuality(
        group: QueueResolutionGroup,
        selection: QueueResolutionSelection
    ) {
        Task { @MainActor in
            do {
                try await viewModel.resolvePersistentQueueItems(group: group, selection: selection)
            } catch {
                persistentQueueErrorMessage = error.localizedDescription
            }
        }
    }

    private var hasEligiblePhysicalDiscItems: Bool {
        viewModel.persistentQueueItems.contains { item in
            guard item.draft.source.kind == .physicalDisc else { return false }
            return switch item.status {
            case .waiting, .interrupted, .stopped, .notStarted:
                true
            case .needsChoice, .inspecting, .processing, .stopping, .attention, .failed, .completed:
                false
            }
        }
    }

    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        ToolbarItem(placement: .navigation) {
            sourceMenu
        }

        ToolbarItem(placement: .automatic) {
            if viewModel.source?.kind == .physicalDisc {
                Button(action: refreshDiscs) {
                    Label("Refresh Disc", systemImage: "arrow.clockwise")
                }
                .help("Refresh inserted 3D Blu-ray discs")
                .disabled(viewModel.hasActiveWork)
            }
        }
    }

    private var sourceMenu: some View {
        Menu {
            if insertedDiscs.isEmpty {
                Button("No Inserted Disc Detected") {}
                    .disabled(true)
            } else {
                ForEach(insertedDiscs, id: \.url) { disc in
                    Button("Add \(disc.displayName) to Queue") {
                        appendSourcesToPersistentQueue([disc])
                    }
                }
            }

            Button("Refresh Disc Drives", action: refreshDiscs)

            Divider()
            Button("Add Disc Image…") { chooseFile(.discImage) }
            Button("Add Blu-ray Folder…") { chooseFolder(.bluRayFolder) }
            Button("Add Folder of Movies…") { chooseFolder(.sourceFolder) }
            Button("Add 3D MKV…") { chooseFile(.matroska) }

            Divider()
            Button("Add MTS or M2TS…") { chooseFile(.transportStream) }

            Divider()
            Button("Configure Source…", action: configureExistingSource)
        } label: {
            Label("Add Sources", systemImage: "plus")
        }
        .help("Add a physical disc, disc image, Blu-ray folder, source folder, MKV, or transport stream to the queue")
        .disabled(!canSelectSource)
    }

    private var statusFooter: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(statusColor)
                .frame(width: 8, height: 8)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 2) {
                Text(statusText)
                    .font(.callout.weight(.medium))
                if let secondaryStatusText {
                    Text(secondaryStatusText)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel(statusAccessibilityLabel)
            .accessibilityIdentifier("main-status")

            if viewModel.hasActiveWorker {
                WorkerProgressGauge(progress: viewModel.state.progress, width: 64)
                    .padding(.leading, 4)

                if let progress = viewModel.state.progress {
                    Text(progress.compactText)
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                }

                if let elapsedText = viewModel.state.elapsedText {
                    Label("Elapsed \(elapsedText)", systemImage: "clock")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                }
            }

            Spacer()

            if canShowDiagnosticAction {
                Button(action: showDiagnosticReport) {
                    Label(diagnosticActionTitle, systemImage: "stethoscope")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help(diagnosticActionHelp)
                .accessibilityLabel(diagnosticActionTitle.replacingOccurrences(of: "…", with: ""))
                .accessibilityHint("Captures diagnostics without stopping the current conversion")
                .accessibilityIdentifier("diagnostics-action")
            }

            Button {
                isShowingActivity.toggle()
            } label: {
                Label(
                    isShowingActivity ? "Hide Activity" : "Show Activity",
                    systemImage: isShowingActivity ? "chevron.down" : "chevron.up"
                )
            }
            .buttonStyle(.plain)
            .accessibilityLabel(isShowingActivity ? "Hide activity details" : "Show activity details")

            if viewModel.hasStoppableWork {
                Button("Stop", role: .destructive, action: viewModel.stopActiveWorker)
                    .keyboardShortcut("p", modifiers: .command)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background { StructuralChromeBackground() }
    }

    private var selectedProfile: EncodingProfile {
        profileStore.profile(withID: selectedProfileID)
    }

    private var diagnosticActionTitle: String {
        if case .success = diagnosticReportViewModel.phase {
            return "View Support Code…"
        }
        return diagnosticReportViewModel.isUploadAvailable ? "Send Diagnostics…" : "Save Diagnostics…"
    }

    private var canShowDiagnosticAction: Bool {
        if viewModel.hasDiagnosticEvidence
            || previewViewModel.hasDiagnosticEvidence
            || diagnosticReportViewModel.hasLocalArtifact {
            return true
        }
        if case .success = diagnosticReportViewModel.phase {
            return true
        }
        return false
    }

    private var diagnosticActionHelp: String {
        diagnosticReportViewModel.isUploadAvailable
            ? "Capture, review, and send privacy-safe diagnostics without stopping the current conversion."
            : "Capture, review, and save privacy-safe diagnostics without stopping the current conversion."
    }

    private func showDiagnosticReport() {
        diagnosticReportViewModel.begin()
        isShowingDiagnosticReport = true
    }

    private var defaultJobOptions: JobOptions {
        Self.jobOptions(from: settings)
    }

    private var selectedTitles: [SourceTitle] {
        guard let inspection = viewModel.state.result else {
            return []
        }
        return titleSelection.resolvedTitles(in: inspection)
    }

    private var conversionDrafts: [ConversionDraft] {
        guard routeQualityBlockReason == nil else {
            return []
        }
        return makeConversionDrafts(options: options, profile: selectedProfile)
    }

    private func makeConversionDrafts(
        options draftOptions: ConversionOptions,
        profile draftProfile: EncodingProfile
    ) -> [ConversionDraft] {
        guard let source = viewModel.source,
              source.kind != .sourceFolder,
              let inspection = viewModel.state.result
        else {
            return []
        }
        if source.kind.isDiscWorkflow {
            return selectedTitles.map { title in
                ConversionDraft(
                    source: source,
                    sourceDetails: inspection,
                    profile: draftProfile,
                    destinationURL: destinationURL,
                    options: draftOptions,
                    selectedTitle: title
                )
            }
        }
        return [
            ConversionDraft(
                source: source,
                sourceDetails: inspection,
                profile: draftProfile,
                destinationURL: destinationURL,
                options: draftOptions
            )
        ]
    }

    private var draft: ConversionDraft? {
        conversionDrafts.count == 1 ? conversionDrafts[0] : nil
    }

    private var plannedOutputURLs: [URL] {
        conversionDrafts.map(\.proposedOutputURL)
    }

    private var selectedVideoCount: Int {
        conversionDrafts.count
    }

    private var statusText: String {
        if let batchQueue = viewModel.batchQueue {
            return batchQueue.summaryText
        }
        if viewModel.hasActiveWorker {
            let stage = viewModel.state.stageMessage
                ?? (viewModel.state.operationKind == .inspection ? "Reading source details" : "Converting video")
            if let queuePosition {
                return "Video \(queuePosition.current) of \(queuePosition.total): \(stage)"
            }
            return stage
        }
        if viewModel.state.phase == .decisionRequired {
            return "Choose how to continue"
        }
        if viewModel.state.phase == .failed {
            if let completedCount = viewModel.completedBatchResults?.count, completedCount > 0 {
                return "\(completedCount) conversion\(completedCount == 1 ? "" : "s") completed before the queue stopped"
            }
            return "Source needs attention"
        }
        if viewModel.state.phase == .cancelled {
            if let completedCount = viewModel.completedBatchResults?.count, completedCount > 0 {
                return "\(completedCount) conversion\(completedCount == 1 ? "" : "s") completed before the queue stopped"
            }
            return viewModel.queueItems.isEmpty ? "Conversion cancelled" : "Conversion queue cancelled"
        }
        if viewModel.state.conversionResult != nil {
            if let results = viewModel.completedBatchResults {
                let allCompleted = !viewModel.queueItems.isEmpty && viewModel.queueItems.allSatisfy { item in
                    if case .completed = item.status { return true }
                    return false
                }
                return allCompleted
                    ? "All \(results.count) conversions complete"
                    : "\(results.count) conversion\(results.count == 1 ? "" : "s") completed before the queue stopped"
            }
            return "Conversion complete"
        }
        guard let source = viewModel.source else {
            return "Insert a 3D Blu-ray disc or choose another source"
        }
        if viewModel.state.result != nil {
            if selectedVideoCount > 1 {
                return "\(selectedVideoCount) 3D videos ready to convert"
            }
            return "Source analyzed and conversion settings ready"
        }
        if source.kind.isDiscWorkflow {
            return "Disc workflow ready"
        }
        return "Conversion settings ready"
    }

    private var statusAccessibilityLabel: String {
        var components = ["Status: \(statusText)"]
        if let secondaryStatusText {
            components.append(secondaryStatusText)
        }
        if let elapsedText = viewModel.state.elapsedText, viewModel.hasActiveWorker {
            components.append("Elapsed time \(elapsedText)")
        }
        if let progress = viewModel.state.progress, viewModel.hasActiveWorker {
            components.append(progress.accessibilityValue)
        }
        return components.joined(separator: ". ")
    }

    private var secondaryStatusText: String? {
        if let warningMessage = viewModel.state.warningMessage {
            return "Warning: \(warningMessage)"
        }
        if let durableQueueLoadErrorMessage = viewModel.durableQueueLoadErrorMessage {
            return durableQueueLoadErrorMessage
        }
        if let durableQueueRuntimeDiagnostic = viewModel.durableQueueRuntimeDiagnostic {
            return durableQueueRuntimeDiagnostic
        }
        if let persistentQueueProjectionError = viewModel.persistentQueueProjectionError {
            return "Queue display is unavailable: \(String(describing: persistentQueueProjectionError))"
        }
        if viewModel.state.operationKind == .conversion,
           let videoRoute = viewModel.state.videoRoute
        {
            return videoRoute.compactSummary
        }
        if let batchQueue = viewModel.batchQueue {
            if batchQueue.isRunning {
                return viewModel.state.stageMessage
                    ?? (viewModel.state.operationKind == .inspection ? "Reading source details" : "Processing video")
            }
            if batchQueue.items.isEmpty {
                return "Choose a folder containing ISO, MKV, MTS, or M2TS sources."
            }
            if batchQueue.isFinished {
                return batchQueue.failedCount > 0
                    ? "Review failed items below or retry them individually."
                    : destinationURL.path
            }
            return "Ready to convert sequentially to \(destinationURL.path)"
        }
        if viewModel.hasActiveWorker {
            let activity = viewModel.state.activityMessage
                ?? (viewModel.state.operationKind == .inspection ? "Inspecting video streams" : "Processing video")
            if let activeQueueItem {
                return "\(activeQueueItem.displayName) — \(activity)"
            }
            return activity
        }
        guard viewModel.source != nil else {
            return DiscSourceDetector.makeMKVAvailable ? "MakeMKV is ready for physical discs" : "MakeMKV is required for physical discs"
        }
        if !conversionCanStart {
            return conversionUnavailableReason
        }
        if let outputPath = viewModel.state.conversionResult?.outputPath {
            return outputPath
        }
        if plannedOutputURLs.count > 1 {
            return "\(plannedOutputURLs.count) files in \(destinationURL.path)"
        }
        return draft?.proposedOutputURL.path
    }

    private var statusColor: Color {
        if let batchQueue = viewModel.batchQueue {
            if batchQueue.isRunning {
                return .blue
            }
            if batchQueue.failedCount > 0 {
                return .red
            }
            if viewModel.state.warningMessage != nil {
                return .orange
            }
            if batchQueue.stoppedCount > 0 || batchQueue.notStartedCount > 0 {
                return .orange
            }
            return batchQueue.items.isEmpty ? .secondary : .green
        }
        if viewModel.hasActiveWorker {
            return .blue
        }
        if viewModel.state.phase == .decisionRequired {
            return .orange
        }
        if viewModel.state.phase == .failed {
            return .red
        }
        if viewModel.state.warningMessage != nil {
            return .orange
        }
        return viewModel.source == nil ? .secondary : .green
    }

    private var conversionCanStart: Bool {
        guard capabilities.conversionAvailable,
              routeQualityBlockReason == nil,
              !viewModel.hasActiveWork,
              !previewViewModel.hasActiveWorker,
              viewModel.state.phase != .decisionRequired
        else {
            return false
        }
        if isBatchSource {
            return viewModel.batchQueue?.items.isEmpty == false
                && !viewModel.isBatchRunning
        }
        return viewModel.source?.kind.supportsConversion == true
            && viewModel.state.result != nil
            && !conversionDrafts.isEmpty
            && viewModel.state.failureCode != "title_unavailable"
    }

    private var canSelectSource: Bool {
        viewModel.canSelectSource && !previewViewModel.hasActiveWorker
    }

    private var isBatchSource: Bool {
        viewModel.source?.kind == .sourceFolder
    }

    private var conversionUnavailableReason: String {
        if let routeQualityBlockReason {
            return routeQualityBlockReason
        }
        guard capabilities.conversionAvailable else {
            return capabilities.conversionUnavailableReason
        }
        if viewModel.state.phase == .decisionRequired {
            return "Choose a recovery option before starting another conversion."
        }
        if viewModel.state.failureCode == "title_unavailable" {
            return "Analyze the source again before converting another video."
        }
        switch viewModel.source?.kind {
        case .sourceFolder:
            return viewModel.batchQueue?.items.isEmpty == false
                ? "The batch is already active."
                : "No supported ISO, MKV, MTS, or M2TS sources were found in this folder."
        case .physicalDisc, .discImage, .bluRayFolder, .matroska, .transportStream:
            return viewModel.state.result == nil
                ? "Source analysis must complete before conversion can start."
                : capabilities.conversionUnavailableReason
        case .none:
            return capabilities.conversionUnavailableReason
        }
    }

    private var routeQualityBlockReason: String? {
        if let stateReason = routeQualityState.blockReason {
            return stateReason
        }
        if case let .failure(error) = RouteQualityEngine.validate(options) {
            return "Resolve the video and quality choices before starting, previewing, or adding this conversion to the queue. \(error.localizedDescription)"
        }
        return nil
    }

    private func resetProfile() {
        routeQualityState.reset()
        options.encoding = selectedProfile.options
        options.job = defaultJobOptions
        options.job.applyProfilePipelineDefaults(profilePipelineDefaults(for: selectedProfile))
    }

    private func profilePipelineDefaults(for profile: EncodingProfile) -> ProfilePipelineDefaults {
        profile.pipelineDefaults ?? defaultJobOptions.profilePipelineDefaults
    }

    private static func jobOptions(from settings: AppSettings) -> JobOptions {
        JobOptions(
            intermediatePolicy: settings.intermediatePolicy,
            softwareEncoder: settings.useSoftwareEncoder,
            keepAwake: settings.keepAwake,
            playSound: settings.playSound
        )
    }

    private func refreshDiscs() {
        guard !isRefreshingDiscs else {
            return
        }
        isRefreshingDiscs = true
        Task { @MainActor in
            let refreshedDiscs = await Task.detached(priority: .utility) {
                DiscSourceDetector.insertedDiscs()
            }.value
            insertedDiscs = refreshedDiscs
            isRefreshingDiscs = false
            guard !viewModel.hasActiveWork,
                  let selectedSource = viewModel.source,
                  selectedSource.kind == .physicalDisc,
                  !refreshedDiscs.contains(where: { $0.workerSourcePath == selectedSource.workerSourcePath })
            else {
                return
            }
            viewModel.clearSource()
        }
    }

    private func handleVolumeUnmount(_ notification: Notification) {
        if let volumeURL = notification.userInfo?[NSWorkspace.volumeURLUserInfoKey] as? URL {
            viewModel.sourceVolumeDidUnmount(volumeURL)
        }
        refreshDiscs()
    }

    private func configureExistingSource() {
        guard canSelectSource,
              let sourceURL = SourcePicker.chooseExistingSource(),
              let source = ConversionSource.infer(from: sourceURL)
        else {
            return
        }
        guard source.kind != .sourceFolder else {
            appendSourcesToPersistentQueue([source])
            return
        }
        titleSelection = .main
        configuredSource = source
        viewModel.selectSource(source)
        isShowingSourceConfiguration = true
    }

    private func addSourcesToPersistentQueue() {
        appendSourcesToPersistentQueue(SourcePicker.chooseQueueSources())
    }

    private func addSourceFolderToPersistentQueue() {
        guard let source = SourcePicker.chooseFolder(kind: .sourceFolder) else {
            return
        }
        appendSourcesToPersistentQueue([source])
    }

    private func addDiscToPersistentQueue(_ disc: ConversionSource) {
        guard insertedDiscs.contains(where: { $0.url == disc.url }) else {
            return
        }
        appendSourcesToPersistentQueue([disc])
    }

    private func appendSourcesToPersistentQueue(_ sources: [ConversionSource]) {
        guard !sources.isEmpty else { return }
        let drafts = sources.map { source in
            var sourceOptions = options
            if source.kind == .physicalDisc {
                sourceOptions.job.removeOriginalAfterSuccess = false
            }
            return ConversionDraft(
                source: source,
                sourceDetails: nil,
                profile: selectedProfile,
                destinationURL: destinationURL,
                options: sourceOptions
            )
        }
        appendDraftsToPersistentQueue(drafts, clearConfiguredSource: false)
    }

    private func chooseFile(_ kind: ConversionSourceKind) {
        guard canSelectSource, let source = SourcePicker.chooseFile(kind: kind) else {
            return
        }
        appendSourcesToPersistentQueue([source])
    }

    private func chooseFolder(_ kind: ConversionSourceKind) {
        guard canSelectSource, let source = SourcePicker.chooseFolder(kind: kind) else {
            return
        }
        appendSourcesToPersistentQueue([source])
    }

    private func acceptDrop(_ urls: [URL], _ location: CGPoint) -> Bool {
        let sources = urls.compactMap { ConversionSource.infer(from: $0) }
        guard !sources.isEmpty else {
            return false
        }
        appendSourcesToPersistentQueue(sources)
        return true
    }

    private var activeQueueItem: ConversionQueueItem? {
        viewModel.queueItems.first { item in
            if case .processing = item.status { return true }
            return false
        }
    }

    private var queuePosition: (current: Int, total: Int)? {
        guard let activeQueueItem,
              let index = viewModel.queueItems.firstIndex(where: { $0.id == activeQueueItem.id })
        else {
            return nil
        }
        return (index + 1, viewModel.queueItems.count)
    }

    private func appendDraftsToPersistentQueue(
        _ drafts: [ConversionDraft],
        conflicts: [RouteQualityConflict?] = [],
        clearConfiguredSource: Bool
    ) {
        Task { @MainActor in
            do {
                let result = try await viewModel.appendPersistentQueueDrafts(drafts, conflicts: conflicts)
                queueAdmissionNoticeMessage = queueAdmissionMessage(for: result)
                if clearConfiguredSource, result.addedCount > 0 {
                    viewModel.clearSource()
                }
            } catch {
                persistentQueueErrorMessage = error.localizedDescription
            }
        }
    }

    private func addConfiguredDraftsToPersistentQueue(_ drafts: [ConversionDraft]) {
        appendDraftsToPersistentQueue(drafts, clearConfiguredSource: true)
        isShowingSourceConfiguration = false
    }

    private func startConfiguredDrafts(_ drafts: [ConversionDraft]) {
        Task { @MainActor in
            do {
                let result = try await viewModel.appendPersistentQueueDrafts(drafts)
                queueAdmissionNoticeMessage = queueAdmissionMessage(for: result)
                guard result.addedCount > 0 else {
                    return
                }
                if viewModel.offPeakSchedule != nil {
                    try await viewModel.cancelOffPeakSchedule()
                }
                isShowingSourceConfiguration = false
                finishSourceConfiguration()
                let outcome = await viewModel.startPersistentQueue()
                if case let .rejected(rejection) = outcome {
                    persistentQueueErrorMessage = persistentQueueCommandMessage(for: rejection)
                }
            } catch {
                persistentQueueErrorMessage = error.localizedDescription
            }
        }
    }

    private func queueConfiguredConflictForReview(
        profileID: String,
        options editedOptions: ConversionOptions,
        conflict: RouteQualityConflict
    ) {
        let profile = profileStore.profile(withID: profileID)
        let drafts = makeConversionDrafts(options: editedOptions, profile: profile)
        appendDraftsToPersistentQueue(
            drafts,
            conflicts: Array(repeating: conflict, count: drafts.count),
            clearConfiguredSource: true
        )
        isShowingSourceConfigurationEditor = false
        isShowingSourceConfiguration = false
    }

    private func applyConfiguredOptions(profileID: String, options editedOptions: ConversionOptions) {
        selectedProfileID = profileID
        options = editedOptions
    }

    private func finishSourceConfiguration() {
        isShowingSourceConfigurationEditor = false
        guard configuredSource != nil else { return }
        configuredSource = nil
        if !viewModel.hasActiveWork {
            viewModel.clearSource()
        }
    }

    private func movePersistentQueueItem(_ itemID: UUID, by offset: Int) {
        let waitingItems = viewModel.persistentQueueItems.filter(\.canMove)
        guard let index = waitingItems.firstIndex(where: { $0.id == itemID }) else { return }
        let targetIndex = index + offset
        guard waitingItems.indices.contains(targetIndex) else { return }
        let targetID = waitingItems[targetIndex].id
        Task { @MainActor in
            do {
                if offset < 0 {
                    try await viewModel.movePersistentQueueItem(itemID, before: targetID)
                } else {
                    try await viewModel.movePersistentQueueItem(itemID, after: targetID)
                }
            } catch {
                persistentQueueErrorMessage = error.localizedDescription
            }
        }
    }

    private func movePersistentQueueItem(
        _ itemID: UUID,
        relativeTo targetID: UUID,
        placement: PersistentQueueMovePlacement
    ) {
        Task { @MainActor in
            do {
                switch placement {
                case .before:
                    try await viewModel.movePersistentQueueItem(itemID, before: targetID)
                case .after:
                    try await viewModel.movePersistentQueueItem(itemID, after: targetID)
                }
            } catch {
                persistentQueueErrorMessage = error.localizedDescription
            }
        }
    }

    private func movePersistentQueueItemNext(_ itemID: UUID) {
        Task { @MainActor in
            do {
                try await viewModel.movePersistentQueueItemNext(itemID)
            } catch {
                persistentQueueErrorMessage = error.localizedDescription
            }
        }
    }

    private func removePersistentQueueItem(_ itemID: UUID) {
        guard viewModel.persistentQueueItems.first(where: { $0.id == itemID })?.canRemove == true else {
            return
        }
        Task { @MainActor in
            guard viewModel.persistentQueueItems.first(where: { $0.id == itemID })?.canRemove == true else {
                return
            }
            do {
                persistentQueueRemovalToken = try await viewModel.removePersistentQueueItems([itemID])
            } catch {
                persistentQueueErrorMessage = error.localizedDescription
            }
        }
    }

    private func clearCompletedPersistentQueueItems() {
        Task { @MainActor in
            do {
                persistentQueueRemovalToken = try await viewModel.clearCompletedPersistentQueueItems()
            } catch {
                persistentQueueErrorMessage = error.localizedDescription
            }
        }
    }

    private func undoPersistentQueueRemoval() {
        guard let token = persistentQueueRemovalToken else { return }
        Task { @MainActor in
            guard viewModel.isPersistentQueueRemovalTokenValid(token) else {
                persistentQueueRemovalToken = nil
                return
            }
            do {
                try await viewModel.restorePersistentQueueItems(token)
                persistentQueueRemovalToken = nil
            } catch {
                persistentQueueRemovalToken = nil
                persistentQueueErrorMessage = error.localizedDescription
            }
        }
    }

    private func startPersistentQueue() {
        Task { @MainActor in
            guard persistentQueueCommandState.canStart else {
                return
            }
            guard !previewViewModel.hasActiveWorker else {
                persistentQueueErrorMessage = "Wait for the preview to finish before starting the queue."
                return
            }
            if viewModel.offPeakSchedule != nil {
                do {
                    try await viewModel.cancelOffPeakSchedule()
                } catch {
                    persistentQueueErrorMessage = error.localizedDescription
                    return
                }
            }
            let outcome = await viewModel.startPersistentQueue()
            if case let .rejected(rejection) = outcome {
                persistentQueueErrorMessage = persistentQueueCommandMessage(for: rejection)
            }
        }
    }

    private func presentNewOffPeakSchedule() {
        let start = Date().addingTimeInterval(15 * 60)
        offPeakScheduleStartAt = start
        offPeakScheduleEndAt = start.addingTimeInterval(8 * 60 * 60)
        offPeakScheduleEditorError = nil
        isEditingOffPeakSchedule = false
        isShowingOffPeakSchedule = true
    }

    private func presentExistingOffPeakSchedule() {
        guard let schedule = viewModel.offPeakSchedule else { return }
        offPeakScheduleStartAt = schedule.startAt
        offPeakScheduleEndAt = schedule.endAt
        offPeakScheduleEditorError = nil
        isEditingOffPeakSchedule = true
        isShowingOffPeakSchedule = true
    }

    private func saveOffPeakSchedule() {
        Task { @MainActor in
            do {
                try await viewModel.saveOffPeakSchedule(
                    startAt: offPeakScheduleStartAt,
                    endAt: offPeakScheduleEndAt
                )
                isShowingOffPeakSchedule = false
            } catch {
                offPeakScheduleEditorError = error.localizedDescription
            }
        }
    }

    private func cancelOffPeakSchedule() {
        Task { @MainActor in
            do {
                try await viewModel.cancelOffPeakSchedule()
            } catch {
                persistentQueueErrorMessage = error.localizedDescription
            }
        }
    }

    private func clearOffPeakScheduleOutcome() {
        Task { @MainActor in
            do {
                try await viewModel.clearOffPeakScheduleOutcome()
            } catch {
                persistentQueueErrorMessage = error.localizedDescription
            }
        }
    }

    private func evaluateOffPeakSchedule(appLaunched: Bool = false) {
        Task { @MainActor in
            guard !previewViewModel.hasActiveWorker else { return }
            _ = await viewModel.evaluateOffPeakSchedule(appLaunched: appLaunched)
        }
    }

    private func pausePersistentQueueAfterCurrent() {
        guard persistentQueueCommandState.canPauseAfterCurrent else {
            return
        }
        if case let .rejected(rejection) = viewModel.pausePersistentQueueAfterCurrent() {
            persistentQueueErrorMessage = persistentQueueCommandMessage(for: rejection)
        }
    }

    private func stopCurrentPersistentQueueItem() {
        guard persistentQueueCommandState.canStopCurrent else {
            return
        }
        if case let .rejected(rejection) = viewModel.stopCurrentPersistentQueueItem() {
            persistentQueueErrorMessage = persistentQueueCommandMessage(for: rejection)
        }
    }

    private func persistentQueueCommandMessage(for rejection: PersistentQueueCommandRejection) -> String {
        switch rejection {
        case .noEligibleItems:
            "No queued videos are currently ready to start."
        case .unresolvedChoices:
            "Resolve every queued video that needs a choice before starting the queue."
        case .noActiveItem:
            "No queue video is currently running."
        case .queueIsNotRunning:
            "Start or resume the queue before using this control."
        case .otherWorkIsActive:
            "Wait for the current conversion to finish before starting the queue."
        }
    }

    private func retryPersistentQueueItem(
        _ item: PersistentQueueItem,
        recoveryChoice: WorkerRecoveryChoice?
    ) {
        if viewModel.persistentQueueRunState == .pauseAfterCurrent, viewModel.hasActiveWorker {
            persistentQueueErrorMessage = "Wait for the current video to finish and the queue to pause before restarting this item."
            return
        }
        Task { @MainActor in
            if !(await viewModel.adoptPersistentQueueItem(item.id, recoveryChoice: recoveryChoice)) {
                persistentQueueErrorMessage = "This queued video could not be restarted. Review its source and recovery choice."
            }
        }
    }

    private func updatePersistentQueueItem(
        _ item: PersistentQueueItem,
        profileID: String,
        options editedOptions: ConversionOptions,
        routeQualityConflict: RouteQualityConflict? = nil
    ) {
        let profile = profileStore.profile(withID: profileID)
        let draft = ConversionDraft(
            source: item.draft.source,
            sourceDetails: item.draft.sourceDetails,
            profile: profile,
            destinationURL: item.draft.destinationURL,
            options: editedOptions,
            selectedTitle: item.draft.selectedTitle
        )
        Task { @MainActor in
            do {
                try await viewModel.updatePersistentQueueItem(
                    item.id,
                    draft: draft,
                    routeQualityConflict: routeQualityConflict
                )
            } catch {
                persistentQueueErrorMessage = error.localizedDescription
            }
        }
    }

    private func changePersistentQueueDestination(_ item: PersistentQueueItem) {
        guard let destination = DestinationPicker.chooseDestination(startingAt: item.draft.destinationURL) else {
            return
        }
        let draft = ConversionDraft(
            source: item.draft.source,
            sourceDetails: item.draft.sourceDetails,
            profile: item.draft.profile,
            destinationURL: destination,
            options: item.draft.options,
            selectedTitle: item.draft.selectedTitle
        )
        Task { @MainActor in
            do {
                try await viewModel.updatePersistentQueueItem(item.id, draft: draft)
            } catch {
                persistentQueueErrorMessage = error.localizedDescription
            }
        }
    }

    private func queueAdmissionMessage(for result: PersistentQueueAppendResult) -> String? {
        guard result.duplicateCount > 0 else { return nil }

        let skippedMessage: String
        if result.duplicateCount == 1 {
            let name = result.duplicateDisplayNames[0]
            skippedMessage = "\(name) is already in the queue, so it wasn’t added again."
        } else {
            skippedMessage = "\(result.duplicateCount) movies are already in the queue, so they weren’t added again."
        }

        guard result.addedCount > 0 else { return skippedMessage }
        let noun = result.addedCount == 1 ? "movie was" : "movies were"
        return "\(skippedMessage) \(result.addedCount) other \(noun) added."
    }

}

private struct SourceConfigurationSheet: View {
    @Environment(\.dismiss) private var dismiss

    let source: ConversionSource
    let inspection: SourceInspection?
    let inspectionFailureMessage: String?
    let inspectionWarningMessage: String?
    let profile: EncodingProfile
    let destinationURL: URL
    @Binding var titleSelection: DiscTitleSelection
    let drafts: [ConversionDraft]
    @ObservedObject var previewViewModel: PreviewViewModel
    @Binding var queueAdmissionNoticeMessage: String?
    @Binding var persistentQueueErrorMessage: String?
    let openSettings: () -> Void
    let addToQueue: ([ConversionDraft]) -> Void
    let startQueue: ([ConversionDraft]) -> Void

    @State private var isShowingPreview = false
    @State private var outputLength = OutputLength.threeMinutes
    @State private var samplePosition = SamplePosition.middle

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    header
                    if let inspection {
                        if let inspectionWarningMessage {
                            Label(inspectionWarningMessage, systemImage: "exclamationmark.triangle.fill")
                                .foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        titleSelectionControls(inspection)
                        setupSummary
                    } else if let inspectionFailureMessage {
                        ContentUnavailableView(
                            "Source Could Not Be Read",
                            systemImage: "exclamationmark.triangle",
                            description: Text(inspectionFailureMessage)
                        )
                        .frame(maxWidth: .infinity, minHeight: 160)
                    } else {
                        ProgressView("Reading source details…")
                            .frame(maxWidth: .infinity, minHeight: 160)
                    }
                }
                .padding(24)
            }

            Divider()
            HStack(spacing: 10) {
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button("Edit Settings…", action: openSettings)
                    .disabled(inspection == nil)
                Button("Preview…") { isShowingPreview = true }
                    .disabled(drafts.count != 1)
                Button("Add to Queue") { addToQueue(drafts) }
                    .disabled(drafts.isEmpty)
                Button("Start Queue") { startQueue(drafts) }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(drafts.isEmpty)
            }
            .padding(16)
        }
        .frame(minWidth: 680, idealWidth: 760, minHeight: 440, idealHeight: 560)
        .sheet(isPresented: $isShowingPreview) {
            if let draft = drafts.first {
                PreviewSheet(
                    viewModel: previewViewModel,
                    conversionDraft: draft,
                    outputLength: $outputLength,
                    samplePosition: $samplePosition,
                    startFullConversion: { preview in
                        isShowingPreview = false
                        startQueue([preview.conversion])
                    }
                )
            }
        }
        .accessibilityIdentifier("source-configuration-sheet")
        .alert(feedbackTitle, isPresented: feedbackIsPresented) {
            Button("OK") {
                queueAdmissionNoticeMessage = nil
                persistentQueueErrorMessage = nil
            }
        } message: {
            Text(feedbackMessage)
        }
    }

    private var feedbackIsPresented: Binding<Bool> {
        Binding(
            get: { queueAdmissionNoticeMessage != nil || persistentQueueErrorMessage != nil },
            set: { isPresented in
                if !isPresented {
                    queueAdmissionNoticeMessage = nil
                    persistentQueueErrorMessage = nil
                }
            }
        )
    }

    private var feedbackTitle: String {
        persistentQueueErrorMessage == nil ? "Already in Queue" : "Queue Action Failed"
    }

    private var feedbackMessage: String {
        persistentQueueErrorMessage ?? queueAdmissionNoticeMessage ?? "The queue action could not be completed."
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text("Configure Source")
                .font(.title2.weight(.semibold))
            Text(source.displayName)
                .font(.headline)
            Text("Configure titles, settings, and previews here. Starting always saves the selected work to the persistent queue first.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder
    private func titleSelectionControls(_ inspection: SourceInspection) -> some View {
        if source.kind.isDiscWorkflow, inspection.titles.count > 1 {
            GroupBox("Titles") {
                VStack(alignment: .leading, spacing: 10) {
                    Picker("Convert", selection: titleMode) {
                        Text("Main Movie").tag("main")
                        Text("All Videos").tag("all")
                        Text("Custom Selection").tag("custom")
                    }
                    .pickerStyle(.segmented)
                    if case let .custom(identifiers) = titleSelection {
                        ForEach(inspection.titles) { title in
                            Toggle(
                                "\(title.name) · \(title.formattedDuration)",
                                isOn: titleBinding(title.id, selectedIDs: identifiers)
                            )
                        }
                    }
                    Text("\(drafts.count) video\(drafts.count == 1 ? "" : "s") will be saved to the queue.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(.top, 4)
            }
        }
    }

    private var titleMode: Binding<String> {
        Binding(
            get: {
                if titleSelection.isMain { return "main" }
                if titleSelection.isAll { return "all" }
                return "custom"
            },
            set: { mode in
                switch mode {
                case "main":
                    titleSelection = .main
                case "all":
                    titleSelection = .all
                default:
                    titleSelection = .custom(Set(inspection?.titles.map(\.id) ?? []))
                }
            }
        )
    }

    private func titleBinding(_ titleID: String, selectedIDs: Set<String>) -> Binding<Bool> {
        Binding(
            get: { selectedIDs.contains(titleID) },
            set: { isSelected in
                var updatedIDs = selectedIDs
                if isSelected {
                    updatedIDs.insert(titleID)
                } else {
                    updatedIDs.remove(titleID)
                }
                titleSelection = .custom(updatedIDs)
            }
        )
    }

    private var setupSummary: some View {
        GroupBox("Conversion Setup") {
            Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 10) {
                GridRow {
                    Text("Profile").foregroundStyle(.secondary)
                    Text(profile.name)
                }
                GridRow {
                    Text("Destination").foregroundStyle(.secondary)
                    Text(destinationURL.path)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                GridRow {
                    Text("Output").foregroundStyle(.secondary)
                    Text(drafts.first?.options.videoRoutePlan.qualityTitle ?? "Waiting for source details")
                }
            }
            .font(.callout)
            .padding(.top, 4)
        }
    }
}

struct ActivityDrawer: View {
    let state: WorkerLifecycleState
    let observabilityStatus: LiveObservabilityStatus
    let showTechnicalDetails: Bool

    private var activityEntries: [WorkerActivityEntry] {
        Array(state.activityHistory.reversed())
    }

    private var historySummary: String {
        let count = activityEntries.count
        return count == 1 ? "1 entry · newest first" : "\(count) entries · newest first"
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Label("Activity History", systemImage: "clock.arrow.circlepath")
                    .font(.caption.weight(.semibold))
                Spacer()
                if !activityEntries.isEmpty {
                    Text(historySummary)
                        .font(.caption2)
                }
            }
            .foregroundStyle(.secondary)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)

            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 9) {
                    if showTechnicalDetails, observabilityStatus.hasDetails {
                        LiveObservabilityStatusView(status: observabilityStatus)
                        Divider()
                    }

                    if activityEntries.isEmpty {
                        Text("Activity will appear here when source analysis or conversion begins.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .topLeading)
                    } else {
                        ForEach(activityEntries) { entry in
                            ActivityEntryRow(entry: entry)
                        }
                    }
                }
                .padding(12)
            }
        }
        .frame(height: 190)
        .background(Color(nsColor: .textBackgroundColor))
        .accessibilityLabel("Activity details")
    }
}

private struct ActivityEntryRow: View {
    let entry: WorkerActivityEntry

    private var symbolName: String {
        switch entry.severity {
        case .information:
            "circle.fill"
        case .success:
            "checkmark.circle.fill"
        case .warning:
            "exclamationmark.triangle.fill"
        case .failure:
            "xmark.octagon.fill"
        }
    }

    private var symbolColor: Color {
        switch entry.severity {
        case .information:
            Color(nsColor: .secondaryLabelColor)
        case .success:
            .green
        case .warning:
            .orange
        case .failure:
            .red
        }
    }

    private var severityLabel: String {
        switch entry.severity {
        case .information:
            "Status"
        case .success:
            "Completed"
        case .warning:
            "Warning"
        case .failure:
            "Error"
        }
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: symbolName)
                .font(.caption2)
                .foregroundStyle(symbolColor)
                .accessibilityHidden(true)

            Text(entry.message)
                .font(.caption.monospaced())
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .topLeading)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(severityLabel): \(entry.message)")
    }
}

private struct LiveObservabilityStatusView: View {
    let status: LiveObservabilityStatus

    @State private var now = Date()
    private let timer = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    private var activityState: LiveObservabilityStatus.ActivityState {
        status.activityState(at: now)
    }

    private var processState: String? {
        status.processState?.rawValue.capitalized
    }

    private var lastOutput: String? {
        status.currentLastOutputAgeSeconds(at: now).map { "\($0)s ago" }
    }

    private var statusSummary: String {
        switch activityState {
        case .active:
            return "Recent tool output or heartbeat"
        case .toolQuietArtifactsActive:
            return "Tool output quiet; artifact growth continues"
        case .stalled:
            return "Waiting for tool output or artifact updates"
        }
    }

    private var artifactLabel: String {
        status.artifacts.count == 1 ? "Artifact" : "Artifacts"
    }

    private var artifactDescription: String? {
        let descriptions = status.artifacts.map(artifactDescription(_:))
        return descriptions.isEmpty ? nil : descriptions.joined(separator: "\n")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Live Tool Status")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)

            if activityState == .toolQuietArtifactsActive {
                Label("Artifact growth continues while the tool stays quiet", systemImage: "clock.badge.checkmark")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            } else if activityState == .stalled {
                Label("Still running; waiting for tool output or artifact updates", systemImage: "clock")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Text("This is a status check, not a failure.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            statusRow("Stage", value: status.stageID)
            statusRow("Tool", value: status.toolID)
            statusRow("Process", value: processState)
            statusRow("Status", value: statusSummary)
            statusRow("Last output", value: lastOutput)
            statusRow(artifactLabel, value: artifactDescription)
        }
        .font(.caption.monospaced())
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Live tool status")
        .accessibilityValue(statusSummary)
        .onReceive(timer) { now = $0 }
    }

    private func artifactDescription(_ artifact: LiveObservabilityStatus.ArtifactStatus) -> String {
        let identity = [friendlyArtifactRole(artifact.role), artifact.state]
            .compactMap { $0 }
            .joined(separator: " · ")
        let size = artifact.sizeBytes.map {
            ByteCountFormatter.string(fromByteCount: $0, countStyle: .file)
        }
        let growth = artifact.growthBytesPerSecond.map {
            $0 > 0
                ? "+\(ByteCountFormatter.string(fromByteCount: $0, countStyle: .file))/s"
                : "no growth"
        }
        let age = artifact.currentModificationAgeSeconds(at: now).map { "mtime \($0)s ago" }
        let values = [identity.isEmpty ? nil : identity, size, growth, age].compactMap { $0 }
        return values.joined(separator: " · ")
    }

    private func friendlyArtifactRole(_ role: String) -> String {
        switch role {
        case "left_eye_video_output":
            return "Left eye"
        case "right_eye_video_output":
            return "Right eye"
        case "stereo_video_output":
            return "Stereo video"
        default:
            return role
        }
    }

    @ViewBuilder
    private func statusRow(_ label: String, value: String?) -> some View {
        if let value {
            LabeledContent(label, value: value)
                .textSelection(.enabled)
        }
    }
}

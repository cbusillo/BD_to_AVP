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
    @State private var outputLength = OutputLength.oneMinute
    @State private var samplePosition = SamplePosition.beginning
    @State private var selectedTab = ConversionSetupTab.video
    @State private var insertedDiscs: [ConversionSource] = []
    @State private var isShowingActivity = false
    @State private var isDropTargeted = false
    @State private var isShowingSaveProfile = false
    @State private var isShowingSetupEditor = false
    @State private var newProfileName = ""
    @State private var profileErrorMessage: String?
    @State private var preserveEncodingOnNextProfileChange = false
    @State private var isShowingPreview = false
    @State private var pendingReviewedPreview: PreviewDraft?
    @State private var titleSelection = DiscTitleSelection.main
    @State private var sourceResetMessage: String?
    @State private var isShowingTitleChooser = false
    @State private var isShowingDiagnosticReport = false
    @State private var isRefreshingDiscs = false
    @StateObject private var routeQualityState: RouteQualityResolutionState
    @StateObject private var setupQueue = SetupQueueAdmission()

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
        _routeQualityState = StateObject(wrappedValue: RouteQualityResolutionState())
    }

    var body: some View {
        presentedContent
    }

    private var mainContent: some View {
        VStack(spacing: 0) {
            noticeContent

            HSplitView {
                sourceOrQueueColumn
                setupColumn
            }

            Divider()
            statusFooter

            activityContent
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
        .focusedSceneValue(\.conversionSourceSelectionAction, sourceSelectionAction)
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
                        Label("Open this 3D Blu-ray source", systemImage: "arrow.down.doc.fill")
                            .font(.title3.weight(.semibold))
                            .padding(14)
                            .background(.regularMaterial, in: Capsule())
                }
            }
        }
    }

    private var observedContent: some View {
        baseContent
        .onAppear(perform: refreshDiscs)
        .onReceive(NSWorkspace.shared.notificationCenter.publisher(for: NSWorkspace.didMountNotification)) { _ in
            refreshDiscs()
        }
        .onReceive(NSWorkspace.shared.notificationCenter.publisher(for: NSWorkspace.didUnmountNotification)) {
            notification in
            handleVolumeUnmount(notification)
        }
        .onChange(of: viewModel.hasActiveWorker) { _, isActive in
            if !isActive {
                refreshDiscs()
            }
        }
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
        .onChange(of: viewModel.persistentQueueItems) { _, items in
            setupQueue.synchronize(with: items)
        }
        .onChange(of: viewModel.setupQueueStartFailureItemIDs) { _, itemIDs in
            guard !itemIDs.isEmpty else { return }
            setupQueue.markStartFailed(itemIDs)
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
        .sheet(isPresented: $isShowingSaveProfile) {
            SaveProfileSheet(name: $newProfileName) {
                saveAsNewProfile()
            }
        }
        .sheet(isPresented: $isShowingSetupEditor) {
            SetupEditSheet(
                initialProfile: selectedProfile,
                initialOptions: options,
                fallbackPipelineDefaults: defaultJobOptions.profilePipelineDefaults,
                sourceKind: viewModel.source?.kind,
                profiles: profileStore.profiles,
                profileStore: profileStore,
                resolutionMemoryStore: resolutionMemoryStore,
                applyToConversion: applyEditedConversion,
                queueConflictForReview: heldConflictQueueAction
            )
        }
        .sheet(isPresented: $isShowingPreview, onDismiss: previewDidDismiss) {
            if let draft {
                PreviewSheet(
                    viewModel: previewViewModel,
                    conversionDraft: draft,
                    outputLength: $outputLength,
                    samplePosition: $samplePosition,
                    startFullConversion: { reviewedDraft in
                        pendingReviewedPreview = reviewedDraft
                        isShowingPreview = false
                    }
                )
            }
        }
        .sheet(isPresented: $isShowingTitleChooser) {
            if let inspection = viewModel.state.result, inspection.titles.count > 1 {
                TitleChooserSheet(
                    titles: inspection.titles,
                    selectedIDs: Set(selectedTitles.map(\.id))
                ) { selectedIDs in
                    titleSelection = .custom(selectedIDs)
                }
            }
        }
        .sheet(isPresented: $isShowingDiagnosticReport) {
            DiagnosticReportSheet(viewModel: diagnosticReportViewModel)
        }
        .alert(
            "Profile Could Not Be Saved",
            isPresented: Binding(
                get: { profileErrorMessage != nil },
                set: { if !$0 { profileErrorMessage = nil } }
            )
        ) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(profileErrorMessage ?? "The profile could not be saved.")
        }
    }

    @ViewBuilder
    private var sourceOrQueueColumn: some View {
        if setupQueue.hasItems, viewModel.state.phase != .decisionRequired {
            queueSidebar
        } else {
            sourceWorkspace
        }
    }

    private var sourceWorkspace: some View {
        SourceWorkspaceView(
            source: viewModel.source,
            state: viewModel.state,
            batchQueue: viewModel.batchQueue,
            isBatchRunning: viewModel.isBatchRunning,
            insertedDiscs: insertedDiscs,
            makeMKVAvailable: DiscSourceDetector.makeMKVAvailable,
            profile: selectedProfile,
            options: options,
            profileModified: profileModified,
            titleSelection: titleSelection,
            titleSelectionSummary: titleSelectionSummary,
            selectedVideoCount: selectedVideoCount,
            queueItems: visibleQueueItems,
            destinationURL: $destinationURL,
            plannedOutputURLs: plannedOutputURLs,
            storageEstimate: VideoStorageEstimate(drafts: conversionDrafts),
            refreshDiscs: refreshDiscs,
            useDisc: selectSource,
            openDiscImage: { chooseFile(.discImage) },
            openBluRayFolder: { chooseFolder(.bluRayFolder) },
            openSourceFolder: { chooseFolder(.sourceFolder) },
            openMKV: { chooseFile(.matroska) },
            importTransportStream: { chooseFile(.transportStream) },
            changeSource: chooseExistingSource,
            chooseDestination: chooseDestination,
            retryAnalysis: viewModel.restartInspection,
            diagnosticsActionTitle: diagnosticActionTitle,
            canShowDiagnostics: canShowDiagnosticAction,
            showDiagnostics: showDiagnosticReport,
            resolveRecoveryChoice: { choice in
                _ = viewModel.resolveRecoveryChoice(choice)
            },
            retryBatchItem: { itemID, choice in
                viewModel.retryBatchItem(itemID, recoveryChoice: choice)
            },
            selectMainTitle: { titleSelection = .main },
            selectAllTitles: { titleSelection = .all },
            chooseTitles: { isShowingTitleChooser = true }
        )
        .frame(minWidth: 300, idealWidth: 360, maxWidth: 430)
    }

    private var sourceSelectionAction: ConversionSourceSelectionAction? {
        guard canSelectSource else {
            return nil
        }
        return ConversionSourceSelectionAction(perform: chooseExistingSource)
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
                    Button("Use \(disc.displayName)") {
                        selectSource(disc)
                    }
                }
            }

            Button("Refresh Disc Drives", action: refreshDiscs)

            Divider()
            Button("Open Disc Image…") { chooseFile(.discImage) }
            Button("Open Blu-ray Folder…") { chooseFolder(.bluRayFolder) }
            Button("Add Folder of Movies…") { chooseFolder(.sourceFolder) }
            Button("Open 3D MKV…") { chooseFile(.matroska) }

            Divider()
            Button("Import MTS or M2TS…") { chooseFile(.transportStream) }

            if viewModel.source != nil {
                Divider()
                Button("Remove Source", role: .destructive) {
                    viewModel.clearSource()
                }
            }
        } label: {
            Label(viewModel.source == nil ? "Choose Source" : "Change Source", systemImage: "opticaldiscdrive")
        }
        .help("Choose a physical disc, disc image, Blu-ray folder, source folder, MKV, or transport stream")
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
                if let sourceResetMessage {
                    Label(sourceResetMessage, systemImage: "arrow.uturn.backward.circle")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .help(sourceResetMessage)
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

    private var setupColumn: some View {
        ConversionSetupView(
            selectedProfileID: $selectedProfileID,
            selectedTab: $selectedTab,
            options: $options,
            profiles: profileStore.profiles,
            selectedProfile: selectedProfile,
            profileModified: profileModified,
            isLocked: viewModel.hasActiveWork || previewViewModel.hasActiveWorker || viewModel.state.phase == .decisionRequired,
            sourceKind: viewModel.source?.kind,
            routeQualityState: routeQualityState,
            resolutionMemoryStore: resolutionMemoryStore,
            isReady: true,
            openEditor: beginSetupEditor,
            saveSelectedProfile: saveSelectedProfile,
            saveAsNewProfile: beginSaveAsNewProfile,
            resetProfile: resetProfile,
            sourceName: viewModel.source?.displayName,
            destinationName: destinationURL.path,
            changeDestination: chooseDestination,
            estimate: "Finished movie size and time are estimated from this source.",
            preview: { isShowingPreview = true },
            addToQueue: addCurrentDraftsToQueue,
            start: startReadyConversion,
            canPreview: previewCanStart,
            canAddToQueue: !conversionDrafts.isEmpty && !viewModel.hasActiveWork,
            canStart: setupQueue.hasItems
                ? setupQueue.canStart && !viewModel.hasActiveWork
                : conversionCanStart,
            showsStartAction: !setupQueue.hasItems
        )
        .frame(minWidth: 500, idealWidth: 680)
    }

    @ViewBuilder
    private var queueSidebar: some View {
        if setupQueue.hasItems {
            SetupQueueAdmissionView(
                admission: setupQueue,
                memoryStore: resolutionMemoryStore,
                canStart: setupQueue.canStart
                    && !viewModel.hasActiveWork
                    && !previewViewModel.hasActiveWorker,
                start: startAdmittedQueue,
                clear: setupQueue.removeAll
            )
        }
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

    private var profileModified: Bool {
        options.encoding != selectedProfile.options
            || options.job.profilePipelineDefaults != profilePipelineDefaults(for: selectedProfile)
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

    private var heldConflictQueueAction: ((String, RouteQualityConflict) -> Void)? {
        guard viewModel.source != nil, viewModel.state.result != nil else { return nil }
        return { profileID, conflict in
            let profile = profileStore.profile(withID: profileID)
            let drafts = makeConversionDrafts(options: conflict.proposedOptions, profile: profile)
            guard !drafts.isEmpty else { return }
            setupQueue.add(
                drafts: drafts,
                conflicts: Array(repeating: conflict, count: drafts.count)
            )
        }
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

    private var visibleQueueItems: [ConversionQueueItem] {
        guard viewModel.queueItems.isEmpty, conversionDrafts.count > 1 else {
            return viewModel.queueItems
        }
        return conversionDrafts.map {
            ConversionQueueItem(id: ConversionQueueItem.stablePreviewID(for: $0), draft: $0)
        }
    }

    private var titleSelectionSummary: String {
        switch titleSelection {
        case .main:
            return "Main Movie"
        case .all:
            return "All \(selectedTitles.count) Videos"
        case .custom:
            if selectedTitles.count == 1 {
                return selectedTitles[0].name
            }
            return "\(selectedTitles.count) Selected Videos"
        }
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
        if let sourceResetMessage {
            components.append(sourceResetMessage)
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

    private var previewCanStart: Bool {
        guard conversionCanStart else {
            return false
        }
        guard selectedVideoCount == 1 else {
            return false
        }
        guard requestedVideoRoute.allowsFinalizedPreview else {
            return false
        }
        switch viewModel.source?.kind {
        case .discImage, .bluRayFolder, .matroska, .transportStream:
            return true
        case .physicalDisc, .sourceFolder, .none:
            return false
        }
    }

    private var previewUnavailableReason: String {
        if previewCanStart {
            return "Create a representative preview with the current video and quality choices."
        }
        if selectedVideoCount > 1 {
            return "Choose one 3D video to create a preview."
        }
        if !requestedVideoRoute.allowsFinalizedPreview {
            return "A late-stage restart reuses an existing video. Choose an earlier start stage to create a representative preview."
        }
        switch viewModel.source?.kind {
        case .physicalDisc:
            return "Preview supports MKV, MTS, M2TS, ISO, and Blu-ray-folder sources."
        default:
            return conversionUnavailableReason
        }
    }

    private var requestedVideoRoute: VideoRoutePlan {
        VideoRoutePlan(options: options)
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

    private func beginSetupEditor() {
        guard !viewModel.hasActiveWork,
              !previewViewModel.hasActiveWorker,
              viewModel.state.phase != .decisionRequired
        else {
            return
        }
        isShowingSetupEditor = true
    }

    private func applyEditedConversion(_ profileID: String, _ editedOptions: ConversionOptions) {
        routeQualityState.reset()
        preserveEncodingOnNextProfileChange = true
        selectedProfileID = profileID
        options = editedOptions
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

    private func saveSelectedProfile() {
        guard selectedProfile.isCustom else {
            return
        }
        do {
            try profileStore.updateProfile(
                selectedProfile.id,
                name: selectedProfile.name,
                options: options.encoding,
                pipelineDefaults: options.job.profilePipelineDefaults
            )
        } catch {
            profileErrorMessage = error.localizedDescription
        }
    }

    private func beginSaveAsNewProfile() {
        newProfileName = profileStore.suggestedDuplicateName(for: selectedProfile.name)
        isShowingSaveProfile = true
    }

    private func saveAsNewProfile() {
        do {
            let identifier = try profileStore.createProfile(
                name: newProfileName,
                options: options.encoding,
                pipelineDefaults: options.job.profilePipelineDefaults
            )
            selectedProfileID = identifier
            isShowingSaveProfile = false
        } catch {
            profileErrorMessage = error.localizedDescription
        }
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

    private func selectSource(_ source: ConversionSource) {
        guard canSelectSource else {
            return
        }
        routeQualityState.reset()
        sourceResetMessage = nil
        if isNewSource(source) {
            sourceResetMessage = options.resetSourceScopedState(
                for: source.kind,
                titleSelection: &titleSelection,
                recoveryDecisionPresent: viewModel.state.recoveryDecision != nil
                    || viewModel.state.phase == .decisionRequired
            ).message
        }
        if source.kind == .physicalDisc {
            options.job.removeOriginalAfterSuccess = false
        }
        viewModel.selectSource(source)
    }

    private func isNewSource(_ source: ConversionSource) -> Bool {
        guard let currentSource = viewModel.source else {
            return true
        }
        return currentSource.kind != source.kind
            || currentSource.url.standardizedFileURL != source.url.standardizedFileURL
            || currentSource.workerSourcePath != source.workerSourcePath
    }

    private func chooseExistingSource() {
        guard canSelectSource,
              let sourceURL = SourcePicker.chooseExistingSource(),
              let source = ConversionSource.infer(from: sourceURL)
        else {
            return
        }
        selectSource(source)
    }

    private func chooseFile(_ kind: ConversionSourceKind) {
        guard canSelectSource, let source = SourcePicker.chooseFile(kind: kind) else {
            return
        }
        selectSource(source)
    }

    private func chooseFolder(_ kind: ConversionSourceKind) {
        guard canSelectSource, let source = SourcePicker.chooseFolder(kind: kind) else {
            return
        }
        selectSource(source)
    }

    private func chooseDestination() {
        if let destination = DestinationPicker.chooseDestination(startingAt: destinationURL) {
            destinationURL = destination
        }
    }

    private func acceptDrop(_ urls: [URL], _ location: CGPoint) -> Bool {
        guard canSelectSource,
              let url = urls.first,
              let source = ConversionSource.infer(from: url)
        else {
            return false
        }
        selectSource(source)
        return true
    }

    private func previewDidDismiss() {
        previewViewModel.discardPreview()
        guard let reviewedPreview = pendingReviewedPreview else {
            return
        }
        pendingReviewedPreview = nil
        viewModel.startConversion(
            draft: reviewedPreview.conversion,
            jobID: reviewedPreview.parentJobID
        )
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

    private func startSelectedConversions() {
        guard !conversionDrafts.isEmpty else {
            return
        }
        if conversionDrafts.count == 1 {
            viewModel.startConversion(draft: conversionDrafts[0])
        } else {
            viewModel.startConversionQueue(drafts: conversionDrafts)
        }
    }

    private func addCurrentDraftsToQueue() {
        guard !conversionDrafts.isEmpty else { return }
        setupQueue.add(drafts: conversionDrafts)
    }

    private func startAdmittedQueue() {
        guard setupQueue.canStart,
              !viewModel.hasActiveWork,
              !previewViewModel.hasActiveWorker,
              viewModel.startConversionQueue(admissionItems: setupQueue.items)
        else { return }
        setupQueue.markAllRunning()
    }

    private func startReadyConversion() {
        if setupQueue.hasItems {
            startAdmittedQueue()
        } else if isBatchSource {
            viewModel.startBatchConversion(
                profile: selectedProfile,
                destinationURL: destinationURL,
                options: options
            )
        } else {
            startSelectedConversions()
        }
    }
}

private struct SaveProfileSheet: View {
    @Environment(\.dismiss) private var dismiss
    @Binding var name: String
    let save: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Save New Profile")
                    .font(.title2.weight(.semibold))
                Text(ProfilePersistenceCopy.summary)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            TextField("Profile name", text: $name)
                .textFieldStyle(.roundedBorder)
                .accessibilityIdentifier("save-profile-name-field")

            HStack {
                Spacer()
                Button("Cancel") {
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)
                .accessibilityIdentifier("save-profile-cancel")

                Button("Save", action: save)
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityIdentifier("save-profile-confirm")
            }
        }
        .padding(24)
        .frame(width: 440)
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

import AppKit
import SwiftUI

struct ContentView: View {
    @ObservedObject var viewModel: ConversionViewModel
    @ObservedObject var previewViewModel: PreviewViewModel
    @ObservedObject var settings: AppSettings
    @ObservedObject var profileStore: ProfileStore
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
    @State private var newProfileName = ""
    @State private var profileErrorMessage: String?
    @State private var preserveEncodingOnNextProfileChange = false
    @State private var isShowingPreview = false
    @State private var pendingReviewedPreview: PreviewDraft?
    @State private var titleSelection = DiscTitleSelection.main
    @State private var isShowingTitleChooser = false

    init(
        viewModel: ConversionViewModel,
        previewViewModel: PreviewViewModel,
        settings: AppSettings,
        profileStore: ProfileStore,
        capabilities: AppCapabilities
    ) {
        _viewModel = ObservedObject(wrappedValue: viewModel)
        _previewViewModel = ObservedObject(wrappedValue: previewViewModel)
        _settings = ObservedObject(wrappedValue: settings)
        _profileStore = ObservedObject(wrappedValue: profileStore)
        self.capabilities = capabilities

        let profile = profileStore.profile(withID: settings.selectedProfileID)
        let initialOptions = ConversionOptions(
            encoding: profile.options,
            job: Self.jobOptions(from: settings)
        )

        _selectedProfileID = State(initialValue: profile.id)
        _options = State(initialValue: initialOptions)
        _destinationURL = State(initialValue: settings.destinationURL)
    }

    var body: some View {
        VStack(spacing: 0) {
            HSplitView {
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
                .frame(minWidth: 350, idealWidth: 390, maxWidth: 450)

                ConversionSetupView(
                    selectedProfileID: $selectedProfileID,
                    selectedTab: $selectedTab,
                    options: $options,
                    profiles: profileStore.profiles,
                    selectedProfile: selectedProfile,
                    profileModified: profileModified,
                    isLocked: viewModel.hasActiveWork
                        || previewViewModel.hasActiveWorker
                        || viewModel.state.phase == .decisionRequired,
                    sourceKind: viewModel.source?.kind,
                    saveSelectedProfile: saveSelectedProfile,
                    saveAsNewProfile: beginSaveAsNewProfile,
                    resetProfile: resetProfile
                )
                .frame(minWidth: 570, idealWidth: 680)
            }

            Divider()
            statusFooter

            if isShowingActivity {
                Divider()
                ActivityDrawer(
                    state: viewModel.state,
                    diagnosticLog: viewModel.diagnosticLog,
                    showTechnicalDetails: settings.showTechnicalDetails
                )
                .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
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
                options.job = newValue
            }
        }
        .onChange(of: viewModel.state.conversionResult) { _, result in
            guard viewModel.batchQueue == nil,
                  let result,
                  viewModel.queueItems.count <= 1
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
                  options.encoding == previousProfile.options
            else {
                return
            }
            options.encoding = currentProfile.options
        }
        .sheet(isPresented: $isShowingSaveProfile) {
            SaveProfileSheet(name: $newProfileName) {
                saveAsNewProfile()
            }
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
            Button("Open Source Folder…") { chooseFolder(.sourceFolder) }
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
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel(statusAccessibilityLabel)

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
            } else if isBatchSource {
                Button("Start Batch Conversion") {
                    viewModel.startBatchConversion(
                        profile: selectedProfile,
                        destinationURL: destinationURL,
                        options: options
                    )
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut("p", modifiers: .command)
                .disabled(!conversionCanStart)
                .help(conversionCanStart ? "Convert the queued sources sequentially." : conversionUnavailableReason)
            } else if viewModel.source == nil || !conversionCanStart {
                Button(startButtonTitle) {}
                    .buttonStyle(.bordered)
                    .disabled(true)
                    .help(viewModel.source == nil ? "Choose a source before processing." : conversionUnavailableReason)
            } else {
                Button("Preview…") {
                    isShowingPreview = true
                }
                .buttonStyle(.bordered)
                .disabled(!previewCanStart)
                .help(previewUnavailableReason)

                Button(startButtonTitle) {
                    startSelectedConversions()
                }
                .buttonStyle(.borderedProminent)
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

    private var profileModified: Bool {
        options.encoding != selectedProfile.options
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
                    profile: selectedProfile,
                    destinationURL: destinationURL,
                    options: options,
                    selectedTitle: title
                )
            }
        }
        return [
            ConversionDraft(
                source: source,
                sourceDetails: inspection,
                profile: selectedProfile,
                destinationURL: destinationURL,
                options: options
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

    private var visibleQueueItems: [ConversionQueueItem] {
        guard viewModel.queueItems.isEmpty, conversionDrafts.count > 1 else {
            return viewModel.queueItems
        }
        return conversionDrafts.map { ConversionQueueItem(draft: $0) }
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

    private var startButtonTitle: String {
        selectedVideoCount > 1 ? "Convert \(selectedVideoCount) Videos" : "Start Full Conversion"
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
        switch viewModel.source?.kind {
        case .discImage, .matroska, .transportStream:
            return true
        case .physicalDisc, .bluRayFolder, .sourceFolder, .none:
            return false
        }
    }

    private var previewUnavailableReason: String {
        if previewCanStart {
            return "Create a representative preview with the current resolved settings."
        }
        if selectedVideoCount > 1 {
            return "Choose one 3D video to create a preview."
        }
        switch viewModel.source?.kind {
        case .physicalDisc, .bluRayFolder:
            return "The first preview slice supports MKV, MTS, M2TS, and ISO sources."
        default:
            return conversionUnavailableReason
        }
    }

    private var canSelectSource: Bool {
        viewModel.canSelectSource && !previewViewModel.hasActiveWorker
    }

    private var isBatchSource: Bool {
        viewModel.source?.kind == .sourceFolder
    }

    private var conversionUnavailableReason: String {
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

    private func resetProfile() {
        options.encoding = selectedProfile.options
    }

    private static func jobOptions(from settings: AppSettings) -> JobOptions {
        JobOptions(
            keepStageFiles: settings.keepIntermediateFiles,
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
                options: options.encoding
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
                options: options.encoding
            )
            selectedProfileID = identifier
            isShowingSaveProfile = false
        } catch {
            profileErrorMessage = error.localizedDescription
        }
    }

    private func refreshDiscs() {
        let refreshedDiscs = DiscSourceDetector.insertedDiscs()
        insertedDiscs = refreshedDiscs
        guard !viewModel.hasActiveWork,
              let selectedSource = viewModel.source,
              selectedSource.kind == .physicalDisc,
              !refreshedDiscs.contains(where: { $0.workerSourcePath == selectedSource.workerSourcePath })
        else {
            return
        }
        viewModel.clearSource()
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
        if source.kind == .physicalDisc {
            options.job.removeOriginalAfterSuccess = false
        }
        titleSelection = .main
        viewModel.selectSource(source)
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
                Text("Video, audio, subtitle, and stereo-correction settings will be saved. Job and safety choices stay with the current conversion.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            TextField("Profile name", text: $name)
                .textFieldStyle(.roundedBorder)

            HStack {
                Spacer()
                Button("Cancel") {
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)

                Button("Save", action: save)
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(24)
        .frame(width: 440)
    }
}

private struct ActivityDrawer: View {
    let state: WorkerLifecycleState
    let diagnosticLog: String
    let showTechnicalDetails: Bool

    private var activityText: String {
        var entries = [
            state.stageMessage,
            state.activityMessage,
            state.warningMessage,
            state.failureMessage,
            state.failureDetails,
        ]
            .compactMap { $0 }
            .filter { !$0.isEmpty }
        if showTechnicalDetails, !diagnosticLog.isEmpty, !entries.contains(diagnosticLog) {
            entries.append(diagnosticLog)
        }
        return entries.isEmpty ? "Activity will appear here when source analysis or conversion begins." : entries.joined(separator: "\n")
    }

    var body: some View {
        ScrollView {
            Text(activityText)
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .topLeading)
                .padding(12)
        }
        .frame(height: 145)
        .background(Color(nsColor: .textBackgroundColor))
        .accessibilityLabel("Activity details")
    }
}

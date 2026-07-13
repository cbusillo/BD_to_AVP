import AppKit
import SwiftUI

struct ContentView: View {
    @ObservedObject var viewModel: ConversionViewModel
    @ObservedObject var settings: AppSettings
    @ObservedObject var profileStore: ProfileStore
    let capabilities: AppCapabilities

    @State private var selectedProfileID: String
    @State private var options: ConversionOptions
    @State private var destinationURL: URL
    @State private var outputLength = OutputLength.fullMovie
    @State private var samplePosition = SamplePosition.beginning
    @State private var selectedTab = ConversionSetupTab.video
    @State private var insertedDiscs: [ConversionSource] = []
    @State private var isShowingActivity = false
    @State private var isDropTargeted = false
    @State private var isShowingSaveProfile = false
    @State private var newProfileName = ""
    @State private var profileErrorMessage: String?
    @State private var preserveEncodingOnNextProfileChange = false

    init(
        viewModel: ConversionViewModel,
        settings: AppSettings,
        profileStore: ProfileStore,
        capabilities: AppCapabilities
    ) {
        _viewModel = ObservedObject(wrappedValue: viewModel)
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
                    insertedDiscs: insertedDiscs,
                    makeMKVAvailable: DiscSourceDetector.makeMKVAvailable,
                    profile: selectedProfile,
                    options: options,
                    profileModified: profileModified,
                    outputOptionsAvailable: false,
                    destinationURL: $destinationURL,
                    outputLength: $outputLength,
                    samplePosition: $samplePosition,
                    plannedOutputURL: draft?.proposedOutputURL,
                    refreshDiscs: refreshDiscs,
                    useDisc: selectSource,
                    openDiscImage: { chooseFile(.discImage) },
                    openBluRayFolder: { chooseFolder(.bluRayFolder) },
                    openMKV: { chooseFile(.matroska) },
                    openSourceFolder: { chooseFolder(.sourceFolder) },
                    importTransportStream: { chooseFile(.transportStream) },
                    changeSource: chooseExistingSource,
                    chooseDestination: chooseDestination,
                    retryAnalysis: viewModel.restartInspection
                )
                .frame(minWidth: 350, idealWidth: 390, maxWidth: 450)

                ConversionSetupView(
                    selectedProfileID: $selectedProfileID,
                    selectedTab: $selectedTab,
                    options: $options,
                    profiles: profileStore.profiles,
                    selectedProfile: selectedProfile,
                    profileModified: profileModified,
                    isLocked: viewModel.hasActiveWorker,
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
            if viewModel.source == nil, !viewModel.hasActiveWorker {
                options.job = newValue
            }
        }
        .onChange(of: viewModel.state.conversionResult) { _, result in
            guard let result else {
                return
            }
            if settings.revealOutput {
                NSWorkspace.shared.activateFileViewerSelecting([result.outputURL])
            }
            if settings.playSound {
                NSSound(named: "Glass")?.play()
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
                  !viewModel.hasActiveWorker,
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
                .disabled(viewModel.hasActiveWorker)
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
            Button("Open 3D MKV…") { chooseFile(.matroska) }
            Button("Open Source Folder…") { chooseFolder(.sourceFolder) }

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
        .help("Choose a physical disc, disc image, Blu-ray folder, MKV, source folder, or transport stream")
        .disabled(!viewModel.canSelectSource)
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
            .accessibilityLabel("Status: \(statusText)")

            if viewModel.hasActiveWorker {
                ProgressView()
                    .controlSize(.small)
                    .padding(.leading, 4)
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

            if viewModel.hasActiveWorker {
                Button("Stop", role: .destructive, action: viewModel.stopActiveWorker)
                    .keyboardShortcut("p", modifiers: .command)
            } else if viewModel.source == nil || !conversionCanStart {
                Button("Start Processing") {}
                    .buttonStyle(.bordered)
                    .disabled(true)
                    .help(viewModel.source == nil ? "Choose a source before processing." : conversionUnavailableReason)
            } else {
                Button("Start Processing") {
                    if let draft {
                        viewModel.startConversion(draft: draft)
                    }
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut("p", modifiers: .command)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.regularMaterial)
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

    private var draft: ConversionDraft? {
        guard let source = viewModel.source else {
            return nil
        }
        return ConversionDraft(
            source: source,
            sourceDetails: viewModel.state.result,
            profile: selectedProfile,
            destinationURL: destinationURL,
            outputLength: outputLength,
            samplePosition: samplePosition,
            options: options
        )
    }

    private var statusText: String {
        if viewModel.hasActiveWorker {
            return viewModel.state.stageMessage
                ?? (viewModel.state.operationKind == .inspection ? "Reading source details" : "Converting video")
        }
        if viewModel.state.phase == .failed {
            return "Source needs attention"
        }
        if viewModel.state.conversionResult != nil {
            return "Conversion complete"
        }
        guard let source = viewModel.source else {
            return "Insert a 3D Blu-ray disc or choose another source"
        }
        if viewModel.state.result != nil {
            return "Source analyzed and conversion settings ready"
        }
        if source.kind.isDiscWorkflow {
            return "Disc workflow ready"
        }
        if source.kind == .sourceFolder {
            return "Source folder ready for batch processing"
        }
        return "Conversion settings ready"
    }

    private var secondaryStatusText: String? {
        if viewModel.hasActiveWorker {
            return viewModel.state.activityMessage
                ?? (viewModel.state.operationKind == .inspection ? "Inspecting video streams" : "Processing video")
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
        return draft?.proposedOutputURL.path
    }

    private var statusColor: Color {
        if viewModel.hasActiveWorker {
            return .blue
        }
        if viewModel.state.phase == .failed {
            return .red
        }
        return viewModel.source == nil ? .secondary : .green
    }

    private var conversionCanStart: Bool {
        capabilities.conversionAvailable
            && viewModel.source?.kind.supportsConversion == true
            && viewModel.state.result != nil
    }

    private var conversionUnavailableReason: String {
        guard capabilities.conversionAvailable else {
            return capabilities.conversionUnavailableReason
        }
        switch viewModel.source?.kind {
        case .physicalDisc, .bluRayFolder:
            return "Physical discs and Blu-ray folders are not yet supported for native conversion."
        case .sourceFolder:
            return "Batch folder conversion is not yet available."
        case .discImage, .matroska, .transportStream where viewModel.state.result == nil:
            return "Source analysis must complete before conversion can start."
        case .none, .discImage, .matroska, .transportStream:
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
        insertedDiscs = DiscSourceDetector.insertedDiscs()
    }

    private func selectSource(_ source: ConversionSource) {
        guard viewModel.canSelectSource else {
            return
        }
        viewModel.selectSource(source)
    }

    private func chooseExistingSource() {
        guard viewModel.canSelectSource, let sourceURL = SourcePicker.chooseExistingSource() else {
            return
        }
        viewModel.selectSource(sourceURL)
    }

    private func chooseFile(_ kind: ConversionSourceKind) {
        guard viewModel.canSelectSource, let source = SourcePicker.chooseFile(kind: kind) else {
            return
        }
        selectSource(source)
    }

    private func chooseFolder(_ kind: ConversionSourceKind) {
        guard viewModel.canSelectSource, let source = SourcePicker.chooseFolder(kind: kind) else {
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
        guard viewModel.canSelectSource, let url = urls.first, let source = ConversionSource.infer(from: url) else {
            return false
        }
        selectSource(source)
        return true
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

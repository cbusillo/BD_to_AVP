import AppKit
import SwiftUI

struct ConversionSetupView: View {
    @Environment(\.openWindow) private var openWindow

    @Binding var selectedProfileID: String
    @Binding var selectedTab: ConversionSetupTab
    @Binding var options: ConversionOptions
    let profiles: [EncodingProfile]
    let selectedProfile: EncodingProfile
    let profileModified: Bool
    let isLocked: Bool
    let sourceKind: ConversionSourceKind?
    @ObservedObject var routeQualityState: RouteQualityResolutionState
    @ObservedObject var resolutionMemoryStore: ResolutionMemoryStore
    let isReady: Bool
    let openEditor: () -> Void
    let saveSelectedProfile: () -> Void
    let saveAsNewProfile: () -> Void
    let resetProfile: () -> Void
    let sourceName: String?
    let destinationName: String?
    let changeDestination: (() -> Void)?
    let estimate: String?
    let preview: () -> Void
    let addToQueue: () -> Void
    let start: () -> Void
    let canPreview: Bool
    let canAddToQueue: Bool
    let canStart: Bool
    let showsStartAction: Bool
    let queueConflictForReview: ((RouteQualityConflict) -> Void)?

    init(
        selectedProfileID: Binding<String>,
        selectedTab: Binding<ConversionSetupTab>,
        options: Binding<ConversionOptions>,
        profiles: [EncodingProfile],
        selectedProfile: EncodingProfile,
        profileModified: Bool,
        isLocked: Bool,
        sourceKind: ConversionSourceKind?,
        routeQualityState: RouteQualityResolutionState,
        resolutionMemoryStore: ResolutionMemoryStore,
        isReady: Bool,
        openEditor: @escaping () -> Void,
        saveSelectedProfile: @escaping () -> Void,
        saveAsNewProfile: @escaping () -> Void,
        resetProfile: @escaping () -> Void,
        sourceName: String? = nil,
        destinationName: String? = nil,
        changeDestination: (() -> Void)? = nil,
        estimate: String? = nil,
        preview: @escaping () -> Void = {},
        addToQueue: @escaping () -> Void = {},
        start: @escaping () -> Void = {},
        canPreview: Bool = false,
        canAddToQueue: Bool = false,
        canStart: Bool = false,
        showsStartAction: Bool = true,
        queueConflictForReview: ((RouteQualityConflict) -> Void)? = nil
    ) {
        _selectedProfileID = selectedProfileID
        _selectedTab = selectedTab
        _options = options
        self.profiles = profiles
        self.selectedProfile = selectedProfile
        self.profileModified = profileModified
        self.isLocked = isLocked
        self.sourceKind = sourceKind
        self.routeQualityState = routeQualityState
        self.resolutionMemoryStore = resolutionMemoryStore
        self.isReady = isReady
        self.openEditor = openEditor
        self.saveSelectedProfile = saveSelectedProfile
        self.saveAsNewProfile = saveAsNewProfile
        self.resetProfile = resetProfile
        self.sourceName = sourceName
        self.destinationName = destinationName
        self.changeDestination = changeDestination
        self.estimate = estimate
        self.preview = preview
        self.addToQueue = addToQueue
        self.start = start
        self.canPreview = canPreview
        self.canAddToQueue = canAddToQueue
        self.canStart = canStart
        self.showsStartAction = showsStartAction
        self.queueConflictForReview = queueConflictForReview
    }

    var body: some View {
        Group {
            if isReady {
                readyBody
            } else {
                editorBody
            }
        }
    }

    private var readyBody: some View {
        VStack(alignment: .leading, spacing: 14) {
            LabeledContent("Source", value: sourceName ?? "Choose a source")
                .accessibilityIdentifier("ready-source")
            Divider()
            HStack(alignment: .firstTextBaseline) {
                Picker("Result", selection: $selectedProfileID) {
                    ForEach(profiles) { profile in
                        Text(profile.name).tag(profile.id)
                    }
                }
                .frame(maxWidth: 300)
                .disabled(isLocked)
                .accessibilityIdentifier("ready-profile-picker")
                Spacer()
                Button("Advanced Settings…", action: openEditor)
                    .buttonStyle(.bordered)
                    .disabled(isLocked)
                    .accessibilityIdentifier("edit-conversion-settings")
            }
            Text(profileOutcomeSummary)
                .font(.callout)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("ready-profile-summary")
            if profileModified {
                HStack(spacing: 8) {
                    Text("For this conversion")
                        .font(.caption.weight(.medium))
                    Text(profileOutcomeSummary)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    Spacer()
                    Button("Revert", action: resetProfile)
                    Button("Save as New…", action: saveAsNewProfile)
                }
                .padding(8)
                .background(.quaternary.opacity(0.55), in: RoundedRectangle(cornerRadius: 8))
                .accessibilityIdentifier("profile-conversion-customized")
            }
            HStack(alignment: .firstTextBaseline) {
                LabeledContent("Destination", value: destinationName ?? "Choose a destination")
                    .accessibilityIdentifier("ready-destination")
                if let changeDestination {
                    Spacer()
                    Button("Change…", action: changeDestination)
                        .disabled(isLocked)
                        .accessibilityIdentifier("ready-change-destination")
                }
            }
            Text(estimate ?? "Finished movie size and time are estimated after source analysis.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("ready-estimate")
            Divider()
            HStack {
                Button("Preview", action: preview)
                    .disabled(!canPreview)
                    .accessibilityIdentifier("ready-preview")
                Button("Add to Queue", action: addToQueue)
                    .disabled(!canAddToQueue)
                    .accessibilityIdentifier("ready-add-to-queue")
                Spacer()
                if showsStartAction {
                    Button("Start", action: start)
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut("p", modifiers: .command)
                        .disabled(!canStart)
                        .accessibilityIdentifier("ready-start")
                }
            }
        }
        .padding(18)
        .background(.background, in: RoundedRectangle(cornerRadius: 12))
        .overlay {
            RoundedRectangle(cornerRadius: 12)
                .strokeBorder(.separator.opacity(0.7))
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var editorBody: some View {
        VStack(spacing: 0) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Advanced Settings")
                        .font(.title3.weight(.semibold))
                    Text("Change only what you need for this conversion.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Spacer()

                Picker("Start with", selection: $selectedProfileID) {
                    ForEach(profiles) { profile in
                        Text(profile.name).tag(profile.id)
                    }
                }
                .frame(width: 190)
                .disabled(isLocked)

                if profileModified {
                    Text("For this conversion")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(.quaternary, in: Capsule())
                }

                Button(action: saveAsNewProfile) {
                    Label("Save current settings as new profile", systemImage: "plus.square.on.square")
                        .labelStyle(.iconOnly)
                }
                .buttonStyle(.borderless)
                .help("Save current settings as a new profile")
                .accessibilityLabel("Save current settings as new profile")
                .accessibilityHint("Opens a form to name and save these settings as a reusable profile")
                .accessibilityIdentifier("save-profile-action")
                .disabled(isLocked)

                Menu {
                    if selectedProfile.isCustom, profileModified {
                        Button("Save Changes to \(selectedProfile.name)", action: saveSelectedProfile)
                    }
                    if profileModified {
                        Divider()
                        Button("Reset to \(selectedProfile.name)", action: resetProfile)
                    }
                    Divider()
                    Button("Manage Profiles…") {
                        openWindow(id: AppWindowID.settings)
                    }
                } label: {
                    Label("Profile Actions", systemImage: "ellipsis.circle")
                        .labelStyle(.iconOnly)
                }
                .menuStyle(.borderlessButton)
                .help("Save, reset, or manage profiles")
                .disabled(isLocked)
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 14)

            Divider()

            HStack(spacing: 0) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Settings")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 10)
                        .padding(.bottom, 2)

                    ForEach(ConversionSetupTab.allCases) { tab in
                        Button {
                            selectedTab = tab
                        } label: {
                            Label(tab.title, systemImage: tab.systemImage)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 8)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .background(
                            selectedTab == tab ? Color.accentColor.opacity(0.14) : Color.clear,
                            in: RoundedRectangle(cornerRadius: 7)
                        )
                        .foregroundStyle(selectedTab == tab ? Color.accentColor : Color.primary)
                        .accessibilityIdentifier(tab.accessibilityIdentifier)
                    }

                    Spacer()
                }
                .padding(12)
                .frame(width: 190)
                .background(.bar)

                Divider()

                Group {
                    switch selectedTab {
                    case .video:
                        EncodingOptionsEditor(
                            options: $options.encoding,
                            section: .video,
                            jobOptions: options.job,
                            routeQualityState: routeQualityState,
                            conversionOptions: $options,
                            profile: selectedProfile,
                            sourceKind: sourceKind,
                            memoryStore: resolutionMemoryStore,
                            queueConflictForReview: queueConflictForReview
                        )
                    case .audioAndSubtitles:
                        EncodingOptionsEditor(options: $options.encoding, section: .audioAndSubtitles)
                    case .filesAndRecovery:
                        filesAndRecoveryForm
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .disabled(isLocked)
        }
        .background {
            InitialFocusAnchor()
                .frame(width: 0, height: 0)
                .accessibilityHidden(true)
        }
    }

    private var filesAndRecoveryForm: some View {
        Form {
            Section("Pipeline and Recovery") {
                VideoRouteSummaryView(plan: routePlan)

                Picker("Start stage", selection: startStageBinding) {
                    ForEach(ConversionStage.allCases) { stage in
                        Text(stage.title).tag(stage)
                    }
                }

                Picker("Output files", selection: reusableFileOutcomeBinding) {
                    ForEach(ReusableFileOutcome.allCases) { outcome in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(outcome.title)
                            Text(outcome.detail)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .tag(outcome)
                    }
                }
                .pickerStyle(.radioGroup)

                Toggle("Continue processing after recoverable errors", isOn: $options.job.continueOnError)
                Toggle("Use software HEVC encoder", isOn: softwareEncoderBinding)
                    .disabled(!softwareEncoderIsApplicable)
                    .help(softwareEncoderHelp)

                if options.job.softwareEncoder, softwareEncoderIsApplicable {
                    Text("Software HEVC requires generated left- and right-eye movies.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            Section("Output Files") {
                Toggle("Overwrite an existing output file", isOn: $options.job.overwriteExisting)
                Toggle(isOn: $options.job.removeOriginalAfterSuccess) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Remove original after success")
                        Text(
                            sourceKind == .physicalDisc
                                ? "Not available for physical discs. The disc is never modified."
                                : "Destructive — the source is removed only after the finished movie is verified."
                        )
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .disabled(sourceKind == .physicalDisc)
            }

            Section("Run Behavior") {
                Toggle("Keep the Mac awake", isOn: $options.job.keepAwake)
                Toggle("Play a sound when finished", isOn: $options.job.playSound)
                Toggle("Show generated commands in activity", isOn: $options.job.outputCommands)
            }

            if let conflict = routeQualityState.conflict {
                Section {
                    RouteQualityConflictView(
                        conflict: conflict,
                        profile: selectedProfile,
                        sourceKind: sourceKind,
                        memoryStore: resolutionMemoryStore,
                        queueForReview: queueConflictForReview.map { queue in { queue(conflict) } },
                        resolve: resolveRouteQuality
                    )
                }
            }
            if let invalidMessage = routeQualityState.invalidMessage {
                Section {
                    Label(invalidMessage, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundStyle(.red)
                }
            }
        }
        .formStyle(.grouped)
    }

    private var reusableFileOutcomeBinding: Binding<ReusableFileOutcome> {
        Binding(
            get: { ReusableFileOutcome(policy: options.job.intermediatePolicy) },
            set: { outcome in
                applyRouteEdit(.reusableIntermediates(outcome == .finishedMovieAndReusableFiles))
            }
        )
    }

    private var startStageBinding: Binding<ConversionStage> {
        Binding(
            get: { options.job.startStage },
            set: { stage in
                applyRouteEdit(.restartStage(stage))
            }
        )
    }

    private var softwareEncoderBinding: Binding<Bool> {
        Binding(
            get: { options.job.softwareEncoder },
            set: { enabled in
                applyRouteEdit(.softwareEncoder(enabled))
            }
        )
    }

    private func applyRouteEdit(_ edit: RouteQualityEdit) {
        routeQualityState.apply(edit, to: &options)
    }

    private func resolveRouteQuality(_ option: RouteQualityResolutionOption) {
        routeQualityState.resolve(option, in: &options)
    }

    private var routePlan: VideoRoutePlan {
        VideoRoutePlan(options: options)
    }

    private var profileOutcomeSummary: String {
        if !profileModified, let builtInProfile = BuiltInProfile(rawValue: selectedProfile.id) {
            return builtInProfile.summary
        }
        let reusableSuffix = options.job.intermediatePolicy.createsReusableArtifacts
            ? " · reusable files kept"
            : ""
        let qualitySummary = routePlan.kind == .existingArtifact
            ? "existing video kept"
            : "\(routePlan.qualityTitle) quality"
        return "\(qualitySummary)\(reusableSuffix)"
    }

    private var softwareEncoderIsApplicable: Bool {
        options.encoding.videoOutputMode == .mvHEVC
            && options.job.startStage.rawValue <= ConversionStage.createLeftRightFiles.rawValue
    }

    private var softwareEncoderHelp: String {
        if options.encoding.videoOutputMode == .av1Stereo {
            return "AV1 output always uses the bundled software encoder."
        }
        if !softwareEncoderIsApplicable {
            return "The selected restart stage does not encode HEVC video."
        }
        return "Use libx265 instead of the default VideoToolbox HEVC encoder."
    }
}

private struct InitialFocusAnchor: NSViewRepresentable {
    func makeNSView(context: Context) -> InitialFocusView {
        InitialFocusView()
    }

    func updateNSView(_ nsView: InitialFocusView, context: Context) {}
}

private final class InitialFocusView: NSView {
    override var acceptsFirstResponder: Bool { true }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
            guard let self, let window, window.firstResponder is NSTextView else {
                return
            }
            window.makeFirstResponder(self)
        }
    }
}

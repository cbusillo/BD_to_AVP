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
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Profile")
                        .font(.title3.weight(.semibold))
                    Text("Choose a Profile, then edit only when you need to change a setting.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Spacer()

                Picker("Profile", selection: $selectedProfileID) {
                    ForEach(profiles) { profile in
                        Text(profile.name).tag(profile.id)
                    }
                }
                .frame(width: 190)
                .disabled(isLocked)
                .accessibilityIdentifier("ready-profile-picker")

                if profileModified {
                    Text("For this conversion")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(.quaternary, in: Capsule())
                        .accessibilityIdentifier("profile-conversion-customized")
                }

                Button("Edit…", action: openEditor)
                    .buttonStyle(.borderedProminent)
                    .disabled(isLocked)
                    .accessibilityIdentifier("edit-conversion-settings")
            }

            Divider()

            VStack(alignment: .leading, spacing: 8) {
                Text(selectedProfile.name)
                    .font(.headline)
                Text(selectedProfile.summary)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Text("Quality: \(options.encoding.videoSummary(for: options.videoRoutePlan))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text("Files: \(ReusableFileOutcome(policy: options.job.intermediatePolicy).title)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("ready-profile-summary")

            OutcomeSummaryView(options: options)
        }
        .padding(18)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var editorBody: some View {
        VStack(spacing: 0) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Conversion Setup")
                        .font(.title3.weight(.semibold))
                    Text("These choices apply to the current disc or source.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Spacer()

                Picker("Profile", selection: $selectedProfileID) {
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

            Picker("Conversion settings", selection: $selectedTab) {
                ForEach(ConversionSetupTab.allCases) { tab in
                    Text(tab.title).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(.horizontal, 18)
            .padding(.vertical, 12)
            .disabled(isLocked)

            Divider()

            Group {
                switch selectedTab {
                case .video:
                    EncodingOptionsEditor(
                        options: $options.encoding,
                        section: .video,
                        jobOptions: options.job,
                        routeQualityState: routeQualityState
                    )
                case .audioAndSubtitles:
                    EncodingOptionsEditor(options: $options.encoding, section: .audioAndSubtitles)
                case .filesAndRecovery:
                    filesAndRecoveryForm
                }
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
                        Text(outcome.title).tag(outcome)
                    }
                }
                .pickerStyle(.radioGroup)

                Text(reusableFileOutcomeDetail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                OutcomeSummaryView(options: options)

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

    private var reusableFileOutcomeDetail: String {
        ReusableFileOutcome(policy: options.job.intermediatePolicy).detail
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

private struct OutcomeSummaryView: View {
    let options: ConversionOptions

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("What you’ll get")
                .font(.headline)
            LabeledContent("Finished movie", value: options.encoding.videoSummary(for: options.videoRoutePlan))
            LabeledContent("Temporary space", value: options.job.intermediatePolicy.createsReusableArtifacts ? "More space while keeping reusable files" : "Removed after success")
            LabeledContent("Reusable files", value: ReusableFileOutcome(policy: options.job.intermediatePolicy).title)
            LabeledContent("Estimated time", value: options.job.intermediatePolicy.createsReusableArtifacts ? "Longer processing" : "Standard processing")
            LabeledContent("Quality", value: options.encoding.videoSummary(for: options.videoRoutePlan))
        }
        .font(.caption)
        .padding(12)
        .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 10))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("What you’ll get")
        .accessibilityIdentifier("conversion-outcome-summary")
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

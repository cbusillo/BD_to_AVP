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
    let saveSelectedProfile: () -> Void
    let saveAsNewProfile: () -> Void
    let resetProfile: () -> Void

    var body: some View {
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
                    Text("Modified")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.orange)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(Color.orange.opacity(0.12), in: Capsule())
                }

                Menu {
                    if selectedProfile.isCustom, profileModified {
                        Button("Save Changes to \(selectedProfile.name)", action: saveSelectedProfile)
                    }
                    Button("Save Current Settings as New Profile…", action: saveAsNewProfile)
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
                    EncodingOptionsEditor(options: $options.encoding, section: .video)
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
                Picker("Start stage", selection: $options.job.startStage) {
                    ForEach(ConversionStage.allCases) { stage in
                        Text(stage.title).tag(stage)
                    }
                }

                Toggle("Keep durable stage files", isOn: $options.job.keepStageFiles)
                Toggle("Continue processing after recoverable errors", isOn: $options.job.continueOnError)
                Toggle("Use software HEVC encoder", isOn: $options.job.softwareEncoder)
                    .disabled(options.encoding.videoOutputMode == .av1Stereo)
                    .help(
                        options.encoding.videoOutputMode == .av1Stereo
                            ? "AV1 output always uses the bundled software encoder."
                            : "Use libx265 instead of the default VideoToolbox HEVC encoder."
                    )
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
        }
        .formStyle(.grouped)
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

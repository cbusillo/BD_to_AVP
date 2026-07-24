import SwiftUI

enum EncodingOptionsSection: String, CaseIterable, Identifiable {
    case video
    case audioAndSubtitles

    var id: String { rawValue }

    var title: String {
        switch self {
        case .video:
            "Video"
        case .audioAndSubtitles:
            "Audio & Subtitles"
        }
    }
}

struct EncodingOptionsEditor: View {
    @Binding var options: EncodingOptions
    let section: EncodingOptionsSection
    let jobOptions: JobOptions?

    @State private var showsAdvancedDirectBitrate: Bool
    @State private var showsAdvancedGeneratedBitrate: Bool

    init(
        options: Binding<EncodingOptions>,
        section: EncodingOptionsSection,
        jobOptions: JobOptions? = nil
    ) {
        _options = options
        self.section = section
        self.jobOptions = jobOptions
        _showsAdvancedDirectBitrate = State(
            initialValue: options.wrappedValue.mvHEVC.directFinalBitrate.mode == .custom
        )
        _showsAdvancedGeneratedBitrate = State(
            initialValue: options.wrappedValue.mvHEVC.generatedEyeBitrate.mode == .custom
        )
    }

    var body: some View {
        switch section {
        case .video:
            videoForm
        case .audioAndSubtitles:
            audioAndSubtitlesForm
        }
    }

    private var videoForm: some View {
        Form {
            Group {
                Section("Video Output") {
                    Picker("Format", selection: $options.videoOutputMode) {
                        ForEach(VideoOutputMode.allCases) { mode in
                            Text(mode.title).tag(mode)
                        }
                    }

                    Text(options.videoOutputMode.detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    Divider()
                    VideoRouteSummaryView(plan: routePlan)

                    if routePlan.kind == .existingArtifact {
                        if options.videoOutputMode == .mvHEVC,
                           routePlan.startStage == ConversionStage.upscaleVideo.rawValue
                        {
                            Toggle("AI FX upscale to 2× resolution", isOn: $options.upscaleEnabled)
                            if options.upscaleEnabled {
                                EncodingQualitySliderRow(title: "Upscale quality", value: upscaleQualityBinding)
                            }
                        }
                    } else if options.videoOutputMode == .mvHEVC {
                        if routePlan.usesGeneratedSettings {
                            EncodingQualitySliderRow(
                                title: "MV-HEVC merge quality",
                                value: hevcQualityBinding
                            )

                            LabeledContent("Eye intermediate bitrate") {
                                Text(generatedEyeBitrateSummary)
                                    .foregroundStyle(.secondary)
                                    .multilineTextAlignment(.trailing)
                            }

                            DisclosureGroup(
                                "Advanced eye intermediate bitrate",
                                isExpanded: $showsAdvancedGeneratedBitrate
                            ) {
                                Picker("Bitrate policy", selection: generatedEyeBitrateModeBinding) {
                                    Text("Automatic (Recommended)").tag(BitrateMode.automatic)
                                    Text("Custom").tag(BitrateMode.custom)
                                }
                                .pickerStyle(.segmented)

                                if options.mvHEVC.generatedEyeBitrate.mode == .custom {
                                    LabeledContent("Custom target") {
                                        Stepper(value: generatedEyeBitrateBinding, in: 1 ... 500) {
                                            Text("\(options.generatedEyeCustomBitrateMbps) Mbps per eye")
                                                .monospacedDigit()
                                                .multilineTextAlignment(.trailing)
                                        }
                                    }
                                } else {
                                    Text(
                                        "Automatic currently resolves to \(VideoRoutePlan.automaticGeneratedEyeBitrateMbps) Mbps per eye."
                                    )
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }

                                Text("This target applies only to generated left- and right-eye movies.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        } else {
                            LabeledContent("Final bitrate") {
                                Text(directFinalBitrateSummary)
                                    .foregroundStyle(.secondary)
                                    .multilineTextAlignment(.trailing)
                            }

                            DisclosureGroup(
                                "Advanced final bitrate",
                                isExpanded: $showsAdvancedDirectBitrate
                            ) {
                                Picker("Bitrate policy", selection: directFinalBitrateModeBinding) {
                                    Text("Automatic (Recommended)").tag(BitrateMode.automatic)
                                    Text("Custom").tag(BitrateMode.custom)
                                }
                                .pickerStyle(.segmented)

                                if options.mvHEVC.directFinalBitrate.mode == .custom {
                                    LabeledContent("Custom target") {
                                        Stepper(value: directFinalBitrateBinding, in: 1 ... 500) {
                                            Text("\(directFinalCustomBitrateMbps) Mbps final")
                                                .monospacedDigit()
                                                .multilineTextAlignment(.trailing)
                                        }
                                    }
                                } else {
                                    Text(
                                        "Automatic currently resolves to \(VideoRoutePlan.automaticDirectBitrateMbps) Mbps final."
                                    )
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }

                                Text("This target applies only when direct MV-HEVC is selected during preflight.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }

                        Toggle("AI FX upscale to 2× resolution", isOn: $options.upscaleEnabled)

                        if options.upscaleEnabled {
                            EncodingQualitySliderRow(title: "Upscale quality", value: upscaleQualityBinding)
                            Toggle(
                                "Link HEVC and upscale quality",
                                isOn: $options.mvHEVC.linkGeneratedAndUpscaleQuality
                            )
                                .onChange(of: options.mvHEVC.linkGeneratedAndUpscaleQuality) { _, linked in
                                    if linked {
                                        options.upscaleQuality = options.mvHEVC.generatedMergeQuality
                                    }
                                }
                        }
                    } else {
                        LabeledContent("AV1 quality") {
                            Stepper(value: $options.av1CRF, in: 0 ... 63) {
                                Text("CRF \(options.av1CRF)")
                                    .monospacedDigit()
                                    .frame(width: 74, alignment: .trailing)
                            }
                        }

                        Text("Lower CRF values preserve more detail and create larger files. CRF 32 is the balanced default.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }

                if routePlan.kind != .existingArtifact {
                    Section("Picture") {
                        if options.videoOutputMode == .mvHEVC {
                            LabeledContent("Field of view") {
                                Stepper(value: $options.fieldOfView, in: 0 ... 360) {
                                    Text("\(options.fieldOfView)°")
                                        .monospacedDigit()
                                        .frame(width: 48, alignment: .trailing)
                                }
                            }

                            LabeledContent("Resolution override") {
                                TextField("", text: $options.resolutionOverride, prompt: Text("Use source"))
                                    .textFieldStyle(.roundedBorder)
                                    .frame(width: 170)
                            }
                        } else {
                            LabeledContent("Resolution") {
                                Text("Full source resolution per eye")
                                    .foregroundStyle(.secondary)
                            }
                        }

                        LabeledContent("Frame-rate override") {
                            TextField("", text: $options.frameRateOverride, prompt: Text("Use source"))
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 170)
                        }
                    }

                    Section("Stereo Corrections") {
                        Toggle("Crop black bars", isOn: $options.cropBlackBars)
                        Toggle("Swap left and right eyes", isOn: $options.swapEyes)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .onChange(of: options.mvHEVC.directFinalBitrate.mode) { _, mode in
            showsAdvancedDirectBitrate = mode == .custom
        }
        .onChange(of: options.mvHEVC.generatedEyeBitrate.mode) { _, mode in
            showsAdvancedGeneratedBitrate = mode == .custom
        }
    }

    private var audioAndSubtitlesForm: some View {
        Form {
            Group {
                Section("Audio") {
                    Picker("Audio handling", selection: $options.audioHandling) {
                        ForEach(AudioHandling.allCases) { handling in
                            Text(handling.title).tag(handling)
                        }
                    }

                    Text(options.audioHandling.detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    Picker("Audio languages", selection: $options.audioLanguages.mode) {
                        ForEach(AudioLanguageMode.allCases) { mode in
                            Text(mode.title).tag(mode)
                        }
                    }
                    .pickerStyle(.segmented)

                    if options.audioLanguages.mode == .preferredOnly {
                        LanguagePickerField(
                            purpose: .audio,
                            selection: $options.audioLanguages.preferredLanguage
                        )
                    }

                    Text(options.audioLanguages.mode.detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    if let bitrateLabel = options.audioHandling.bitrateLabel {
                        LabeledContent(bitrateLabel) {
                            Stepper(value: $options.audioBitrate, in: 128 ... 1_000, step: 32) {
                                Text("\(options.audioBitrate) kbps")
                                    .monospacedDigit()
                                    .frame(width: 92, alignment: .trailing)
                            }
                        }
                    }
                }

                Section("Subtitles") {
                    Picker("Subtitle handling", selection: $options.subtitles.mode) {
                        ForEach(SubtitleMode.allCases) { mode in
                            Text(mode.title).tag(mode)
                        }
                    }
                    .pickerStyle(.segmented)

                    if options.subtitles.mode != .off {
                        LanguagePickerField(
                            purpose: .subtitle,
                            selection: $options.subtitles.preferredLanguage
                        )
                    }

                    Text(options.subtitles.mode.detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .formStyle(.grouped)
    }

    private var hevcQualityBinding: Binding<Int> {
        Binding(
            get: { options.mvHEVC.generatedMergeQuality },
            set: { newValue in
                options.mvHEVC.generatedMergeQuality = newValue
                if options.mvHEVC.linkGeneratedAndUpscaleQuality {
                    options.upscaleQuality = newValue
                }
            }
        )
    }

    private var routePlan: VideoRoutePlan {
        VideoRoutePlan(
            encoding: options,
            job: jobOptions ?? JobOptions()
        )
    }

    private var generatedEyeBitrateSummary: String {
        switch options.mvHEVC.generatedEyeBitrate.mode {
        case .automatic:
            "Automatic (Recommended) · \(VideoRoutePlan.automaticGeneratedEyeBitrateMbps) Mbps per eye"
        case .custom:
            "Custom · \(options.generatedEyeCustomBitrateMbps) Mbps per eye"
        }
    }

    private var directFinalBitrateSummary: String {
        switch options.mvHEVC.directFinalBitrate.mode {
        case .automatic:
            "Automatic (Recommended) · \(VideoRoutePlan.automaticDirectBitrateMbps) Mbps final"
        case .custom:
            "Custom · \(directFinalCustomBitrateMbps) Mbps final"
        }
    }

    private var directFinalCustomBitrateMbps: Int {
        options.mvHEVC.directFinalBitrate.customMbps ?? VideoRoutePlan.automaticDirectBitrateMbps
    }

    private var directFinalBitrateModeBinding: Binding<BitrateMode> {
        Binding(
            get: { options.mvHEVC.directFinalBitrate.mode },
            set: { mode in
                options.mvHEVC.directFinalBitrate.mode = mode
                if mode == .custom, options.mvHEVC.directFinalBitrate.customMbps == nil {
                    options.mvHEVC.directFinalBitrate.customMbps = VideoRoutePlan.automaticDirectBitrateMbps
                }
            }
        )
    }

    private var directFinalBitrateBinding: Binding<Int> {
        Binding(
            get: { directFinalCustomBitrateMbps },
            set: { newValue in
                options.mvHEVC.directFinalBitrate.mode = .custom
                options.mvHEVC.directFinalBitrate.customMbps = newValue
            }
        )
    }

    private var generatedEyeBitrateModeBinding: Binding<BitrateMode> {
        Binding(
            get: { options.mvHEVC.generatedEyeBitrate.mode },
            set: { mode in
                options.mvHEVC.generatedEyeBitrate.mode = mode
                if mode == .custom, options.mvHEVC.generatedEyeBitrate.customMbps == nil {
                    options.mvHEVC.generatedEyeBitrate.customMbps = MVHEVCOptions.defaultGeneratedEyeBitrate
                }
            }
        )
    }

    private var generatedEyeBitrateBinding: Binding<Int> {
        Binding(
            get: { options.generatedEyeCustomBitrateMbps },
            set: { newValue in
                options.mvHEVC.generatedEyeBitrate.mode = .custom
                options.mvHEVC.generatedEyeBitrate.customMbps = newValue
            }
        )
    }

    private var upscaleQualityBinding: Binding<Int> {
        Binding(
            get: { options.upscaleQuality },
            set: { newValue in
                options.upscaleQuality = newValue
                if options.mvHEVC.linkGeneratedAndUpscaleQuality {
                    options.mvHEVC.generatedMergeQuality = newValue
                }
            }
        )
    }
}

private struct EncodingQualitySliderRow: View {
    let title: String
    @Binding var value: Int

    var body: some View {
        LabeledContent(title) {
            HStack(spacing: 10) {
                Slider(
                    value: Binding(
                        get: { Double(value) },
                        set: { value = Int($0.rounded()) }
                    ),
                    in: 0 ... 100,
                    step: 1
                )
                .frame(minWidth: 120, idealWidth: 180)

                Text("\(value)")
                    .monospacedDigit()
                    .frame(width: 30, alignment: .trailing)
            }
        }
    }
}

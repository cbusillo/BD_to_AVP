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
                Section("Spatial Video Encoding") {
                    EncodingQualitySliderRow(title: "HEVC quality", value: hevcQualityBinding)
                    LabeledContent("Left / right bitrate") {
                        Stepper(value: $options.leftRightBitrate, in: 1 ... 100) {
                            Text("\(options.leftRightBitrate) Mbps")
                                .monospacedDigit()
                                .frame(width: 76, alignment: .trailing)
                        }
                    }
                    Toggle("AI FX upscale to 2× resolution", isOn: $options.upscaleEnabled)

                    if options.upscaleEnabled {
                        EncodingQualitySliderRow(title: "Upscale quality", value: upscaleQualityBinding)
                        Toggle("Link HEVC and upscale quality", isOn: $options.linkQuality)
                            .onChange(of: options.linkQuality) { _, linked in
                                if linked {
                                    options.upscaleQuality = options.hevcQuality
                                }
                            }
                    }
                }

                Section("Picture") {
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
        .formStyle(.grouped)
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
                    .pickerStyle(.segmented)

                    Text(options.audioHandling.detail)
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

                Section("Subtitles and Languages") {
                    Picker("Subtitle handling", selection: $options.subtitles.mode) {
                        ForEach(SubtitleMode.allCases) { mode in
                            Text(mode.title).tag(mode)
                        }
                    }
                    .pickerStyle(.segmented)

                    if options.subtitles.mode != .off {
                        LanguagePickerField(selection: $options.subtitles.preferredLanguage)
                    }

                    Text(
                        "\(options.subtitles.mode.detail) Source audio tracks are preserved independently of subtitle choices."
                    )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
    }

    private var hevcQualityBinding: Binding<Int> {
        Binding(
            get: { options.hevcQuality },
            set: { newValue in
                options.hevcQuality = newValue
                if options.linkQuality {
                    options.upscaleQuality = newValue
                }
            }
        )
    }

    private var upscaleQualityBinding: Binding<Int> {
        Binding(
            get: { options.upscaleQuality },
            set: { newValue in
                options.upscaleQuality = newValue
                if options.linkQuality {
                    options.hevcQuality = newValue
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
                .frame(minWidth: 180)

                Text("\(value)")
                    .monospacedDigit()
                    .frame(width: 30, alignment: .trailing)
            }
        }
    }
}

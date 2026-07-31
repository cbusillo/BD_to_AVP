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

    init(
        options: Binding<EncodingOptions>,
        section: EncodingOptionsSection,
        jobOptions: JobOptions? = nil
    ) {
        _options = options
        self.section = section
        self.jobOptions = jobOptions
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
                    Picker("Format", selection: videoOutputModeBinding) {
                        ForEach(VideoOutputMode.allCases) { mode in
                            Text(mode.title).tag(mode)
                        }
                    }

                    Text(options.videoOutputMode.detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    if options.videoOutputMode == .av1Stereo {
                        Label(
                            "M2 Vision Pro is not supported. Use AV1 only for M5 Vision Pro testing until physical M5 stereo playback is confirmed.",
                            systemImage: "exclamationmark.triangle.fill"
                        )
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                    }

                    Divider()
                    VideoRouteSummaryView(plan: routePlan)
                    VideoQualityEditor(options: $options, context: qualityEditorContext)
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

    private var videoOutputModeBinding: Binding<VideoOutputMode> {
        Binding(
            get: { options.videoOutputMode },
            set: { mode in
                options.selectVideoOutputMode(mode)
            }
        )
    }

    private var routePlan: VideoRoutePlan {
        VideoRoutePlan(
            encoding: options,
            job: jobOptions ?? JobOptions()
        )
    }

    private var qualityEditorContext: VideoQualityEditor.Context {
        jobOptions.map(VideoQualityEditor.Context.conversion) ?? .profile
    }
}

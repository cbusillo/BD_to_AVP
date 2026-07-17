import SwiftUI
import UniformTypeIdentifiers

struct PlaybackProbeView: View {
    @ObservedObject var model: PlaybackProbeModel
    @State private var isImporting = false

    var body: some View {
        VStack(spacing: 0) {
            inspector
            Divider()
            playbackControls
        }
        .frame(minWidth: 520, minHeight: 620)
        .fileImporter(
            isPresented: $isImporting,
            allowedContentTypes: [.movie],
            allowsMultipleSelection: false
        ) { result in
            switch result {
            case let .success(urls):
                if let url = urls.first {
                    model.importAsset(from: url)
                }
            case let .failure(error):
                model.reportImportFailure(error)
            }
        }
    }

    private var playbackControls: some View {
        HStack(spacing: 12) {
            Button {
                model.togglePlayback()
            } label: {
                Label(model.isPlaying ? "Pause" : "Play", systemImage: model.isPlaying ? "pause.fill" : "play.fill")
            }
            .disabled(!model.canControlPlayback)

            Button("Beginning") {
                model.seek(to: .beginning)
            }
            .disabled(!model.canSeek)

            Button("Middle") {
                model.seek(to: .middle)
            }
            .disabled(!model.canSeek)

            Button("End") {
                model.seek(to: .end)
            }
            .disabled(!model.canSeek)

            Spacer()

            Text(model.timeSummary)
                .font(.system(.body, design: .monospaced))
                .foregroundStyle(.secondary)

            Button("Open…") {
                isImporting = true
            }

            Button(model.spatialViewIsOpen ? "Close Spatial View" : "Open Spatial View") {
                model.toggleSpatialView()
            }
        }
        .buttonStyle(.bordered)
        .padding(.horizontal, 24)
        .padding(.vertical, 16)
        .background(.regularMaterial)
    }

    private var inspector: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Spatial Playback Probe")
                        .font(.title2.bold())
                    Text(model.assetName)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }

                statusSection

                if let failure = model.failure {
                    failureSection(failure)
                }

                mediaSection

                VStack(alignment: .leading, spacing: 8) {
                    Label("Playback-only companion", systemImage: "checkmark.shield")
                        .font(.headline)
                    Text("The target imports finalized movies and reports native decode and rendering state. It does not embed Python, ripping, or conversion dependencies.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(24)
        }
        .background(.ultraThinMaterial)
    }

    private var statusSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Validation")
                .font(.headline)

            statusRow(
                title: "Stereo MV-HEVC decode",
                value: model.decodeSupportText,
                symbol: model.stereoDecodeSupported ? "checkmark.circle.fill" : "xmark.octagon.fill",
                color: model.stereoDecodeSupported ? .green : .red
            )
            statusRow(title: "AVPlayer item", value: model.playerItemStatusText)
            statusRow(title: "RealityKit rendering", value: model.renderingStatusText)
            statusRow(title: "Spatial view", value: model.spatialViewStatusText)
            statusRow(title: "Requested presentation", value: "Stereo · Spatial · Portal")
            statusRow(
                title: "Actual presentation",
                value: model.actualPresentationText,
                symbol: model.isActuallySpatial ? "checkmark.circle.fill" : "circle.dashed",
                color: model.isActuallySpatial ? .green : .orange
            )
            .accessibilityIdentifier("actual-presentation-status")
        }
    }

    private var mediaSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Media")
                .font(.headline)

            Picker("Audio", selection: $model.selectedAudioID) {
                ForEach(model.audioOptions) { option in
                    Text(option.name).tag(option.id)
                }
            }
            .disabled(model.audioOptions.isEmpty)

            Picker("Subtitles", selection: $model.selectedSubtitleID) {
                ForEach(model.subtitleOptions) { option in
                    Text(option.name).tag(option.id)
                }
            }
            .disabled(model.subtitleOptions.isEmpty)
        }
        .onChange(of: model.selectedAudioID) { _, identifier in
            model.selectAudio(identifier)
        }
        .onChange(of: model.selectedSubtitleID) { _, identifier in
            model.selectSubtitle(identifier)
        }
    }

    private func failureSection(_ failure: ProbeFailure) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(failure.title, systemImage: "exclamationmark.triangle.fill")
                .font(.headline)
                .foregroundStyle(.red)
            Text(failure.message)
                .font(.caption)
            Text("Category: \(failure.category.rawValue)")
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
        }
        .padding(14)
        .background(.red.opacity(0.12), in: RoundedRectangle(cornerRadius: 14))
    }

    private func statusRow(
        title: String,
        value: String,
        symbol: String = "circle.fill",
        color: Color = .secondary
    ) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: symbol)
                .foregroundStyle(color)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(value)
                    .font(.body)
            }
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
    }

}

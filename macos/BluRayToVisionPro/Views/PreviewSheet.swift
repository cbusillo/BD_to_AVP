import AVKit
import SwiftUI

struct PreviewSheet: View {
    @Environment(\.dismiss) private var dismiss

    @ObservedObject var viewModel: PreviewViewModel
    let conversionDraft: ConversionDraft
    @Binding var outputLength: OutputLength
    @Binding var samplePosition: SamplePosition
    let startFullConversion: (PreviewDraft) -> Void

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    selectionCard
                    statusContent
                }
                .padding(22)
            }

            Divider()
            footer
        }
        .frame(width: 820)
        .frame(minHeight: 620)
        .interactiveDismissDisabled(viewModel.hasActiveWorker)
        .onAppear(perform: viewModel.validateArtifact)
    }

    private var header: some View {
        HStack(spacing: 14) {
            Image(systemName: "play.rectangle.on.rectangle")
                .font(.system(size: 28, weight: .semibold))
                .foregroundStyle(.tint)
                .frame(width: 46, height: 46)
                .background(.tint.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))

            VStack(alignment: .leading, spacing: 3) {
                Text("Representative Conversion Preview")
                    .font(.title2.weight(.semibold))
                Text("Uses a separate child job and the exact resolved profile settings below.")
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(22)
    }

    private var selectionCard: some View {
        GroupBox {
            Grid(alignment: .leading, horizontalSpacing: 22, verticalSpacing: 12) {
                GridRow {
                    Text("Length")
                        .foregroundStyle(.secondary)
                    Picker("Preview length", selection: $outputLength) {
                        ForEach(OutputLength.previewCases) { length in
                            Text(length.name.replacingOccurrences(of: " Sample", with: "")).tag(length)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.segmented)
                    .disabled(viewModel.hasActiveWorker)
                }

                GridRow {
                    Text("Range")
                        .foregroundStyle(.secondary)
                    Picker("Preview range", selection: $samplePosition) {
                        ForEach(SamplePosition.allCases) { position in
                            Text(position.name).tag(position)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.segmented)
                    .disabled(viewModel.hasActiveWorker)
                }

                Divider()
                    .gridCellColumns(2)

                GridRow {
                    Text("Source")
                        .foregroundStyle(.secondary)
                    Text(conversionDraft.source.displayName)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }

                GridRow {
                    Text("Profile")
                        .foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(conversionDraft.profile.name)
                            .fontWeight(.medium)
                        Text(conversionDraft.options.compactSummary)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                if conversionDraft.source.kind == .discImage {
                    Divider()
                        .gridCellColumns(2)
                    Label(
                        "ISO previews must prepare the selected disc title before the bounded range can be encoded.",
                        systemImage: "clock.badge.exclamationmark"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .gridCellColumns(2)
                }
            }
            .padding(4)
        } label: {
            Label("Preview Snapshot", systemImage: "camera.filters")
                .font(.headline)
        }
    }

    @ViewBuilder
    private var statusContent: some View {
        if viewModel.phase == .ready, let lease = viewModel.artifactLease {
            PreviewPlayerView(
                lease: lease,
                generateAgain: generatePreview,
                removePreview: viewModel.discardPreview,
                startFullConversion: startReviewedConversion
            )
            .id(lease.artifact.outputPath)
        } else {
            GroupBox {
                VStack(alignment: .leading, spacing: 14) {
                    HStack(spacing: 12) {
                        statusIcon
                        VStack(alignment: .leading, spacing: 3) {
                            Text(viewModel.stageMessage)
                                .font(.headline)
                            if let detail = statusDetail {
                                Text(detail)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                        if viewModel.hasActiveWorker {
                            ProgressView()
                                .controlSize(.small)
                        }
                    }

                    if !viewModel.diagnosticLog.isEmpty, viewModel.phase == .failed {
                        DisclosureGroup("Technical Details") {
                            Text(viewModel.diagnosticLog)
                                .font(.caption.monospaced())
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.top, 6)
                        }
                    }
                }
                .padding(4)
            } label: {
                Label("Preview Status", systemImage: "waveform.path.ecg")
                    .font(.headline)
            }
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            if viewModel.hasActiveWorker {
                Text("The preview cache is isolated from the full conversion output.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Stop Preview", role: .destructive, action: viewModel.stopActiveWorker)
            } else {
                Button("Close") {
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)
                Spacer()
                if viewModel.phase != .ready {
                    Button(viewModel.phase == .failed ? "Try Again" : "Generate Preview", action: generatePreview)
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 14)
        .background(.regularMaterial)
    }

    private var statusIcon: some View {
        Image(systemName: statusSystemImage)
            .font(.system(size: 22, weight: .semibold))
            .foregroundStyle(statusColor)
            .frame(width: 38, height: 38)
            .background(statusColor.opacity(0.12), in: Circle())
    }

    private var statusSystemImage: String {
        switch viewModel.phase {
        case .idle:
            "slider.horizontal.3"
        case .preparing:
            "film.stack"
        case .encoding:
            "visionpro"
        case .ready:
            "play.fill"
        case .stopping:
            "stop.fill"
        case .failed:
            "exclamationmark.triangle.fill"
        case .expired:
            "clock.badge.xmark"
        }
    }

    private var statusColor: Color {
        switch viewModel.phase {
        case .failed:
            .red
        case .expired:
            .orange
        case .idle:
            .secondary
        default:
            .accentColor
        }
    }

    private var statusDetail: String? {
        viewModel.failureMessage
            ?? viewModel.activityMessage
            ?? (viewModel.phase == .idle
                ? "Choose a representative range, then generate a finalized spatial-video sample."
                : nil)
    }

    private func generatePreview() {
        guard let previewDraft = PreviewDraft(
            conversion: conversionDraft,
            outputLength: outputLength,
            samplePosition: samplePosition
        ) else {
            return
        }
        viewModel.startPreview(previewDraft)
    }

    private func startReviewedConversion() {
        guard let reviewedDraft = viewModel.reviewedDraft else {
            return
        }
        startFullConversion(reviewedDraft)
    }
}

private struct PreviewPlayerView: View {
    let lease: PreviewArtifactLease
    let generateAgain: () -> Void
    let removePreview: () -> Void
    let startFullConversion: () -> Void

    @State private var player: AVPlayer

    init(
        lease: PreviewArtifactLease,
        generateAgain: @escaping () -> Void,
        removePreview: @escaping () -> Void,
        startFullConversion: @escaping () -> Void
    ) {
        self.lease = lease
        self.generateAgain = generateAgain
        self.removePreview = removePreview
        self.startFullConversion = startFullConversion
        _player = State(initialValue: AVPlayer(url: lease.artifact.outputURL))
    }

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 12) {
                VideoPlayer(player: player)
                    .frame(minHeight: 300)
                    .background(.black, in: RoundedRectangle(cornerRadius: 8))
                    .clipShape(RoundedRectangle(cornerRadius: 8))

                HStack(spacing: 12) {
                    Label(rangeDescription, systemImage: "timeline.selection")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button("Replay") {
                        player.seek(to: .zero)
                        player.play()
                    }
                    Button("Remove Preview", role: .destructive) {
                        tearDownPlayer()
                        removePreview()
                    }
                    Button("Generate Again") {
                        tearDownPlayer()
                        removePreview()
                        generateAgain()
                    }
                    Button("Start Full Conversion") {
                        tearDownPlayer()
                        startFullConversion()
                    }
                    .buttonStyle(.borderedProminent)
                }

                Text("Native playback includes seek, time, audio, and subtitle controls when those tracks are available.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(4)
        } label: {
            Label("Ready to Play", systemImage: "play.circle.fill")
                .font(.headline)
                .foregroundStyle(.green)
        }
        .onDisappear(perform: tearDownPlayer)
    }

    private var rangeDescription: String {
        let start = Int(lease.artifact.startSeconds.rounded())
        let duration = Int(lease.artifact.durationSeconds.rounded())
        return "\(lease.artifact.position.capitalized) · starts at \(formatTime(start)) · \(formatTime(duration))"
    }

    private func formatTime(_ seconds: Int) -> String {
        let hours = seconds / 3600
        let minutes = (seconds % 3600) / 60
        let remainingSeconds = seconds % 60
        if hours > 0 {
            return String(format: "%d:%02d:%02d", hours, minutes, remainingSeconds)
        }
        return String(format: "%d:%02d", minutes, remainingSeconds)
    }

    private func tearDownPlayer() {
        player.pause()
        player.replaceCurrentItem(with: nil)
    }
}

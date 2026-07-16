import SwiftUI

struct SourceWorkspaceView: View {
    let source: ConversionSource?
    let state: WorkerLifecycleState
    let batchQueue: ConversionQueueState?
    let isBatchRunning: Bool
    let insertedDiscs: [ConversionSource]
    let makeMKVAvailable: Bool
    let profile: EncodingProfile
    let options: ConversionOptions
    let profileModified: Bool
    @Binding var destinationURL: URL
    let plannedOutputURL: URL?
    let refreshDiscs: () -> Void
    let useDisc: (ConversionSource) -> Void
    let openDiscImage: () -> Void
    let openBluRayFolder: () -> Void
    let openSourceFolder: () -> Void
    let openMKV: () -> Void
    let importTransportStream: () -> Void
    let changeSource: () -> Void
    let chooseDestination: () -> Void
    let retryAnalysis: () -> Void
    let resolveRecoveryChoice: (WorkerRecoveryChoice) -> Void
    let retryBatchItem: (UUID, WorkerRecoveryChoice?) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let source {
                    selectedSourceSection(source)
                } else {
                    discFirstSourceSection
                }

                outputSection
                pipelineSection
                jobSummarySection
            }
            .padding(16)
        }
    }

    private var discFirstSourceSection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .top, spacing: 14) {
                    Image(systemName: "opticaldisc")
                        .font(.system(size: 38, weight: .light))
                        .symbolRenderingMode(.hierarchical)
                        .foregroundStyle(Color.accentColor)
                        .frame(width: 48)
                        .accessibilityHidden(true)

                    VStack(alignment: .leading, spacing: 4) {
                        Text("Convert a 3D Blu-ray Disc")
                            .font(.title3.weight(.semibold))
                        Text("The app finds the main MVC title, prepares both eyes, and creates a spatial movie for Apple Vision Pro.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }

                Divider()

                HStack(spacing: 8) {
                    Label(
                        makeMKVAvailable ? "MakeMKV ready" : "MakeMKV is required for discs and images",
                        systemImage: makeMKVAvailable ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
                    )
                    .font(.callout)
                    .foregroundStyle(makeMKVAvailable ? Color.green : Color.orange)
                    Spacer()
                    if !makeMKVAvailable, let downloadURL = DiscSourceDetector.makeMKVDownloadURL {
                        Link("Download MakeMKV…", destination: downloadURL)
                    }
                    Button("Refresh Drives", action: refreshDiscs)
                }

                if let disc = insertedDiscs.first {
                    HStack(spacing: 10) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(disc.displayName)
                                .fontWeight(.medium)
                            Text("3D title and MVC streams will be verified before conversion.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("Use Inserted Disc") {
                            useDisc(disc)
                        }
                        .buttonStyle(.borderedProminent)
                    }
                } else {
                    HStack(spacing: 10) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("No 3D Blu-ray disc detected")
                                .fontWeight(.medium)
                            Text("Insert a disc, then refresh the drive list.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("Use Inserted Disc") {}
                            .buttonStyle(.bordered)
                            .disabled(true)
                    }
                }

                Divider()

                Text("Other Sources")
                    .font(.headline)

                VStack(spacing: 8) {
                    HStack(spacing: 8) {
                        sourceButton("Open Disc Image…", systemImage: "opticaldiscdrive", action: openDiscImage)
                        sourceButton("Open Blu-ray Folder…", systemImage: "folder.badge.gearshape", action: openBluRayFolder)
                    }
                    HStack(spacing: 8) {
                        sourceButton("Open 3D MKV…", systemImage: "film.stack", action: openMKV)
                        sourceButton("Open Source Folder…", systemImage: "folder.stack", action: openSourceFolder)
                    }
                }

                Button("Import MTS or M2TS transport stream…", action: importTransportStream)
                    .buttonStyle(.link)
                    .font(.callout)
            }
            .padding(4)
        } label: {
            Label("Source", systemImage: "externaldrive")
                .font(.headline)
        }
    }

    private func selectedSourceSection(_ source: ConversionSource) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: source.kind.systemImage)
                        .font(.system(size: 28, weight: .medium))
                        .symbolRenderingMode(.hierarchical)
                        .foregroundStyle(Color.accentColor)
                        .frame(width: 38)
                        .accessibilityHidden(true)

                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 7) {
                            Text(source.displayName)
                                .font(.title3.weight(.semibold))
                                .lineLimit(1)
                            Text(source.kind.title)
                                .font(.caption.weight(.medium))
                                .foregroundStyle(.secondary)
                                .padding(.horizontal, 7)
                                .padding(.vertical, 2)
                                .background(Color.secondary.opacity(0.12), in: Capsule())
                        }
                        Text(source.locationDescription)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }

                    Spacer()
                    Button("Change…", action: changeSource)
                        .disabled(isBatchRunning || state.phase.isRunning || state.phase == .decisionRequired)
                }

                if source.kind.isSecondaryImport {
                    Label(
                        "Existing transport stream import — physical discs, images, folders, and MKV are the primary workflows.",
                        systemImage: "info.circle"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }

                if source.kind == .sourceFolder {
                    Divider()
                    batchQueueSection
                } else if state.phase.isRunning {
                    Divider()
                    HStack(spacing: 10) {
                        WorkerProgressGauge(progress: state.progress, width: 84)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(state.operationKind == .inspection ? "Reading source details…" : "Converting video…")
                                .fontWeight(.medium)
                            Text(
                                state.stageMessage
                                    ?? (state.operationKind == .inspection ? "Inspecting video streams" : "Processing video")
                            )
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            if let progress = state.progress {
                                Text(progress.detailText)
                                    .font(.caption2.monospacedDigit())
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                } else if state.phase == .decisionRequired, let decision = state.recoveryDecision {
                    Divider()
                    recoveryDecisionSection(decision)
                } else if state.phase == .failed {
                    Divider()
                    Label(
                        state.failureMessage
                            ?? (state.operationKind == .inspection
                                ? "The source could not be analyzed."
                                : "The source could not be converted."),
                        systemImage: "exclamationmark.triangle.fill"
                    )
                    .foregroundStyle(.red)
                    if let details = state.failureDetails, !details.isEmpty {
                        Text(details)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                    if state.operationKind == .inspection, state.failureRetryable {
                        Button("Analyze Again", action: retryAnalysis)
                    }
                    if state.failureCode == "makemkv_missing",
                       let downloadURL = DiscSourceDetector.makeMKVDownloadURL
                    {
                        Link("Download MakeMKV…", destination: downloadURL)
                    }
                } else if let result = state.result {
                    Divider()
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 22) {
                            SourceFact(label: "Resolution", value: result.resolution)
                            SourceFact(label: "Frame Rate", value: result.frameRate)
                            SourceFact(label: "Scan", value: result.scanDescription)
                            SourceFact(label: "Size", value: result.formattedSize)
                            Spacer()
                        }
                        Grid(alignment: .leading, horizontalSpacing: 22, verticalSpacing: 8) {
                            GridRow {
                                SourceFact(label: "Resolution", value: result.resolution)
                                SourceFact(label: "Frame Rate", value: result.frameRate)
                            }
                            GridRow {
                                SourceFact(label: "Scan", value: result.scanDescription)
                                SourceFact(label: "Size", value: result.formattedSize)
                            }
                            GridRow {
                                SourceFact(label: "Duration", value: result.formattedDuration)
                                Color.clear
                            }
                        }
                    }
                } else if source.kind.isDiscWorkflow {
                    Divider()
                    LabeledContent("3D title") {
                        Text("Longest MVC title — selected automatically")
                            .foregroundStyle(.secondary)
                    }
                    LabeledContent("Disc analysis") {
                        Text("Verified before processing starts")
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(4)
        } label: {
            Label("Source", systemImage: "externaldrive")
                .font(.headline)
        }
    }

    @ViewBuilder
    private var batchQueueSection: some View {
        if let batchQueue {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 8) {
                    Label(batchQueue.summaryText, systemImage: "list.bullet.rectangle")
                        .font(.callout.weight(.medium))
                    Spacer()
                    if !batchQueue.items.isEmpty {
                        Text(batchQueue.countsText)
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }

                if batchQueue.items.isEmpty {
                    Label(
                        "No ISO, MKV, MTS, or M2TS sources were found in this folder.",
                        systemImage: "folder.badge.questionmark"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                } else {
                    VStack(spacing: 0) {
                        ForEach(batchQueue.items) { item in
                            batchQueueRow(item, queue: batchQueue)
                            if item.id != batchQueue.items.last?.id {
                                Divider()
                            }
                        }
                    }
                    .background(Color.secondary.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
                }
            }
        } else {
            Label("Choose a source folder to build a conversion queue.", systemImage: "folder.stack")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    private func batchQueueRow(
        _ item: ConversionQueueItem,
        queue: ConversionQueueState
    ) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: item.status.systemImage)
                .foregroundStyle(batchStatusColor(item.status))
                .frame(width: 18)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 2) {
                Text(item.source.displayName)
                    .font(.callout.weight(.medium))
                    .lineLimit(1)
                Text(batchItemDetail(item, queue: queue))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .truncationMode(.middle)
                if queue.activeItemID == item.id, let progress = state.progress {
                    Text(progress.detailText)
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }

            Spacer(minLength: 8)

            if item.canRetry, !queue.isRunning {
                batchRetryControl(item)
            } else {
                Text(item.status.title)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(batchStatusColor(item.status))
                    .accessibilityLabel("\(item.source.displayName), \(item.status.title)")
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
    }

    @ViewBuilder
    private func batchRetryControl(_ item: ConversionQueueItem) -> some View {
        let recoveryChoices = item.recoveryDecision?.supportedChoices.filter { $0 != .cancel } ?? []
        if item.recoveryDecision == nil {
            Button("Retry") {
                retryBatchItem(item.id, nil)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .accessibilityLabel("Retry \(item.source.displayName)")
        } else if !recoveryChoices.isEmpty {
            Menu("Retry…") {
                ForEach(recoveryChoices) { choice in
                    Button(choice.title) {
                        retryBatchItem(item.id, choice)
                    }
                }
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .accessibilityLabel("Retry \(item.source.displayName)")
        } else {
            Text("Needs Attention")
                .font(.caption.weight(.medium))
                .foregroundStyle(.orange)
        }
    }

    private func batchItemDetail(
        _ item: ConversionQueueItem,
        queue: ConversionQueueState
    ) -> String {
        if queue.activeItemID == item.id {
            return state.stageMessage
                ?? (item.status == .inspecting ? "Reading source details" : "Processing video")
        }
        if let failureMessage = item.failureMessage {
            return failureMessage
        }
        if let outputPath = item.conversionResult?.outputPath {
            return outputPath
        }
        return item.source.url.deletingLastPathComponent().path
    }

    private func batchStatusColor(_ status: ConversionQueueItemStatus) -> Color {
        switch status {
        case .completed:
            .green
        case .failed:
            .red
        case .stopping, .stopped:
            .orange
        case .inspecting, .converting:
            .blue
        case .pending, .notStarted:
            .secondary
        }
    }

    private var outputSection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                LabeledContent("Destination") {
                    HStack(spacing: 8) {
                        Text(destinationURL.path)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Button("Choose…", action: chooseDestination)
                            .disabled(outputControlsLocked)
                    }
                }

                LabeledContent("Output") {
                    Text("Full Movie")
                        .foregroundStyle(.secondary)
                }

                if let plannedOutputURL {
                    Divider()
                    LabeledContent("Planned file") {
                        Text(plannedOutputURL.lastPathComponent)
                            .font(.callout.monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .textSelection(.enabled)
                    }
                }
            }
            .padding(4)
        } label: {
            Label("Output", systemImage: "arrow.down.doc")
                .font(.headline)
        }
    }

    private var jobSummarySection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                LabeledContent("Profile") {
                    HStack(spacing: 6) {
                        Text(profile.name)
                        if profileModified {
                            Text("Modified")
                                .font(.caption.weight(.medium))
                                .foregroundStyle(.orange)
                        }
                    }
                }
                LabeledContent("Video") {
                    Text(options.compactSummary)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.trailing)
                }
                LabeledContent("Subtitles") {
                    Text(subtitleSummary)
                        .foregroundStyle(.secondary)
                }
                LabeledContent("Start stage") {
                    Text(options.job.startStage.title)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(4)
        } label: {
            Label("Current Job", systemImage: "checklist")
                .font(.headline)
        }
    }

    private var pipelineSection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 5) {
                    PipelineStep(systemImage: "opticaldisc", title: "1 Create MKV")
                    pipelineChevron
                    PipelineStep(systemImage: "rectangle.split.2x1", title: "2–4 Extract 3D")
                    pipelineChevron
                    PipelineStep(systemImage: "visionpro", title: "5–6 Encode")
                    pipelineChevron
                    PipelineStep(systemImage: "checkmark.circle", title: "7–9 Finish")
                }
                Text("Nine restartable stages are available under Files & Recovery.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(4)
        } label: {
            Label("Conversion Pipeline", systemImage: "point.3.connected.trianglepath.dotted")
                .font(.headline)
        }
    }

    private var pipelineChevron: some View {
        Image(systemName: "chevron.right")
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.secondary)
            .accessibilityHidden(true)
    }

    private var subtitleSummary: String {
        guard options.encoding.includeSubtitles else {
            return "Skipped"
        }
        return options.encoding.keepExtraLanguages
            ? "\(options.encoding.language.name) preferred; keep others"
            : "\(options.encoding.language.name) only"
    }

    private var outputControlsLocked: Bool {
        isBatchRunning || state.phase.isRunning || state.phase == .decisionRequired
    }

    private func recoveryDecisionSection(_ decision: WorkerDecision) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(decision.prompt, systemImage: "arrow.clockwise.circle.fill")
                .font(.callout.weight(.semibold))
                .foregroundStyle(.orange)
            if let details = decision.details, !details.isEmpty {
                Text(details)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            HStack(spacing: 8) {
                ForEach(decision.supportedChoices) { choice in
                    recoveryButton(choice)
                }
            }
        }
    }

    @ViewBuilder
    private func recoveryButton(_ choice: WorkerRecoveryChoice) -> some View {
        switch choice {
        case .cancel:
            Button(choice.title, role: .cancel) {
                resolveRecoveryChoice(choice)
            }
            .buttonStyle(.bordered)
            .accessibilityHint(choice.accessibilityHint)
        case .retryContinueOnError, .retryWithoutSubtitles:
            Button(choice.title) {
                resolveRecoveryChoice(choice)
            }
            .buttonStyle(.borderedProminent)
            .accessibilityHint(choice.accessibilityHint)
        }
    }

    private func sourceButton(_ title: String, systemImage: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .buttonStyle(.bordered)
    }
}

private struct SourceFact: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.callout.weight(.medium))
                .textSelection(.enabled)
        }
    }
}

private struct PipelineStep: View {
    let systemImage: String
    let title: String

    var body: some View {
        VStack(spacing: 3) {
            Image(systemName: systemImage)
                .font(.callout)
                .foregroundStyle(Color.accentColor)
                .accessibilityHidden(true)
            Text(title)
                .font(.caption2.weight(.medium))
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }
}

import SwiftUI

struct SourceWorkspaceView: View {
    let source: ConversionSource?
    let state: WorkerLifecycleState
    let insertedDiscs: [ConversionSource]
    let makeMKVAvailable: Bool
    let profile: EncodingProfile
    let options: ConversionOptions
    let profileModified: Bool
    let titleSelection: DiscTitleSelection
    let titleSelectionSummary: String
    let selectedVideoCount: Int
    let queueItems: [ConversionQueueItem]
    @Binding var destinationURL: URL
    let plannedOutputURLs: [URL]
    let refreshDiscs: () -> Void
    let useDisc: (ConversionSource) -> Void
    let openDiscImage: () -> Void
    let openBluRayFolder: () -> Void
    let openMKV: () -> Void
    let importTransportStream: () -> Void
    let changeSource: () -> Void
    let chooseDestination: () -> Void
    let retryAnalysis: () -> Void
    let resolveRecoveryChoice: (WorkerRecoveryChoice) -> Void
    let selectMainTitle: () -> Void
    let selectAllTitles: () -> Void
    let chooseTitles: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let source {
                    selectedSourceSection(source)
                } else {
                    discFirstSourceSection
                }

                if queueItems.count > 1 {
                    ConversionQueueSection(items: queueItems)
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
                    sourceButton("Open 3D MKV…", systemImage: "film.stack", action: openMKV)
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
                        .disabled(state.phase.isRunning || state.phase == .decisionRequired)
                }

                if source.kind.isSecondaryImport {
                    Label(
                        "Existing transport stream import — physical discs, images, folders, and MKV are the primary workflows.",
                        systemImage: "info.circle"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }

                if state.phase.isRunning {
                    Divider()
                    HStack(spacing: 10) {
                        ProgressView()
                            .controlSize(.small)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(state.operationKind == .inspection ? "Reading source details…" : "Converting video…")
                                .fontWeight(.medium)
                            Text(
                                state.stageMessage
                                    ?? (state.operationKind == .inspection ? "Inspecting video streams" : "Processing video")
                            )
                                .font(.caption)
                                .foregroundStyle(.secondary)
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
                    if state.failureRetryable,
                       state.operationKind == .inspection || state.failureCode == "title_unavailable"
                    {
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
                    if result.titles.count > 1 {
                        Divider()
                        titleSelectionSection(result)
                    } else if let mainTitle = result.mainTitle {
                        Divider()
                        LabeledContent("3D video") {
                            Text("\(mainTitle.name) · \(mainTitle.formattedDuration)")
                                .foregroundStyle(.secondary)
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
                } else if source.kind == .sourceFolder {
                    Divider()
                    Label("Supported sources in this folder will be processed in sequence.", systemImage: "list.bullet.rectangle")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(4)
        } label: {
            Label("Source", systemImage: "externaldrive")
                .font(.headline)
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
                    Text(selectedVideoCount > 1 ? "\(selectedVideoCount) full videos" : "Full Movie")
                        .foregroundStyle(.secondary)
                }

                if plannedOutputURLs.count == 1, let plannedOutputURL = plannedOutputURLs.first {
                    Divider()
                    LabeledContent("Planned file") {
                        Text(plannedOutputURL.lastPathComponent)
                            .font(.callout.monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .textSelection(.enabled)
                    }
                } else if plannedOutputURLs.count > 1 {
                    Divider()
                    LabeledContent("Planned files") {
                        Text("\(plannedOutputURLs.count) files")
                            .foregroundStyle(.secondary)
                    }
                    Text("\(plannedOutputURLs[0].lastPathComponent) and \(plannedOutputURLs.count - 1) more")
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
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
                if selectedVideoCount > 1 {
                    LabeledContent("3D videos") {
                        Text(titleSelectionSummary)
                            .foregroundStyle(.secondary)
                    }
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
        state.phase.isRunning || state.phase == .decisionRequired
    }

    private func titleSelectionSection(_ result: SourceInspection) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            LabeledContent("Convert") {
                Menu {
                    Button(action: selectMainTitle) {
                        selectionOption("Main Movie", selected: titleSelection.isMain)
                    }
                    Button(action: selectAllTitles) {
                        selectionOption(
                            "All Detected 3D Videos (\(result.titles.count))",
                            selected: titleSelection.isAll
                        )
                    }
                    Divider()
                    Button(action: chooseTitles) {
                        selectionOption("Choose Videos…", selected: titleSelection.isCustom)
                    }
                } label: {
                    HStack(spacing: 5) {
                        Text(titleSelectionSummary)
                        Image(systemName: "chevron.down")
                            .font(.caption2.weight(.semibold))
                    }
                }
                .menuStyle(.borderlessButton)
                .disabled(outputControlsLocked)
                .accessibilityLabel("3D videos to convert: \(titleSelectionSummary)")
            }
            Text("\(result.titles.count) compatible 3D videos detected. Multiple selections are converted one at a time.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder
    private func selectionOption(_ title: String, selected: Bool) -> some View {
        if selected {
            Label(title, systemImage: "checkmark")
        } else {
            Text(title)
        }
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

private struct ConversionQueueSection: View {
    let items: [ConversionQueueItem]
    @State private var isExpanded = false

    var body: some View {
        GroupBox {
            DisclosureGroup(isExpanded: $isExpanded) {
                VStack(spacing: 0) {
                    ForEach(items.indices, id: \.self) { index in
                        let item = items[index]
                        HStack(spacing: 9) {
                            statusIcon(item.status)
                                .frame(width: 18)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(item.displayName)
                                    .font(.callout.weight(.medium))
                                    .lineLimit(1)
                                Text(outputText(item))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                            }
                            Spacer()
                            Text(statusText(item.status))
                                .font(.caption)
                                .foregroundStyle(statusColor(item.status))
                        }
                        .padding(.vertical, 7)
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel("\(item.displayName), \(statusText(item.status))")
                        if index < items.index(before: items.endIndex) {
                            Divider()
                        }
                    }
                }
                .padding(.top, 6)
            } label: {
                HStack {
                    Text(queueSummary)
                        .font(.callout.weight(.medium))
                    Spacer()
                    Text("\(completedCount)/\(items.count)")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
            .padding(4)
        } label: {
            Label("Conversion Queue", systemImage: "list.number")
                .font(.headline)
        }
    }

    private var completedCount: Int {
        items.count { item in
            if case .completed = item.status { return true }
            return false
        }
    }

    private var queueSummary: String {
        if items.allSatisfy({ if case .completed = $0.status { return true }; return false }) {
            return "All videos complete"
        }
        if items.contains(where: { if case .attention = $0.status { return true }; return false }) {
            return "Queue needs a decision"
        }
        if items.contains(where: { if case .failed = $0.status { return true }; return false }) {
            return "Queue stopped"
        }
        if items.contains(where: { if case .cancelled = $0.status { return true }; return false }) {
            return completedCount > 0
                ? "Queue stopped after \(completedCount) complete"
                : "Queue cancelled"
        }
        if items.allSatisfy({ if case .waiting = $0.status { return true }; return false }) {
            return "\(items.count) videos ready"
        }
        return "Converting \(items.count) videos"
    }

    @ViewBuilder
    private func statusIcon(_ status: ConversionQueueItemStatus) -> some View {
        switch status {
        case .waiting:
            Image(systemName: "clock")
                .foregroundStyle(.secondary)
        case .processing:
            ProgressView()
                .controlSize(.mini)
        case .attention:
            Image(systemName: "exclamationmark.circle.fill")
                .foregroundStyle(.orange)
        case .completed:
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
        case .failed:
            Image(systemName: "xmark.circle.fill")
                .foregroundStyle(.red)
        case .cancelled:
            Image(systemName: "minus.circle")
                .foregroundStyle(.secondary)
        }
    }

    private func statusText(_ status: ConversionQueueItemStatus) -> String {
        switch status {
        case .waiting:
            "Waiting"
        case .processing:
            "Converting"
        case .attention:
            "Decision needed"
        case .completed:
            "Complete"
        case .failed:
            "Failed"
        case .cancelled:
            "Cancelled"
        }
    }

    private func statusColor(_ status: ConversionQueueItemStatus) -> Color {
        switch status {
        case .attention:
            .orange
        case .failed:
            .red
        case .completed:
            .green
        default:
            .secondary
        }
    }

    private func outputText(_ item: ConversionQueueItem) -> String {
        switch item.status {
        case .completed(let result):
            result.outputURL.lastPathComponent
        case .attention(let message), .failed(let message):
            message
        default:
            item.plannedOutputURL.lastPathComponent
        }
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

import AppKit
import SwiftUI

struct PersistentQueueSidebarView: View {
    let items: [PersistentQueueItem]
    @Binding var selectedID: UUID?
    @Binding var compactRows: Bool
    let insertedDiscs: [ConversionSource]
    let makeMKVAvailable: Bool
    let activeProgress: WorkerProgress?
    let activeElapsedText: String?
    let runState: PersistentQueueRunState
    let canStart: Bool
    let canPauseAfterCurrent: Bool
    let canStopCurrent: Bool
    let canUndo: Bool
    let offPeakSchedule: OffPeakQueueSchedule?
    let offPeakScheduleOutcome: OffPeakScheduleOutcome?
    let offPeakScheduleErrorMessage: String?
    let addSources: () -> Void
    let addSourceFolder: () -> Void
    let addDisc: (ConversionSource) -> Void
    let move: (UUID, Int) -> Void
    let moveNext: (UUID) -> Void
    let remove: (UUID) -> Void
    let clearCompleted: () -> Void
    let undo: () -> Void
    let start: () -> Void
    let startLater: () -> Void
    let editSchedule: () -> Void
    let cancelSchedule: () -> Void
    let dismissScheduleOutcome: () -> Void
    let pauseAfterCurrent: () -> Void
    let stopCurrent: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            scheduleBanner
            if let banner {
                Label(banner.title, systemImage: banner.systemImage)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(banner.tint)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 9)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(banner.tint.opacity(0.08))
                    .accessibilityIdentifier("persistent-queue-banner")
            }
            if items.isEmpty {
                emptyState
            } else {
                queueList
            }
            Divider()
            actionBar
        }
        .background(Color(nsColor: .controlBackgroundColor))
        .accessibilityIdentifier("persistent-queue-sidebar")
    }

    @ViewBuilder
    private var scheduleBanner: some View {
        if let errorMessage = offPeakScheduleErrorMessage {
            Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                .font(.caption.weight(.medium))
                .foregroundStyle(.red)
                .padding(.horizontal, 12)
                .padding(.vertical, 9)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.red.opacity(0.08))
                .accessibilityIdentifier("off-peak-schedule-error")
        } else if let schedule = offPeakSchedule {
            VStack(alignment: .leading, spacing: 4) {
                Label(
                    "Scheduled \(schedule.startAt.formatted(date: .abbreviated, time: .shortened))–\(schedule.endAt.formatted(date: .omitted, time: .shortened))",
                    systemImage: "clock.badge.checkmark.fill"
                )
                    .font(.caption.weight(.semibold))
                Text("Keep the app open. The Mac is not kept awake or woken automatically.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            .foregroundStyle(Color.accentColor)
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.accentColor.opacity(0.08))
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("off-peak-schedule-banner")
        } else if let outcome = offPeakScheduleOutcome, outcome.kind == .missed {
            HStack(alignment: .top, spacing: 8) {
                Label(outcome.message, systemImage: "clock.badge.exclamationmark.fill")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.orange)
                    .accessibilityIdentifier("off-peak-schedule-missed-message")
                Spacer(minLength: 4)
                Button(action: dismissScheduleOutcome) {
                    Image(systemName: "xmark")
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Dismiss missed schedule notice")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .background(Color.orange.opacity(0.08))
            .accessibilityIdentifier("off-peak-schedule-missed")
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text("Queue")
                .font(.headline)
            Spacer()
            if !items.isEmpty {
                Text("\(items.count)")
                    .font(.caption.monospacedDigit().weight(.medium))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(Color.secondary.opacity(0.12), in: Capsule())
            }
            Menu {
                Toggle("Compact Queue", isOn: $compactRows)
                Divider()
                Button("Clear Completed", action: clearCompleted)
                    .disabled(completedItems.isEmpty)
                if canUndo {
                    Button("Undo Remove", action: undo)
                }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .menuStyle(.borderlessButton)
            .accessibilityLabel("Queue options")
        }
        .padding(.horizontal, 14)
        .frame(height: 44)
        .background { StructuralChromeBackground() }
    }

    private var emptyState: some View {
        VStack(spacing: 14) {
            Spacer()
            Image(systemName: "opticaldisc")
                .font(.system(size: 42, weight: .light))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(Color.accentColor)
                .accessibilityHidden(true)
            Text("Nothing in the queue")
                .font(.title3.weight(.semibold))
            Text("Add files, disc images, Blu-ray folders, movie folders, or an inserted disc.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button("Add Sources…", action: addSources)
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .accessibilityIdentifier("queue-empty-add")
            Label(
                makeMKVAvailable ? "MakeMKV ready" : "MakeMKV is required for disc sources",
                systemImage: makeMKVAvailable ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
            )
                .font(.caption)
                .foregroundStyle(makeMKVAvailable ? .green : .orange)
            Text("Videos convert one at a time in the order shown.")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var queueList: some View {
        List(selection: $selectedID) {
            if !activeItems.isEmpty {
                Section("Now Converting") {
                    rows(activeItems)
                }
            }
            if !pendingItems.isEmpty {
                Section(activeItems.isEmpty ? "Queue" : "Up Next") {
                    rows(pendingItems)
                }
            }
            if !completedItems.isEmpty {
                Section("Completed") {
                    rows(completedItems)
                }
            }
        }
        .listStyle(.sidebar)
        .accessibilityLabel("Conversion queue")
        .accessibilityIdentifier("persistent-queue-list")
    }

    @ViewBuilder
    private func rows(_ sectionItems: [PersistentQueueItem]) -> some View {
        ForEach(sectionItems) { item in
            PersistentQueueRow(
                item: item,
                position: (items.firstIndex(where: { $0.id == item.id }) ?? 0) + 1,
                total: items.count,
                compact: compactRows,
                progress: item.status.isActive ? activeProgress : nil,
                move: move,
                moveNext: moveNext,
                remove: remove
            )
            .tag(item.id)
        }
    }

    private var actionBar: some View {
        VStack(spacing: 7) {
            HStack(spacing: 8) {
                Menu {
                    Button("Add Files or Folders…", action: addSources)
                    Button("Add Folder of Movies…", action: addSourceFolder)
                    if !insertedDiscs.isEmpty {
                        Divider()
                        ForEach(insertedDiscs, id: \.url) { disc in
                            Button("Add \(disc.displayName)") { addDisc(disc) }
                        }
                    }
                } label: {
                    Label("Add", systemImage: "plus")
                }
                .menuStyle(.borderlessButton)
                .accessibilityIdentifier("persistent-queue-add")

                Button {
                    if let selectedID { remove(selectedID) }
                } label: {
                    Label("Remove", systemImage: "minus")
                }
                .buttonStyle(.borderless)
                .disabled(selectedItem?.canRemove != true)
                .accessibilityIdentifier("persistent-queue-remove")

                Spacer()
                if canUndo {
                    Button("Undo", action: undo)
                        .buttonStyle(.borderless)
                }
                if !completedItems.isEmpty {
                    Button("Clear Completed", action: clearCompleted)
                        .buttonStyle(.borderless)
                }
            }
            VStack(alignment: .trailing, spacing: 8) {
                HStack(spacing: 8) {
                    Button(offPeakSchedule == nil ? startButtonTitle : "Start Now", action: start)
                        .buttonStyle(.borderedProminent)
                        .disabled(!canStart)
                        .frame(maxWidth: .infinity)
                        .accessibilityIdentifier("persistent-queue-start")
                        .accessibilityLabel(offPeakSchedule == nil ? startButtonTitle : "Start Now")
                    if offPeakSchedule == nil {
                        Button("Start Later…", action: startLater)
                            .disabled(!canStart)
                            .accessibilityIdentifier("off-peak-schedule-create")
                    }
                }
                if offPeakSchedule != nil {
                    HStack(spacing: 8) {
                        Button("Edit Schedule…", action: editSchedule)
                            .accessibilityIdentifier("off-peak-schedule-edit")
                        Button("Cancel Schedule", role: .destructive, action: cancelSchedule)
                            .accessibilityIdentifier("off-peak-schedule-cancel")
                    }
                }
                HStack(spacing: 8) {
                    Button("Pause After Current", action: pauseAfterCurrent)
                        .disabled(!canPauseAfterCurrent)
                        .accessibilityIdentifier("persistent-queue-pause-after-current")
                        .accessibilityLabel("Pause queue after the current video")
                    Button("Stop Current", role: .destructive, action: stopCurrent)
                        .disabled(!canStopCurrent)
                        .accessibilityIdentifier("persistent-queue-stop-current")
                        .accessibilityLabel("Stop only the current video")
                }
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background { StructuralChromeBackground() }
    }

    private var activeItems: [PersistentQueueItem] { items.filter(\.status.isActive) }
    private var completedItems: [PersistentQueueItem] { items.filter(\.status.isCompleted) }
    private var pendingItems: [PersistentQueueItem] {
        items.filter { !$0.status.isActive && !$0.status.isCompleted }
    }
    private var selectedItem: PersistentQueueItem? {
        selectedID.flatMap { id in items.first(where: { $0.id == id }) }
    }
    private var startButtonTitle: String {
        runState == .paused || items.contains(where: { $0.isRestored || $0.status.isStopped })
            ? "Resume Queue"
            : "Start Queue"
    }

    private var banner: (title: String, systemImage: String, tint: Color)? {
        switch runState {
        case .pauseAfterCurrent:
            return ("Pause requested — the current video will finish before the queue pauses.", "pause.circle.fill", .orange)
        case .paused:
            return ("Queue paused — pending videos remain waiting until you resume.", "pause.circle.fill", .secondary)
        case .idle, .running:
            break
        }
        if items.contains(where: \.isRestored) {
            return ("Queue restored — interrupted videos require an explicit restart.", "arrow.counterclockwise.circle.fill", .orange)
        }
        if items.contains(where: { item in
            switch item.status {
            case .attention, .failed:
                true
            default:
                false
            }
        }) {
            return ("Some videos need attention.", "exclamationmark.triangle.fill", .red)
        }
        return nil
    }
}

struct OffPeakScheduleSheet: View {
    @Binding var startAt: Date
    @Binding var endAt: Date
    let isEditing: Bool
    let hasPhysicalDiscItems: Bool
    let errorMessage: String?
    let cancel: () -> Void
    let save: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 6) {
                Text(isEditing ? "Edit Scheduled Window" : "Start Queue Later")
                    .font(.title2.weight(.semibold))
                Text("The app must remain open. This schedule does not wake the Mac or keep it awake.")
                    .foregroundStyle(.secondary)
            }

            Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 12) {
                GridRow {
                    Text("Start")
                    DatePicker("Start", selection: $startAt, displayedComponents: [.date, .hourAndMinute])
                        .labelsHidden()
                        .accessibilityIdentifier("off-peak-start-date")
                }
                GridRow {
                    Text("Stop starting new videos")
                    DatePicker(
                        "Stop starting new videos",
                        selection: $endAt,
                        displayedComponents: [.date, .hourAndMinute]
                    )
                        .labelsHidden()
                        .accessibilityIdentifier("off-peak-end-date")
                }
            }

            Text("If the Mac sleeps, the queue can start late only while this window is still open. Reopening the app after the start time marks the schedule missed. A video already running may finish after the window ends.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if hasPhysicalDiscItems {
                Label(
                    "Physical-disc items run only if the same disc is inserted when the window opens. Missing discs are parked so other available videos can continue.",
                    systemImage: "opticaldisc.fill"
                )
                    .font(.callout)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("off-peak-disc-warning")
            }

            if let errorMessage {
                Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout)
                    .foregroundStyle(.red)
                    .accessibilityIdentifier("off-peak-editor-error")
            }

            HStack {
                Spacer()
                Button("Cancel", action: cancel)
                    .keyboardShortcut(.cancelAction)
                Button(isEditing ? "Save Changes" : "Schedule Queue", action: save)
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .accessibilityIdentifier("off-peak-schedule-save")
            }
        }
        .padding(24)
        .frame(width: 560)
        .accessibilityIdentifier("off-peak-schedule-sheet")
    }
}

private struct PersistentQueueRow: View {
    let item: PersistentQueueItem
    let position: Int
    let total: Int
    let compact: Bool
    let progress: WorkerProgress?
    let move: (UUID, Int) -> Void
    let moveNext: (UUID) -> Void
    let remove: (UUID) -> Void

    var body: some View {
        HStack(spacing: compact ? 7 : 9) {
            Image(systemName: item.status.systemImage)
                .font(.system(size: compact ? 13 : 15, weight: .medium))
                .foregroundStyle(item.status.tint)
                .frame(width: compact ? 15 : 18)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: compact ? 1 : 3) {
                Text(item.displayName)
                    .font(compact ? .callout : .callout.weight(.medium))
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(item.status.subtitle(for: item))
                    .font(.caption)
                    .foregroundStyle(item.status.tint)
                    .lineLimit(1)
                    .truncationMode(.middle)
                if item.status.isActive, let progress {
                    if let stageFraction = progress.stageFraction {
                        ProgressView(value: stageFraction)
                            .progressViewStyle(.linear)
                            .controlSize(.small)
                            .accessibilityLabel(progress.accessibilityValue)
                    } else {
                        ProgressView()
                            .controlSize(.small)
                            .accessibilityLabel(progress.accessibilityValue)
                    }
                }
            }
            Spacer(minLength: 4)
            if item.isRestored {
                Image(systemName: "arrow.counterclockwise.circle.fill")
                    .foregroundStyle(.orange)
                    .accessibilityHidden(true)
            }
        }
        .padding(.vertical, compact ? 2 : 4)
        .contentShape(Rectangle())
        .contextMenu {
            if item.canMove {
                Button("Convert Next") { moveNext(item.id) }
                Button("Move Up") { move(item.id, -1) }
                Button("Move Down") { move(item.id, 1) }
                Divider()
            }
            Button("Remove from Queue", role: .destructive) { remove(item.id) }
                .disabled(!item.canRemove)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(item.displayName), \(item.status.title), \(position) of \(total)")
        .accessibilityAction(named: "Move Up") {
            if item.canMove { move(item.id, -1) }
        }
        .accessibilityAction(named: "Move Down") {
            if item.canMove { move(item.id, 1) }
        }
        .accessibilityAction(named: "Remove from Queue") {
            if item.canRemove { remove(item.id) }
        }
    }
}

struct PersistentQueueDetailView: View {
    let item: PersistentQueueItem?
    let activeProgress: WorkerProgress?
    let activeElapsedText: String?
    let edit: (PersistentQueueItem) -> Void
    let changeDestination: (PersistentQueueItem) -> Void
    let retry: (PersistentQueueItem, WorkerRecoveryChoice?) -> Void

    var body: some View {
        Group {
            if let item {
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        header(item)
                        statusBanner(item)
                        Divider()
                        settings(item)
                    }
                    .padding(20)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            } else {
                VStack(spacing: 12) {
                    Image(systemName: "slider.horizontal.3")
                        .font(.system(size: 34, weight: .light))
                        .foregroundStyle(.secondary)
                    Text("Select a video to configure it")
                        .font(.title3.weight(.semibold))
                    Text("Each queued video keeps its own profile, destination, and recovery settings.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .accessibilityIdentifier("persistent-queue-detail")
    }

    private func header(_ item: PersistentQueueItem) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: item.draft.source.kind.queueSystemImage)
                .font(.system(size: 30, weight: .medium))
                .foregroundStyle(Color.accentColor)
                .frame(width: 36)
            VStack(alignment: .leading, spacing: 3) {
                Text(item.displayName)
                    .font(.title2.weight(.semibold))
                Text(item.draft.source.url.deletingLastPathComponent().path)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer()
            Text(item.draft.source.kind.title)
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(.quaternary, in: Capsule())
        }
    }

    @ViewBuilder
    private func statusBanner(_ item: PersistentQueueItem) -> some View {
        HStack(spacing: 10) {
            Label(item.status.detailTitle, systemImage: item.status.systemImage)
                .font(.callout.weight(.medium))
                .foregroundStyle(item.status.tint)
            Spacer()
            if item.status.isActive, let activeProgress {
                Text(activeProgress.compactText)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            if item.status.isActive, let activeElapsedText {
                Label(activeElapsedText, systemImage: "clock")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            if item.canRetry {
                if case let .attention(decision) = item.status, !decision.supportedChoices.isEmpty {
                    Menu("Choose Action") {
                        ForEach(decision.supportedChoices) { choice in
                            Button(choice.title) { retry(item, choice) }
                        }
                    }
                } else {
                    Button(item.isRestored ? "Restart Safely" : "Retry") { retry(item, nil) }
                }
            }
            if case let .completed(result) = item.status {
                Button("Reveal in Finder") {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: result.outputPath)])
                }
            }
        }
        .padding(10)
        .background(item.status.tint.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
    }

    private func settings(_ item: PersistentQueueItem) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Conversion Setup")
                        .font(.title3.weight(.semibold))
                    Text(item.isEditable ? "Waiting items can be edited until they start." : "Settings are locked for this queue state.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Edit…") { edit(item) }
                    .disabled(!item.isEditable)
            }
            Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 10) {
                GridRow {
                    Text("Profile").foregroundStyle(.secondary)
                    Text(item.draft.profile.name)
                }
                GridRow {
                    Text("Destination").foregroundStyle(.secondary)
                    HStack {
                        Text(item.draft.destinationURL.path)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Button("Change…") { changeDestination(item) }
                            .disabled(!item.isEditable)
                    }
                }
                GridRow {
                    Text("Planned file").foregroundStyle(.secondary)
                    Text(item.draft.proposedOutputURL.lastPathComponent)
                }
                GridRow {
                    Text("Output").foregroundStyle(.secondary)
                    Text(item.draft.options.videoRoutePlan.qualityTitle)
                }
                GridRow {
                    Text("Attempts").foregroundStyle(.secondary)
                    Text("\(item.attemptCount)")
                }
            }
            .font(.callout)
        }
    }
}

private extension PersistentQueueItemStatus {
    var isActive: Bool {
        switch self {
        case .inspecting, .processing, .stopping:
            true
        default:
            false
        }
    }

    var isCompleted: Bool {
        if case .completed = self { return true }
        return false
    }

    var isStopped: Bool {
        switch self {
        case .stopped, .notStarted:
            true
        default:
            false
        }
    }

    var title: String {
        switch self {
        case .waiting: "Waiting"
        case .inspecting: "Reading Source"
        case .processing: "Converting"
        case .stopping: "Stopping"
        case .interrupted: "Interrupted"
        case .attention: "Needs Attention"
        case .failed: "Failed"
        case .completed: "Completed"
        case .stopped: "Stopped"
        case .notStarted: "Not Started"
        }
    }

    var detailTitle: String {
        switch self {
        case let .attention(decision): decision.prompt
        case let .failed(failure): failure.message
        case let .completed(result): URL(fileURLWithPath: result.outputPath).lastPathComponent
        case .interrupted: "Interrupted during the previous app session"
        default: title
        }
    }

    var systemImage: String {
        switch self {
        case .waiting: "clock"
        case .inspecting: "doc.text.magnifyingglass"
        case .processing: "gearshape.2"
        case .stopping: "hourglass"
        case .interrupted: "arrow.counterclockwise.circle"
        case .attention: "exclamationmark.circle.fill"
        case .failed: "exclamationmark.triangle.fill"
        case .completed: "checkmark.circle.fill"
        case .stopped, .notStarted: "stop.circle.fill"
        }
    }

    var tint: Color {
        switch self {
        case .waiting, .stopped, .notStarted: .secondary
        case .inspecting, .processing, .stopping: .accentColor
        case .interrupted, .attention: .orange
        case .failed: .red
        case .completed: .green
        }
    }

    func subtitle(for item: PersistentQueueItem) -> String {
        switch self {
        case let .attention(decision): decision.prompt
        case let .failed(failure): failure.message
        case let .completed(result): URL(fileURLWithPath: result.outputPath).lastPathComponent
        case .interrupted: "Will restart from the last safe stage"
        case .processing: "\(item.draft.profile.name) · \(item.draft.proposedOutputURL.lastPathComponent)"
        default: "\(item.draft.profile.name) · \(item.draft.proposedOutputURL.lastPathComponent)"
        }
    }
}

private extension ConversionSourceKind {
    var queueSystemImage: String {
        switch self {
        case .discImage: "opticaldiscdrive"
        case .matroska: "film.stack"
        case .transportStream: "externaldrive"
        case .bluRayFolder, .sourceFolder: "folder.badge.gearshape"
        case .physicalDisc: "opticaldisc"
        }
    }
}

private extension DurableQueueDecision {
    var supportedChoices: [WorkerRecoveryChoice] {
        choices.compactMap(WorkerRecoveryChoice.init(rawValue:)).filter { $0 != .cancel }
    }
}

import AppKit
import SwiftUI
import UniformTypeIdentifiers

enum PersistentQueueMovePlacement: Equatable, Sendable {
    case before
    case after
}

private struct PersistentQueueDropInsertion: Equatable {
    let targetID: UUID
    let placement: PersistentQueueMovePlacement
}

private extension UTType {
    static let persistentQueueItem = UTType(
        exportedAs: "com.shinycomputers.bd-to-avp.queue-item",
        conformingTo: .data
    )
}

struct PersistentQueueSidebarView: View {
    @State private var dropInsertion: PersistentQueueDropInsertion?

    let items: [PersistentQueueItem]
    @Binding var selectedID: UUID?
    @Binding var compactRows: Bool
    let commandState: PersistentQueueCommandState
    let makeMKVAvailable: Bool
    let activeProgress: WorkerProgress?
    let activeElapsedText: String?
    let offPeakScheduleOutcome: OffPeakScheduleOutcome?
    let offPeakScheduleErrorMessage: String?
    let addSources: () -> Void
    let addSourceFolder: () -> Void
    let addDisc: (ConversionSource) -> Void
    let addDroppedURLs: ([URL], CGPoint) -> Bool
    let move: (UUID, Int) -> Void
    let moveRelative: (UUID, UUID, PersistentQueueMovePlacement) -> Void
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
        .onChange(of: items.map(\.id)) { _, _ in
            dropInsertion = nil
        }
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
        } else if let schedule = commandState.offPeakSchedule {
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
                if commandState.canUndo {
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
                canMoveUp: canMove(item, by: -1),
                canMoveDown: canMove(item, by: 1),
                canConvertNext: canMoveToNext(item),
                dropInsertion: $dropInsertion,
                addDroppedURLs: addDroppedURLs,
                move: move,
                moveRelative: moveRelative,
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
                    if !commandState.insertedDiscs.isEmpty {
                        Divider()
                        ForEach(commandState.insertedDiscs, id: \.url) { disc in
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
                .disabled(!commandState.canRemoveSelectedItem)
                .accessibilityIdentifier("persistent-queue-remove")

                Menu {
                    if let selectedItem {
                        if selectedItem.canMove {
                            Button("Move Up") { move(selectedItem.id, -1) }
                                .disabled(!canMove(selectedItem, by: -1))
                            Button("Move Down") { move(selectedItem.id, 1) }
                                .disabled(!canMove(selectedItem, by: 1))
                            Divider()
                            Button("Convert Next") { moveNext(selectedItem.id) }
                                .disabled(!canMoveToNext(selectedItem))
                        } else if let reason = selectedItem.queueManipulationLockReason {
                            Text(reason)
                        }
                    } else {
                        Text("Select a waiting item to arrange it.")
                    }
                } label: {
                    Label("Arrange", systemImage: "arrow.up.arrow.down")
                }
                .menuStyle(.borderlessButton)
                .accessibilityHint("Move the selected waiting item with Command-Option-Up Arrow or Command-Option-Down Arrow.")
                .accessibilityIdentifier("persistent-queue-arrange")

                Spacer()
                if commandState.canUndo {
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
                    Button(commandState.startTitle, action: start)
                        .buttonStyle(.borderedProminent)
                        .disabled(!commandState.canStart)
                        .frame(maxWidth: .infinity)
                        .accessibilityIdentifier("persistent-queue-start")
                        .accessibilityLabel(commandState.startTitle)
                    if commandState.offPeakSchedule == nil {
                        Button("Start Later…", action: startLater)
                            .disabled(!commandState.canStart)
                            .accessibilityIdentifier("off-peak-schedule-create")
                    }
                }
                if commandState.offPeakSchedule != nil {
                    HStack(spacing: 8) {
                        Button("Edit Schedule…", action: editSchedule)
                            .accessibilityIdentifier("off-peak-schedule-edit")
                        Button("Cancel Schedule", role: .destructive, action: cancelSchedule)
                            .accessibilityIdentifier("off-peak-schedule-cancel")
                    }
                }
                HStack(spacing: 8) {
                    Button("Pause After Current", action: pauseAfterCurrent)
                        .disabled(!commandState.canPauseAfterCurrent)
                        .accessibilityIdentifier("persistent-queue-pause-after-current")
                        .accessibilityLabel("Pause queue after the current video")
                    Button("Stop Current", role: .destructive, action: stopCurrent)
                        .disabled(!commandState.canStopCurrent)
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

    private var waitingItems: [PersistentQueueItem] {
        items.filter(\.canMove)
    }

    private func canMove(_ item: PersistentQueueItem, by offset: Int) -> Bool {
        guard let index = waitingItems.firstIndex(where: { $0.id == item.id }) else {
            return false
        }
        return waitingItems.indices.contains(index + offset)
    }

    private func canMoveToNext(_ item: PersistentQueueItem) -> Bool {
        waitingItems.first?.id != item.id
    }
    private var selectedItem: PersistentQueueItem? {
        selectedID.flatMap { id in items.first(where: { $0.id == id }) }
    }
    private var banner: (title: String, systemImage: String, tint: Color)? {
        switch commandState.runState {
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
            case .needsChoice, .attention, .failed:
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
    @State private var rowHeight: CGFloat = 0

    let item: PersistentQueueItem
    let position: Int
    let total: Int
    let compact: Bool
    let progress: WorkerProgress?
    let canMoveUp: Bool
    let canMoveDown: Bool
    let canConvertNext: Bool
    @Binding var dropInsertion: PersistentQueueDropInsertion?
    let addDroppedURLs: ([URL], CGPoint) -> Bool
    let move: (UUID, Int) -> Void
    let moveRelative: (UUID, UUID, PersistentQueueMovePlacement) -> Void
    let moveNext: (UUID) -> Void
    let remove: (UUID) -> Void

    var body: some View {
        interactiveRow
    }

    @ViewBuilder
    private var interactiveRow: some View {
        if item.canMove {
            baseRow
                .onDrag { queueItemProvider() }
                .background {
                    GeometryReader { proxy in
                        Color.clear
                            .onAppear { rowHeight = proxy.size.height }
                            .onChange(of: proxy.size) { _, size in
                                rowHeight = size.height
                            }
                    }
                }
                .onDrop(
                    of: [UTType.persistentQueueItem, UTType.fileURL],
                    delegate: PersistentQueueRowDropDelegate(
                        targetID: item.id,
                        rowHeight: rowHeight,
                        dropInsertion: $dropInsertion,
                        addDroppedURLs: addDroppedURLs,
                        moveRelative: moveRelative
                    )
                )
                .accessibilityAction(named: "Convert Next") {
                    if canConvertNext { moveNext(item.id) }
                }
                .accessibilityAction(named: "Move Up") {
                    if canMoveUp { move(item.id, -1) }
                }
                .accessibilityAction(named: "Move Down") {
                    if canMoveDown { move(item.id, 1) }
                }
        } else {
            baseRow
                .accessibilityHint(item.queueManipulationLockReason ?? "This item cannot move or be edited.")
        }
    }

    private func queueItemProvider() -> NSItemProvider {
        let provider = NSItemProvider()
        let itemData = Data(item.id.uuidString.utf8)
        provider.registerDataRepresentation(
            forTypeIdentifier: UTType.persistentQueueItem.identifier,
            visibility: .ownProcess
        ) { completion in
            completion(itemData, nil)
            return nil
        }
        return provider
    }

    private var baseRow: some View {
        HStack(spacing: compact ? 7 : 9) {
            Image(systemName: item.status.systemImage)
                .font(.system(size: compact ? 13 : 15, weight: .medium))
                .foregroundStyle(item.status.tint)
                .frame(width: compact ? 15 : 18)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: compact ? 1 : 3) {
                Text(item.sourceIdentity)
                    .font(compact ? .callout : .callout.weight(.medium))
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .accessibilityLabel("Source: \(item.sourceIdentity)")
                if compact {
                    Text("\(item.selectedTitleIdentity) · \(item.status.title)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .accessibilityLabel(
                            "Selected title: \(item.selectedTitleIdentity). State: \(item.status.title)"
                        )
                } else {
                    Text(item.selectedTitleIdentity)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .accessibilityLabel("Selected title: \(item.selectedTitleIdentity)")
                    HStack(spacing: 4) {
                        Text("\(item.sourceKindName) · \(item.draft.profile.name)")
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Text("· \(item.status.title)")
                            .foregroundStyle(item.status.tint)
                            .lineLimit(1)
                            .fixedSize(horizontal: true, vertical: false)
                            .layoutPriority(1)
                    }
                    .font(.caption)
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(
                        "Source kind: \(item.sourceKindName). Profile: \(item.draft.profile.name). "
                            + "State: \(item.status.title)"
                    )
                }
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
                Button("Move Up") { move(item.id, -1) }
                    .disabled(!canMoveUp)
                Button("Move Down") { move(item.id, 1) }
                    .disabled(!canMoveDown)
                Divider()
                Button("Convert Next") { moveNext(item.id) }
                    .disabled(!canConvertNext)
                Divider()
            } else if let reason = item.queueManipulationLockReason {
                Text(reason)
                Divider()
            }
            Button("Remove from Queue", role: .destructive) { remove(item.id) }
                .disabled(!item.canRemove)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            "Source: \(item.sourceIdentity). Selected title: \(item.selectedTitleIdentity). "
                + "Source kind: \(item.sourceKindName). Profile: \(item.draft.profile.name). "
                + "State: \(item.status.title). Queue item \(position) of \(total)."
        )
        .accessibilityAction(named: "Remove from Queue") {
            if item.canRemove { remove(item.id) }
        }
        .overlay(alignment: insertionAlignment) {
            if dropInsertion?.targetID == item.id {
                Color.accentColor
                    .frame(height: 2)
                    .padding(.horizontal, 4)
                    .accessibilityHidden(true)
            }
        }
        .animation(.easeInOut(duration: 0.15), value: dropInsertion)
    }

    private var insertionAlignment: Alignment {
        dropInsertion?.placement == .before ? .top : .bottom
    }
}

private struct PersistentQueueRowDropDelegate: DropDelegate {
    let targetID: UUID
    let rowHeight: CGFloat
    @Binding var dropInsertion: PersistentQueueDropInsertion?
    let addDroppedURLs: ([URL], CGPoint) -> Bool
    let moveRelative: (UUID, UUID, PersistentQueueMovePlacement) -> Void

    func validateDrop(info: DropInfo) -> Bool {
        info.hasItemsConforming(to: [UTType.persistentQueueItem, UTType.fileURL])
    }

    func dropEntered(info: DropInfo) {
        guard info.hasItemsConforming(to: [UTType.persistentQueueItem]) else {
            return
        }
        updateInsertion(for: info)
    }

    func dropExited(info _: DropInfo) {
        guard dropInsertion?.targetID == targetID else {
            return
        }
        dropInsertion = nil
    }

    func dropUpdated(info: DropInfo) -> DropProposal? {
        if info.hasItemsConforming(to: [UTType.persistentQueueItem]) {
            updateInsertion(for: info)
            return DropProposal(operation: .move)
        }
        dropInsertion = nil
        return info.hasItemsConforming(to: [UTType.fileURL])
            ? DropProposal(operation: .copy)
            : DropProposal(operation: .forbidden)
    }

    func performDrop(info: DropInfo) -> Bool {
        defer { dropInsertion = nil }
        if let provider = info.itemProviders(for: [UTType.persistentQueueItem]).first {
            return performQueueItemDrop(provider: provider, info: info)
        }
        return performFileURLDrop(info: info)
    }

    private func performQueueItemDrop(provider: NSItemProvider, info: DropInfo) -> Bool {
        guard validateDrop(info: info) else {
            return false
        }
        let targetID = targetID
        let targetPlacement = placement(for: info)
        let moveRelative = moveRelative
        provider.loadDataRepresentation(forTypeIdentifier: UTType.persistentQueueItem.identifier) { data, error in
            guard error == nil,
                  let data,
                  let value = String(data: data, encoding: .utf8),
                  let sourceID = UUID(uuidString: value),
                  sourceID != targetID
            else {
                return
            }
            DispatchQueue.main.async {
                moveRelative(sourceID, targetID, targetPlacement)
            }
        }
        return true
    }

    private func performFileURLDrop(info: DropInfo) -> Bool {
        guard info.hasItemsConforming(to: [UTType.fileURL]) else {
            return false
        }
        let pasteboardURLs = NSPasteboard(name: .drag)
            .readObjects(forClasses: [NSURL.self]) as? [NSURL] ?? []
        let fileURLs = pasteboardURLs.compactMap { $0.filePathURL as URL? }
        guard !fileURLs.isEmpty else {
            return false
        }
        return addDroppedURLs(fileURLs, info.location)
    }

    private func updateInsertion(for info: DropInfo) {
        dropInsertion = PersistentQueueDropInsertion(targetID: targetID, placement: placement(for: info))
    }

    private func placement(for info: DropInfo) -> PersistentQueueMovePlacement {
        let measuredHeight = rowHeight > 0 ? rowHeight : 44
        return info.location.y < measuredHeight / 2 ? .before : .after
    }
}

struct PersistentQueueDetailView: View {
    let item: PersistentQueueItem?
    let resolutionGroup: QueueResolutionGroup?
    @ObservedObject var resolutionMemoryStore: ResolutionMemoryStore
    let activeProgress: WorkerProgress?
    let activeElapsedText: String?
    let edit: (PersistentQueueItem) -> Void
    let changeDestination: (PersistentQueueItem) -> Void
    let retry: (PersistentQueueItem, WorkerRecoveryChoice?) -> Void
    let resolveRouteQuality: (QueueResolutionGroup, QueueResolutionSelection) -> Void

    var body: some View {
        Group {
            if let item {
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        header(item)
                        statusBanner(item)
                        if let resolutionGroup {
                            PersistentQueueRouteQualityResolutionView(
                                group: resolutionGroup,
                                memoryStore: resolutionMemoryStore,
                                apply: resolveRouteQuality
                            )
                        }
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
                Text(item.sourceIdentity)
                    .font(.title2.weight(.semibold))
                    .accessibilityLabel("Source: \(item.sourceIdentity)")
                Text(item.sourceLocation)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .accessibilityLabel("Source location: \(item.sourceLocation)")
            }
            Spacer()
            Text(item.sourceKindName)
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
            itemDetails(item)
            Divider()
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Conversion Setup")
                        .font(.title3.weight(.semibold))
                    Text(item.isEditable
                        ? "Waiting items can be edited until they start."
                        : (item.queueManipulationLockReason ?? "Settings are locked for this queue state."))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Edit…") { edit(item) }
                    .disabled(!item.isEditable)
                    .help(item.isEditable ? "Edit this waiting item." : (item.queueManipulationLockReason ?? "Settings are locked."))
            }
            Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 10) {
                GridRow {
                    Text("Profile").foregroundStyle(.secondary)
                    detailValue(item.draft.profile.name, label: "Profile")
                }
                GridRow {
                    Text("Destination").foregroundStyle(.secondary)
                    HStack {
                        detailValue(item.draft.destinationURL.path, label: "Destination")
                        Button("Change…") { changeDestination(item) }
                            .disabled(!item.isEditable)
                            .help(item.isEditable ? "Change this waiting item's destination." : (item.queueManipulationLockReason ?? "Settings are locked."))
                    }
                }
                GridRow {
                    Text("Planned file").foregroundStyle(.secondary)
                    detailValue(item.draft.proposedOutputURL.lastPathComponent, label: "Planned file")
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

    private func itemDetails(_ item: PersistentQueueItem) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Queue Item")
                .font(.title3.weight(.semibold))
            Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 10) {
                GridRow {
                    Text("Source").foregroundStyle(.secondary)
                    detailValue(item.sourceIdentity, label: "Source")
                }
                GridRow {
                    Text("Source location").foregroundStyle(.secondary)
                    detailValue(item.sourceLocation, label: "Source location")
                }
                GridRow {
                    Text("Selected title").foregroundStyle(.secondary)
                    detailValue(item.selectedTitleIdentity, label: "Selected title")
                }
                GridRow {
                    Text("Source kind").foregroundStyle(.secondary)
                    detailValue(item.sourceKindName, label: "Source kind")
                }
                GridRow {
                    Text("State").foregroundStyle(.secondary)
                    detailValue(item.status.title, label: "State")
                }
            }
            .font(.callout)
        }
    }

    private func detailValue(_ value: String, label: String) -> some View {
        Text(value)
            .lineLimit(1)
            .truncationMode(.middle)
            .accessibilityLabel("\(label): \(value)")
    }
}

private struct PersistentQueueRouteQualityResolutionView: View {
    let group: QueueResolutionGroup
    @ObservedObject var memoryStore: ResolutionMemoryStore
    let apply: (QueueResolutionGroup, QueueResolutionSelection) -> Void
    @State private var selection = QueueResolutionSelection()
    @State private var message: String?
    @State private var loadedSuggestion = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("A choice is needed before this video can run")
                .font(.headline)
            Text(group.conflict.reason)
                .font(.callout)
                .foregroundStyle(.secondary)
            Picker("Resolution", selection: $selection.resolutionID) {
                Text("Choose a resolution").tag(String?.none)
                ForEach(group.conflict.resolutions.filter(\.isAvailable)) { option in
                    Text(option.title).tag(Optional(option.id))
                }
            }
            .pickerStyle(.radioGroup)
            Picker("Apply to", selection: $selection.scope) {
                Text("All \(group.candidates.count) matching items").tag(QueueResolutionScope.allMatching)
                ForEach(group.candidates) { candidate in
                    Text("\(candidate.title) only").tag(QueueResolutionScope.item(candidate.id))
                }
            }
            .pickerStyle(.menu)
            Toggle("Suggest this next time for \(group.profileName)", isOn: $selection.shouldSuggest)
                .font(.caption)
            if let suggestion {
                Text(suggestion.staleExplanation ?? "Suggested for this Profile. You’ll still confirm it with Apply Choice.")
                    .font(.caption)
                    .foregroundStyle(suggestion.isStale ? .orange : .secondary)
                Button("Forget suggestion") {
                    do {
                        try memoryStore.forget(conflictID: group.conflict.stableID, scope: suggestion.entry.scope)
                        selection.resolutionID = nil
                        message = nil
                    } catch {
                        message = error.localizedDescription
                    }
                }
                .buttonStyle(.borderless)
            }
            HStack {
                if let message {
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(.red)
                }
                Spacer()
                Button("Apply Choice") {
                    guard let resolutionID = selection.resolutionID else { return }
                    if selection.shouldSuggest,
                       let option = group.conflict.resolutions.first(where: { $0.id == resolutionID })
                    {
                        do {
                            try memoryStore.store(
                                resolutionID: option.id,
                                for: group.conflict.stableID,
                                scope: .profile(group.candidates[0].draft.profile.id),
                                mappingVersion: group.conflict.mappingVersion
                            )
                        } catch {
                            message = error.localizedDescription
                            return
                        }
                    }
                    apply(group, selection)
                }
                .buttonStyle(.borderedProminent)
                .disabled(!selection.canApply)
                .accessibilityIdentifier("persistent-queue-apply-choice")
            }
        }
        .padding(12)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
        .accessibilityIdentifier("persistent-queue-conflict-group")
        .onAppear {
            guard !loadedSuggestion else { return }
            loadedSuggestion = true
            if let suggestion, !suggestion.isStale {
                selection.resolutionID = group.conflict.resolutions.contains(where: {
                    $0.id == suggestion.entry.resolutionID && $0.isAvailable
                }) ? suggestion.entry.resolutionID : nil
            }
        }
    }

    private var suggestion: ResolutionMemorySuggestion? {
        let candidate = group.candidates[0]
        return memoryStore.suggestion(
            conflictID: group.conflict.stableID,
            profileID: candidate.draft.profile.id,
            sourceKind: candidate.draft.source.kind,
            mappingVersion: group.conflict.mappingVersion
        )
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
        case .needsChoice: "Needs a Choice"
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
        case let .needsChoice(conflict): conflict.reason
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
        case .needsChoice: "exclamationmark.circle.fill"
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
        case .needsChoice, .interrupted, .attention: .orange
        case .failed: .red
        case .completed: .green
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

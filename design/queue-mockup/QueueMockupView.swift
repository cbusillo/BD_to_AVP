import Combine
import SwiftUI

struct QueueMockupRootView: View {
    @StateObject private var model = QueueMockupModel()
    private let timer = Timer.publish(every: 0.5, on: .main, in: .common).autoconnect()

    var body: some View {
        VStack(spacing: 0) {
            HSplitView {
                QueueSidebar(model: model)
                    .frame(minWidth: 300, idealWidth: 330, maxWidth: 420)

                QueueDetailPane(model: model)
                    .frame(minWidth: 600, idealWidth: 760)
            }

            Divider()

            QueueFooter(model: model)
        }
        .frame(minWidth: 920, minHeight: 620)
        .preferredColorScheme(model.appearance.colorScheme)
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                Picker("Scenario", selection: $model.scenario) {
                    ForEach(MockScenario.allCases) { scenario in
                        Text(scenario.title).tag(scenario)
                    }
                }
                .pickerStyle(.menu)
                .frame(width: 170)
                .onChange(of: model.scenario) { _, scenario in
                    model.load(scenario)
                }

                Picker("Appearance", selection: $model.appearance) {
                    ForEach(MockAppearance.allCases) { appearance in
                        Text(appearance.title).tag(appearance)
                    }
                }
                .pickerStyle(.menu)
                .frame(width: 165)

                Toggle(isOn: $model.compactRows) {
                    Image(systemName: "rectangle.compress.vertical")
                }
                .toggleStyle(.button)
                .help("Use compact queue rows")
            }
        }
        .alert("Stop the current conversion?", isPresented: $model.showStopConfirmation) {
            Button("Keep Converting", role: .cancel) {}
            Button("Stop and Pause Queue", role: .destructive, action: model.stopCurrent)
        } message: {
            Text("Current progress will be lost. The remaining videos will stay in the queue until you resume it.")
        }
        .onReceive(timer) { _ in
            model.tickProgress()
        }
    }
}

struct QueueSidebar: View {
    @ObservedObject var model: QueueMockupModel

    var body: some View {
        VStack(spacing: 0) {
            queueHeader
            Divider()

            if let banner = bannerContent {
                QueueBannerView(content: banner)
                    .padding(.horizontal, 8)
                    .padding(.top, 8)
            }

            if model.items.isEmpty {
                QueueEmptyState(addSource: model.addSyntheticSource)
            } else {
                queueList
            }

            Divider()
            queueActionBar
        }
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private var queueHeader: some View {
        HStack(spacing: 8) {
            Text("Queue")
                .font(.headline)

            Spacer()

            if !model.items.isEmpty {
                Text("\(model.items.count)")
                    .font(.caption.monospacedDigit().weight(.medium))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(Color.secondary.opacity(0.12), in: Capsule())
            }

            Menu {
                Toggle("Compact Queue", isOn: $model.compactRows)
                Divider()
                Button("Clear Completed", action: model.clearCompleted)
                    .disabled(model.completedItems.isEmpty)
                Button("Remove All…", role: .destructive) {
                    model.items.removeAll()
                    model.selectedID = nil
                    model.queueState = .empty
                }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .menuStyle(.borderlessButton)
            .accessibilityLabel("Queue options")
        }
        .padding(.horizontal, 14)
        .frame(height: 44)
        .background(MockChromeBackground())
    }

    private var queueList: some View {
        List(selection: $model.selectedID) {
            if !model.activeItems.isEmpty {
                Section("Now Converting") {
                    ForEach(model.activeItems) { item in
                        QueueRow(item: item, compact: model.compactRows, isSelected: model.selectedID == item.id, model: model)
                            .tag(item.id)
                    }
                }
            }

            if !model.waitingItems.isEmpty || !model.attentionItems.isEmpty {
                Section(model.activeItems.isEmpty ? "Queue" : "Up Next") {
                    ForEach(model.items.filter { !$0.status.isActive && !$0.status.isCompleted }) { item in
                        QueueRow(item: item, compact: model.compactRows, isSelected: model.selectedID == item.id, model: model)
                            .tag(item.id)
                    }
                }
            }

            if !model.completedItems.isEmpty {
                Section("Completed") {
                    ForEach(model.completedItems) { item in
                        QueueRow(item: item, compact: model.compactRows, isSelected: model.selectedID == item.id, model: model)
                            .tag(item.id)
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .accessibilityLabel("Conversion queue")
    }

    private var queueActionBar: some View {
        HStack(spacing: 6) {
            Menu {
                Button("Add Files…", action: model.addSyntheticSource)
                Button("Add Folder…", action: model.addSyntheticSource)
                Divider()
                Button("Add Inserted Disc", action: model.addSyntheticSource)
                Button("Import MTS or M2TS…", action: model.addSyntheticSource)
            } label: {
                Image(systemName: "plus")
                    .frame(width: 20, height: 20)
            }
            .menuStyle(.borderlessButton)
            .help("Add videos to the queue")
            .accessibilityLabel("Add to queue")

            Button(action: model.removeSelected) {
                Image(systemName: "minus")
                    .frame(width: 20, height: 20)
            }
            .buttonStyle(.borderless)
            .disabled(model.selectedID == nil)
            .help("Remove the selected video")

            Spacer()

            if !model.completedItems.isEmpty {
                Button("Clear Completed", action: model.clearCompleted)
                    .buttonStyle(.borderless)
                    .font(.caption)
            }
        }
        .padding(.horizontal, 12)
        .frame(height: 34)
        .background(MockChromeBackground())
    }

    private var bannerContent: QueueBannerContent? {
        switch model.queueState {
        case .scheduled:
            QueueBannerContent(
                icon: "calendar.badge.clock",
                tint: .secondary,
                title: "Queue starts tonight at 11:00 PM",
                detail: "\(model.queueCountText.capitalized) will convert during your off-peak window.",
                actionTitle: nil,
                action: {}
            )
        case .paused:
            QueueBannerContent(
                icon: "pause.circle.fill",
                tint: .orange,
                title: "Queue paused",
                detail: "Your remaining videos are safe and ready to continue.",
                actionTitle: nil,
                action: {}
            )
        case .pausing:
            QueueBannerContent(
                icon: "hourglass",
                tint: .orange,
                title: "Finishing the current video",
                detail: "The queue will pause before the next video begins.",
                actionTitle: nil,
                action: {}
            )
        case .restored:
            QueueBannerContent(
                icon: "arrow.counterclockwise.circle.fill",
                tint: .orange,
                title: "Queue restored",
                detail: "One interrupted video needs to restart from its last safe stage.",
                actionTitle: nil,
                action: {}
            )
        case .finished where !model.attentionItems.isEmpty:
            QueueBannerContent(
                icon: "exclamationmark.triangle.fill",
                tint: .red,
                title: "Some videos need attention",
                detail: "\(model.completedItems.count) completed · \(model.attentionItems.count) need review.",
                actionTitle: nil,
                action: {}
            )
        case .finished:
            QueueBannerContent(
                icon: "checkmark.circle.fill",
                tint: .green,
                title: "Queue complete",
                detail: "Every video finished successfully.",
                actionTitle: nil,
                action: {}
            )
        default:
            nil
        }
    }
}

struct QueueEmptyState: View {
    let addSource: () -> Void

    var body: some View {
        VStack(spacing: 15) {
            Spacer(minLength: 26)

            Image(systemName: "opticaldisc")
                .font(.system(size: 44, weight: .light))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(Color.accentColor)
                .accessibilityHidden(true)

            Text("Nothing in the queue")
                .font(.title3.weight(.semibold))

            Text("Insert a 3D Blu-ray disc, or drop a disc image, Blu-ray folder, or 3D MKV here.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)

            Label("MakeMKV ready", systemImage: "checkmark.circle.fill")
                .font(.callout)
                .foregroundStyle(.green)

            Button("Add Source…", action: addSource)
                .buttonStyle(.borderedProminent)
                .controlSize(.large)

            Text("Videos convert one at a time in the order shown.")
                .font(.caption)
                .foregroundStyle(.secondary)

            Spacer()
        }
        .padding(.horizontal, 24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .contentShape(Rectangle())
    }
}

struct QueueRow: View {
    let item: MockQueueItem
    let compact: Bool
    let isSelected: Bool
    @ObservedObject var model: QueueMockupModel

    var body: some View {
        HStack(alignment: .center, spacing: compact ? 7 : 9) {
            statusGlyph

            VStack(alignment: .leading, spacing: compact ? 1 : 3) {
                Text(item.name)
                    .font(compact ? .callout : .callout.weight(.medium))
                    .lineLimit(1)
                    .truncationMode(.middle)

                if !compact || shouldShowSubtitleInCompactMode {
                    Text(item.subtitle)
                        .font(.caption)
                        .foregroundStyle(subtitleColor)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }

                if case let .converting(_, progress, _) = item.status {
                    ProgressView(value: progress)
                        .progressViewStyle(.linear)
                        .controlSize(.small)
                        .tint(isSelected ? Color.white.opacity(0.78) : Color.accentColor)
                        .accessibilityHidden(true)
                }
            }

            Spacer(minLength: 6)

            trailingAccessory
                .frame(width: 32, alignment: .trailing)
        }
        .padding(.vertical, compact ? 2 : 4)
        .contentShape(Rectangle())
        .contextMenu {
            if item.status.isEditable {
                Button("Convert Next", action: model.moveSelectedNext)
                Button("Move Up") { model.moveSelected(by: -1) }
                Button("Move Down") { model.moveSelected(by: 1) }
                Divider()
            }
            if case .failed = item.status {
                Button("Retry", action: model.retrySelected)
                Divider()
            }
            Button("Remove from Queue", role: .destructive, action: model.removeSelected)
                .disabled(item.status.isActive)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(item.name), \(item.status.title), \(positionText)")
        .accessibilityAction(named: "Move Up") { model.moveSelected(by: -1) }
        .accessibilityAction(named: "Move Down") { model.moveSelected(by: 1) }
        .accessibilityAction(named: "Remove from Queue", model.removeSelected)
    }

    @ViewBuilder
    private var statusGlyph: some View {
        if isSelected && isSeverityStatus {
            Circle()
                .fill(item.status.tint)
                .frame(width: compact ? 15 : 18, height: compact ? 15 : 18)
                .overlay {
                    Image(systemName: item.status.systemImage)
                        .font(.system(size: compact ? 8 : 10, weight: .bold))
                        .foregroundStyle(Color.white)
                }
                .accessibilityHidden(true)
        } else {
            Image(systemName: item.status.systemImage)
                .font(.system(size: compact ? 13 : 15, weight: .medium))
                .foregroundStyle(isSelected ? Color.white : item.status.tint)
                .frame(width: compact ? 15 : 18)
                .accessibilityHidden(true)
        }
    }

    @ViewBuilder
    private var trailingAccessory: some View {
        switch item.status {
        case .converting:
            EmptyView()
        case .failed, .needsAttention, .unavailable, .interrupted, .stopped:
            if isSelected {
                Menu {
                    Button("Retry", action: model.retrySelected)
                    Button("Convert Next", action: model.moveSelectedNext)
                    Divider()
                    Button("Remove from Queue", role: .destructive, action: model.removeSelected)
                } label: {
                    Image(systemName: "ellipsis")
                }
                .menuStyle(.borderlessButton)
            }
        case .completed:
            if isSelected {
                Image(systemName: "arrow.forward.circle")
                    .foregroundStyle(.secondary)
                    .help("Reveal output in Finder")
            }
        default:
            if isSelected {
                Menu {
                    Button("Convert Next", action: model.moveSelectedNext)
                    Button("Move Up") { model.moveSelected(by: -1) }
                    Button("Move Down") { model.moveSelected(by: 1) }
                    Divider()
                    Button("Remove from Queue", role: .destructive, action: model.removeSelected)
                } label: {
                    Image(systemName: "ellipsis")
                }
                .menuStyle(.borderlessButton)
            }
        }
    }

    private var shouldShowSubtitleInCompactMode: Bool {
        switch item.status {
        case .converting, .failed, .needsAttention, .unavailable, .interrupted:
            true
        default:
            false
        }
    }

    private var isSeverityStatus: Bool {
        switch item.status {
        case .failed, .unavailable, .needsAttention, .interrupted, .completed:
            true
        default:
            false
        }
    }

    private var subtitleColor: Color {
        if isSelected {
            return .white.opacity(0.82)
        }
        switch item.status {
        case .failed, .unavailable:
            return .red
        case .needsAttention, .interrupted, .paused:
            return .orange
        default:
            return .secondary
        }
    }

    private var positionText: String {
        guard let index = model.items.firstIndex(where: { $0.id == item.id }) else {
            return ""
        }
        return "\(index + 1) of \(model.items.count)"
    }
}

struct QueueDetailPane: View {
    @ObservedObject var model: QueueMockupModel

    var body: some View {
        VStack(spacing: 0) {
            if let item = model.selectedItem {
                QueueItemDetailHeader(item: item, position: detailPosition, model: model)
                Divider()
                MockConversionSetup(item: item, selectedTab: $model.selectedTab)
            } else {
                QueueDetailEmptyState()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var detailPosition: String {
        guard let index = model.selectedIndex else {
            return ""
        }
        return "\(index + 1) of \(model.items.count)"
    }
}

struct QueueDetailEmptyState: View {
    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "slider.horizontal.3")
                .font(.system(size: 40, weight: .light))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(.secondary)
            Text("Select a video to configure it")
                .font(.title3.weight(.semibold))
            Text("Each queued video keeps its own profile, destination, and recovery settings.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(32)
    }
}

struct QueueItemDetailHeader: View {
    let item: MockQueueItem
    let position: String
    @ObservedObject var model: QueueMockupModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: item.kind.systemImage)
                    .font(.system(size: 28, weight: .medium))
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(Color.accentColor)
                    .frame(width: 38)
                    .accessibilityHidden(true)

                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 7) {
                        Text(item.name)
                            .font(.title3.weight(.semibold))
                            .lineLimit(1)
                            .truncationMode(.middle)

                        Text(item.kind.title)
                            .font(.caption.weight(.medium))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 2)
                            .background(Color.secondary.opacity(0.12), in: Capsule())

                        Text(position)
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }

                    Text(item.location)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }

                Spacer()

                Menu {
                    Button("Convert Next", action: model.moveSelectedNext)
                    Button("Preview…") {}
                    Button("Reveal in Finder") {}
                    Divider()
                    Button("Remove from Queue", role: .destructive, action: model.removeSelected)
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .menuStyle(.borderlessButton)
                .accessibilityLabel("Video actions")
            }

            itemStateStrip

            HStack(spacing: 22) {
                MockFact(label: "Resolution", value: item.resolution)
                MockFact(label: "Frame Rate", value: item.frameRate)
                MockFact(label: "Duration", value: item.duration)
                MockFact(label: "Source Size", value: item.size)
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 15)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    @ViewBuilder
    private var itemStateStrip: some View {
        switch item.status {
        case let .converting(stage, progress, elapsed):
            HStack(spacing: 10) {
                Image(systemName: "gearshape.2")
                    .foregroundStyle(Color.accentColor)
                Text(stage)
                    .font(.callout.weight(.medium))
                Spacer()
                Text("\(Int(progress * 100))% · \(elapsed)")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        case let .scheduled(time):
            HStack {
                Label("Starts \(time)", systemImage: "calendar.badge.clock")
                    .font(.callout.weight(.medium))
            }
        case let .failed(message), let .unavailable(message):
            HStack(alignment: .top, spacing: 9) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
                VStack(alignment: .leading, spacing: 2) {
                    Text(message)
                        .font(.callout.weight(.medium))
                    Text("Review the saved details, then retry this video without disturbing the rest of the queue.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Retry", action: model.retrySelected)
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
            }
            .padding(9)
            .background(Color.red.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
        case let .needsAttention(message):
            HStack {
                Label(message, systemImage: "exclamationmark.circle.fill")
                    .font(.callout.weight(.medium))
                    .foregroundStyle(.orange)
                Spacer()
                Button("Choose Retry…") {}
                    .buttonStyle(.bordered)
                    .controlSize(.small)
            }
            .padding(9)
            .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
        case .interrupted:
            HStack {
                Label("Interrupted during the previous app session", systemImage: "arrow.counterclockwise.circle")
                    .font(.callout.weight(.medium))
                    .foregroundStyle(.orange)
                Spacer()
                Button("Restart Safely", action: model.retrySelected)
                    .buttonStyle(.bordered)
                    .controlSize(.small)
            }
            .padding(9)
            .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
        case let .completed(output):
            HStack {
                Label(output, systemImage: "checkmark.circle.fill")
                    .font(.callout.weight(.medium))
                    .foregroundStyle(.green)
                Spacer()
                Button("Reveal in Finder") {}
                    .buttonStyle(.bordered)
                    .controlSize(.small)
            }
        case let .paused(stage):
            HStack {
                Label("Paused after \(stage)", systemImage: "pause.circle.fill")
                    .font(.callout.weight(.medium))
                    .foregroundStyle(.orange)
            }
        default:
            HStack {
                LabeledContent("Planned file", value: item.plannedOutput)
                    .font(.callout)
                Spacer()
            }
        }
    }
}

struct MockFact: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.callout.weight(.medium))
        }
    }
}

struct MockConversionSetup: View {
    let item: MockQueueItem
    @Binding var selectedTab: String
    @State private var quality = 75.0
    @State private var bitrate = 20.0
    @State private var cropBlackBars = false
    @State private var swapEyes = false
    @State private var includeSubtitles = true

    private let tabs = ["Video", "Audio & Subtitles", "Files & Recovery"]

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Conversion Setup")
                        .font(.title3.weight(.semibold))
                    Text("These choices apply to \(item.name).")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Spacer()

                Picker("Profile", selection: .constant(item.profile)) {
                    Text("Balanced").tag("Balanced")
                    Text("Original Resolution").tag("Original Resolution")
                    Text("4K Upscale").tag("4K Upscale")
                }
                .frame(width: 220)
                .disabled(!item.status.isEditable)
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 14)

            Picker("Setup Section", selection: $selectedTab) {
                ForEach(tabs, id: \.self) { tab in
                    Text(tab).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(.horizontal, 20)
            .padding(.bottom, 10)

            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if !item.status.isEditable {
                        Label(
                            item.status.isCompleted
                                ? "Settings are locked because this video has completed."
                                : "Settings are locked while this video converts.",
                            systemImage: "lock.fill"
                        )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }

                    switch selectedTab {
                    case "Audio & Subtitles":
                        audioSettings
                    case "Files & Recovery":
                        fileSettings
                    default:
                        videoSettings
                    }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .disabled(!item.status.isEditable)
        }
    }

    private var videoSettings: some View {
        VStack(alignment: .leading, spacing: 18) {
            MockSettingsSection(title: "Spatial Video Encoding") {
                VStack(spacing: 14) {
                    LabeledContent("HEVC quality") {
                        HStack {
                            Slider(value: $quality, in: 40 ... 100)
                                .frame(width: 260)
                            Text("\(Int(quality))")
                                .monospacedDigit()
                                .frame(width: 30, alignment: .trailing)
                        }
                    }
                    LabeledContent("Left / right bitrate") {
                        HStack {
                            Stepper("", value: $bitrate, in: 5 ... 80, step: 5)
                                .labelsHidden()
                            Text("\(Int(bitrate)) Mbps")
                                .monospacedDigit()
                                .frame(width: 78, alignment: .trailing)
                        }
                    }
                }
            }

            MockSettingsSection(title: "Stereo Corrections") {
                VStack(spacing: 12) {
                    Toggle("Crop black bars", isOn: $cropBlackBars)
                    Toggle("Swap left and right eyes", isOn: $swapEyes)
                }
            }
        }
    }

    private var audioSettings: some View {
        VStack(alignment: .leading, spacing: 18) {
            MockSettingsSection(title: "Audio") {
                VStack(spacing: 12) {
                    LabeledContent("Preferred language", value: "English")
                    LabeledContent("Audio handling", value: "Automatic")
                }
            }
            MockSettingsSection(title: "Subtitles") {
                Toggle("Include subtitles", isOn: $includeSubtitles)
            }
        }
    }

    private var fileSettings: some View {
        VStack(alignment: .leading, spacing: 18) {
            MockSettingsSection(title: "Destination") {
                VStack(spacing: 12) {
                    LabeledContent("Folder", value: item.destination)
                    LabeledContent("Planned file", value: item.plannedOutput)
                }
            }
            MockSettingsSection(title: "Recovery") {
                VStack(spacing: 12) {
                    Toggle("Keep intermediate files", isOn: .constant(false))
                    Toggle("Continue after subtitle errors", isOn: .constant(false))
                }
            }
        }
    }
}

struct MockSettingsSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    init(title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title)
                .font(.headline)
            VStack(spacing: 0) {
                content
                    .padding(14)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.secondary.opacity(0.06), in: RoundedRectangle(cornerRadius: 10))
        }
    }
}

struct QueueFooter: View {
    @ObservedObject var model: QueueMockupModel

    var body: some View {
        HStack(spacing: 11) {
            Circle()
                .fill(statusColor)
                .frame(width: 8, height: 8)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 2) {
                Text(statusText)
                    .font(.callout.weight(.medium))
                Text(secondaryStatusText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .accessibilityElement(children: .combine)

            if let active = model.activeItems.first,
               case let .converting(_, progress, elapsed) = active.status
            {
                ProgressView(value: progress)
                    .frame(width: 70)
                    .tint(.accentColor)
                    .accessibilityHidden(true)
                Text("\(Int(progress * 100))%")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
                Label("Elapsed \(elapsed)", systemImage: "clock")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            Spacer()

            Button {
                model.showActivity.toggle()
            } label: {
                Label(model.showActivity ? "Hide Activity" : "Show Activity", systemImage: model.showActivity ? "chevron.down" : "chevron.up")
            }
            .buttonStyle(.plain)

            footerControls
        }
        .padding(.horizontal, 16)
        .frame(height: 58)
        .background(MockChromeBackground())
    }

    @ViewBuilder
    private var footerControls: some View {
        switch model.queueState {
        case .empty:
            Button("Start Queue") {}
                .buttonStyle(.bordered)
                .disabled(true)
        case .idle:
            Menu("Start Later") {
                Button("Start in 1 Hour", action: model.scheduleQueue)
                Button("Start Tonight at 11:00 PM", action: model.scheduleQueue)
                Button("Start at Time…", action: model.scheduleQueue)
            }
            .menuStyle(.button)
            .buttonStyle(.bordered)

            Button("Start Queue", action: model.startQueue)
                .buttonStyle(.borderedProminent)
                .keyboardShortcut("p", modifiers: .command)
        case .running:
            Button("Pause After Current", action: model.pauseAfterCurrent)
                .buttonStyle(.borderedProminent)
                .keyboardShortcut("p", modifiers: .command)

            Button("Stop Current…") {
                model.showStopConfirmation = true
            }
            .buttonStyle(.bordered)
            .tint(.red)
        case .pausing:
            Button("Finishing Current Video…") {}
                .buttonStyle(.bordered)
                .disabled(true)

            Button("Pause Now", action: model.finishPause)
                .buttonStyle(.bordered)
        case .paused, .restored:
            Button("Resume Queue", action: model.resumeQueue)
                .buttonStyle(.borderedProminent)
                .keyboardShortcut("p", modifiers: .command)
        case .scheduled:
            Button("Cancel Schedule") {
                model.load(.idle)
            }
            .buttonStyle(.bordered)

            Button("Start Now", action: model.startQueue)
                .buttonStyle(.borderedProminent)
        case .finished:
            if !model.attentionItems.isEmpty {
                Button("Review Issues") {
                    model.selectedID = model.attentionItems.first?.id
                }
                    .buttonStyle(.borderedProminent)
            } else {
                Button("Clear Completed", action: model.clearCompleted)
                    .buttonStyle(.bordered)
            }
        }
    }

    private var statusText: String {
        switch model.queueState {
        case .empty:
            return "Add videos to build your queue"
        case .idle:
            return "\(model.queueCountText.capitalized) ready"
        case .running:
            if let active = model.activeItems.first, let index = model.items.firstIndex(where: { $0.id == active.id }) {
                return "Converting \(index + 1) of \(model.items.count) · \(active.name)"
            }
            return "Queue running"
        case .pausing:
            return "Finishing the current video"
        case .paused:
            return "Queue paused"
        case .scheduled:
            return "Starts tonight at 11:00 PM"
        case .restored:
            return "Queue restored after relaunch"
        case .finished:
            return model.attentionItems.isEmpty ? "Queue complete" : "Queue summary"
        }
    }

    private var secondaryStatusText: String {
        switch model.queueState {
        case .empty:
            return "Files, folders, disc images, and inserted discs are supported"
        case .idle:
            return "Videos convert one at a time in the order shown"
        case .running:
            return model.activeItems.first?.subtitle ?? "Preparing the next video"
        case .pausing:
            return "The queue will pause before the next video starts"
        case .paused:
            return "\(model.completedItems.count) complete · \(model.waitingItems.count) waiting"
        case .scheduled:
            return "Keep the app open; no new video starts outside the off-peak window"
        case .restored:
            return "Review the interrupted video before resuming"
        case .finished:
            return "\(model.completedItems.count) complete · \(model.attentionItems.count) need attention"
        }
    }

    private var statusColor: Color {
        switch model.queueState {
        case .empty:
            return .secondary
        case .idle:
            return .green
        case .scheduled:
            return .blue
        case .restored, .paused, .pausing:
            return .orange
        case .running:
            return .blue
        case .finished:
            return model.attentionItems.isEmpty ? .green : .red
        }
    }
}

struct QueueBannerContent {
    let icon: String
    let tint: Color
    let title: String
    let detail: String
    let actionTitle: String?
    let action: () -> Void
}

struct QueueBannerView: View {
    let content: QueueBannerContent

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: content.icon)
                .foregroundStyle(content.tint)
                .frame(width: 16)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 2) {
                Text(content.title)
                    .font(.callout.weight(.medium))
                Text(content.detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Spacer(minLength: 6)

            if let actionTitle = content.actionTitle {
                Button(actionTitle, action: content.action)
                    .buttonStyle(.borderless)
                    .controlSize(.small)
            }
        }
        .padding(9)
        .background(content.tint.opacity(0.08), in: RoundedRectangle(cornerRadius: 9))
        .overlay {
            RoundedRectangle(cornerRadius: 9)
                .strokeBorder(content.tint.opacity(0.18))
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(content.title). \(content.detail)")
    }
}

struct MockChromeBackground: View {
    var body: some View {
        Rectangle()
            .fill(.bar)
    }
}

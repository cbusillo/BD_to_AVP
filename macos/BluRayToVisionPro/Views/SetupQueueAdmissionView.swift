import SwiftUI

struct SetupQueueAdmissionView: View {
    @ObservedObject var admission: SetupQueueAdmission
    @ObservedObject var memoryStore: ResolutionMemoryStore
    let start: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("Queue", systemImage: "list.number")
                    .font(.headline)
                Spacer()
                Text("\(admission.items.count) items")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    ForEach(admission.groups) { group in
                        SetupQueueConflictGroupView(
                            group: group,
                            admission: admission,
                            memoryStore: memoryStore
                        )
                    }
                    ForEach(admission.items) { item in
                        SetupQueueRow(item: item) {
                            admission.changeResolution(item.id)
                        }
                    }
                }
            }
            Divider()
            Button("Start Queue", action: start)
                .buttonStyle(.borderedProminent)
                .disabled(!admission.canStart)
                .accessibilityIdentifier("queue-start")
        }
        .padding(14)
        .frame(minWidth: 260, idealWidth: 300, maxWidth: 340)
        .accessibilityIdentifier("batch-queue-sidebar")
    }
}

private struct SetupQueueConflictGroupView: View {
    let group: QueueResolutionGroup
    @ObservedObject var admission: SetupQueueAdmission
    @ObservedObject var memoryStore: ResolutionMemoryStore
    @State private var selection = QueueResolutionSelection()
    @State private var message: String?
    @State private var loadedSuggestion = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("A choice is needed")
                .font(.subheadline.weight(.semibold))
            Text(group.titleDisclosure)
                .font(.caption)
                .foregroundStyle(.secondary)
            Picker("Resolution", selection: $selection.resolutionID) {
                Text("Choose a resolution").tag(String?.none)
                ForEach(group.conflict.resolutions) { option in
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
                        try memoryStore.forget(
                            conflictID: group.conflict.stableID,
                            scope: suggestion.entry.scope
                        )
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
                       let option = group.conflict.resolutions.first(where: { $0.id == resolutionID }) {
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
                    switch admission.apply(group: group, selection: selection) {
                    case .success:
                        message = nil
                    case let .failure(error):
                        message = error.localizedDescription
                    }
                }
                .disabled(!selection.canApply)
                .accessibilityIdentifier("queue-apply-choice")
            }
        }
        .padding(10)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
        .accessibilityIdentifier("queue-conflict-group")
        .onAppear {
            guard !loadedSuggestion else { return }
            loadedSuggestion = true
            if let suggestion, !suggestion.isStale {
                selection.resolutionID = suggestion.entry.resolutionID
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

private struct SetupQueueRow: View {
    let item: SetupQueueAdmissionItem
    let change: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon)
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.displayName)
                    .lineLimit(1)
                Text(secondaryLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                if let trace = item.resolutionTrace {
                    Text("\(trace.qualityOutcome) · \(trace.fileOutcome)")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
            Spacer()
            if item.state == .waiting, item.originalConflict != nil {
                Button("Change…", action: change)
                    .buttonStyle(.borderless)
                    .accessibilityIdentifier("queue-row-change")
            }
        }
        .accessibilityIdentifier("queue-row-\(item.id.uuidString)")
    }

    private var secondaryLine: String {
        switch item.state {
        case .waiting: "Waiting · \(item.originalDraft.profile.name)"
        case .held: "Needs a choice · \(item.originalDraft.profile.name)"
        case .running: "Running · \(item.originalDraft.profile.name)"
        case .completed: "Finished · \(item.originalDraft.profile.name)"
        case .attention: "Needs attention · \(item.originalDraft.profile.name)"
        case .stopped: "Stopped · \(item.originalDraft.profile.name)"
        }
    }

    private var icon: String {
        switch item.state {
        case .waiting: "clock"
        case .held: "exclamationmark.circle"
        case .running: "gearshape"
        case .completed: "checkmark.circle"
        case .attention: "exclamationmark.triangle"
        case .stopped: "stop.circle"
        }
    }
}

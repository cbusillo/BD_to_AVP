import Combine
import Foundation

enum SetupQueueAdmissionState: Equatable {
    case waiting
    case held(RouteQualityConflict)
    case running
    case completed
    case attention
    case stopped

    var acceptsChanges: Bool {
        switch self {
        case .waiting, .held:
            true
        case .running, .completed, .attention, .stopped:
            false
        }
    }
}

struct SetupQueueAdmissionItem: Identifiable, Equatable {
    let id: UUID
    let originalDraft: ConversionDraft
    let originalConflict: RouteQualityConflict?
    var resolvedDraft: ConversionDraft?
    var state: SetupQueueAdmissionState
    var resolutionTrace: DurableQueueResolutionTrace?

    init(
        id: UUID = UUID(),
        draft: ConversionDraft,
        conflict: RouteQualityConflict? = nil
    ) {
        self.id = id
        originalDraft = draft
        originalConflict = conflict
        resolvedDraft = conflict == nil ? draft : nil
        state = conflict.map(SetupQueueAdmissionState.held) ?? .waiting
        resolutionTrace = nil
    }

    var displayName: String {
        originalDraft.selectedTitle?.name ?? originalDraft.source.displayName
    }

    var currentDraft: ConversionDraft? {
        resolvedDraft
    }

    var conflict: RouteQualityConflict? {
        guard case let .held(conflict) = state else { return nil }
        return conflict
    }
}

struct SetupQueueAddResult: Equatable {
    let addedCount: Int
    let duplicateDisplayNames: [String]

    var duplicateCount: Int { duplicateDisplayNames.count }
}

@MainActor
final class SetupQueueAdmission: ObservableObject {
    @Published private(set) var items: [SetupQueueAdmissionItem] = []

    var isBatch: Bool { items.count > 1 }
    var hasItems: Bool { !items.isEmpty }
    var canClear: Bool { items.allSatisfy { $0.state != .running } }

    var groups: [QueueResolutionGroup] {
        QueueResolutionGroup.group(
            items.compactMap { item in
                guard item.state.acceptsChanges,
                      let conflict = item.conflict
                else { return nil }
                return QueueResolutionCandidate(id: item.id, draft: item.originalDraft, conflict: conflict)
            }
        )
    }

    var canStart: Bool {
        guard !items.isEmpty else { return false }
        return items.allSatisfy { item in
            guard let draft = item.currentDraft else { return false }
            if case .failure = RouteQualityEngine.validate(draft.options) { return false }
            return item.state == .waiting
        }
    }

    var startableDrafts: [ConversionDraft] {
        guard canStart else { return [] }
        return items.compactMap(\.currentDraft)
    }

    func canAdd(drafts: [ConversionDraft]) -> Bool {
        guard !drafts.isEmpty else { return false }
        if items.allSatisfy({ $0.state == .completed || $0.state == .stopped }) {
            return true
        }
        let existingIDs = Set(items.map { ConversionQueueItem.stablePreviewID(for: $0.originalDraft) })
        return drafts.contains { !existingIDs.contains(ConversionQueueItem.stablePreviewID(for: $0)) }
    }

    @discardableResult
    func add(drafts: [ConversionDraft], conflicts: [RouteQualityConflict?] = []) -> SetupQueueAddResult {
        guard !drafts.isEmpty else {
            return SetupQueueAddResult(addedCount: 0, duplicateDisplayNames: [])
        }
        if items.allSatisfy({ $0.state == .completed || $0.state == .stopped }) {
            items.removeAll()
        }
        let suppliedConflicts = conflicts + Array(repeating: nil, count: max(0, drafts.count - conflicts.count))
        var admittedIDs = Set(items.map { ConversionQueueItem.stablePreviewID(for: $0.originalDraft) })
        var admittedItems: [SetupQueueAdmissionItem] = []
        var duplicateDisplayNames: [String] = []

        for (index, draft) in drafts.enumerated() {
            let identity = ConversionQueueItem.stablePreviewID(for: draft)
            guard admittedIDs.insert(identity).inserted else {
                duplicateDisplayNames.append(draft.selectedTitle?.name ?? draft.source.displayName)
                continue
            }
            admittedItems.append(
                SetupQueueAdmissionItem(draft: draft, conflict: suppliedConflicts[index])
            )
        }
        items.append(contentsOf: admittedItems)
        return SetupQueueAddResult(
            addedCount: admittedItems.count,
            duplicateDisplayNames: duplicateDisplayNames
        )
    }

    func apply(
        group: QueueResolutionGroup,
        selection: QueueResolutionSelection
    ) -> Result<Void, RouteQualityResolutionError> {
        guard let resolutionID = selection.resolutionID,
              let resolution = group.conflict.resolutions.first(where: { $0.id == resolutionID })
        else {
            return .failure(.init(message: "Choose a resolution before applying it."))
        }
        let candidateIDs: Set<UUID>
        switch selection.scope {
        case .allMatching:
            candidateIDs = Set(group.candidates.map(\.id))
        case let .item(id):
            candidateIDs = [id]
        }
        let selectedCandidates = group.candidates.filter { candidateIDs.contains($0.id) }
        guard !selectedCandidates.isEmpty else {
            return .failure(.init(message: "Choose a waiting item before applying it."))
        }
        let selectedGroup = QueueResolutionGroup(key: group.key, candidates: selectedCandidates)
        switch QueueResolutionApplication.apply(group: selectedGroup, selection: selection) {
        case let .failure(error):
            return .failure(error)
        case let .success(application):
            for candidate in selectedCandidates {
                guard let draft = application.resolvedDrafts[candidate.id] else { continue }
                guard let index = items.firstIndex(where: { $0.id == candidate.id }),
                      items[index].state.acceptsChanges
                else { continue }
                let conflict = candidate.conflict
                items[index].resolvedDraft = draft
                items[index].state = .waiting
                items[index].resolutionTrace = DurableQueueResolutionTrace(
                    conflictID: conflict.stableID,
                    resolutionID: resolution.id,
                    qualityOutcome: "\(draft.options.videoRoutePlan.qualityTitle) quality",
                    fileOutcome: draft.options.job.intermediatePolicy.createsReusableArtifacts
                        ? "Reusable files kept"
                        : "Reusable files removed"
                )
            }
            return .success(())
        }
    }

    func markRunning(_ id: UUID) {
        guard let index = items.firstIndex(where: { $0.id == id }),
              items[index].state == .waiting
        else { return }
        items[index].state = .running
    }

    func markAllRunning() {
        for index in items.indices where items[index].state == .waiting {
            items[index].state = .running
        }
    }

    func markStartFailed(_ itemIDs: Set<UUID>) {
        for index in items.indices where itemIDs.contains(items[index].id) {
            switch items[index].state {
            case .waiting, .running:
                items[index].state = .attention
            case .held, .completed, .attention, .stopped:
                break
            }
        }
    }

    func changeResolution(_ id: UUID) {
        guard let index = items.firstIndex(where: { $0.id == id }),
              items[index].state == .waiting,
              let conflict = items[index].originalConflict
        else {
            return
        }
        items[index].resolvedDraft = nil
        items[index].resolutionTrace = nil
        items[index].state = .held(conflict)
    }

    func synchronize(with runtimeItems: [PersistentQueueItem]) {
        for runtimeItem in runtimeItems {
            guard let index = items.firstIndex(where: { $0.id == runtimeItem.id }) else {
                continue
            }
            items[index].resolutionTrace = runtimeItem.resolutionTrace ?? items[index].resolutionTrace
            switch runtimeItem.status {
            case .waiting:
                if items[index].currentDraft != nil {
                    items[index].state = .waiting
                }
            case .inspecting, .processing, .stopping:
                items[index].state = .running
            case .completed:
                items[index].state = .completed
            case .interrupted, .attention, .failed:
                items[index].state = .attention
            case .stopped, .notStarted:
                items[index].state = .stopped
            }
        }
    }

    func durableItems() -> [DurableConversionQueueItem] {
        items.enumerated().compactMap { offset, item in
            guard let draft = item.currentDraft else { return nil }
            return DurableConversionQueueItem(
                id: item.id,
                ordinal: offset,
                origin: item.originalDraft.source.kind == .sourceFolder
                    ? .sourceFolder
                    : (item.originalDraft.selectedTitle == nil ? .singleSource : .multiTitle),
                intent: DurableQueueItemIntent(draft: draft),
                inspection: draft.sourceDetails,
                resolutionTrace: item.resolutionTrace
            )
        }
    }

    func removeAll() {
        items.removeAll()
    }
}

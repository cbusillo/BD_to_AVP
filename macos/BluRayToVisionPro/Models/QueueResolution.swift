import Foundation

struct QueueResolutionCandidate: Identifiable, Equatable {
    let id: UUID
    let title: String
    let draft: ConversionDraft
    let conflict: RouteQualityConflict

    init(id: UUID = UUID(), draft: ConversionDraft, conflict: RouteQualityConflict) {
        self.id = id
        title = draft.selectedTitle?.name ?? draft.source.displayName
        self.draft = draft
        self.conflict = conflict
    }

    var groupKey: String {
        [
            conflict.stableID,
            draft.profile.id,
            draft.source.kind.rawValue,
            conflict.proposedRoute.kind.rawValue,
            String(describing: conflict.proposedRoute.qualitySelection),
            conflict.resolutions.filter(\.isAvailable).map(\.choice.rawValue).sorted().joined(separator: ","),
        ].joined(separator: "|")
    }
}

struct QueueResolutionGroup: Identifiable, Equatable {
    let key: String
    let candidates: [QueueResolutionCandidate]

    var id: String { key }
    var conflict: RouteQualityConflict { candidates[0].conflict }
    var profileName: String { candidates[0].draft.profile.name }
    var titleList: [String] { candidates.map(\.title) }
    var titleDisclosure: String {
        let visible = titleList.prefix(6)
        let more = titleList.count - visible.count
        return visible.joined(separator: ", ") + (more > 0 ? ", and \(more) more" : "")
    }

    static func group(_ candidates: [QueueResolutionCandidate]) -> [QueueResolutionGroup] {
        Dictionary(grouping: candidates, by: \.groupKey)
            .values
            .map { QueueResolutionGroup(key: $0[0].groupKey, candidates: $0) }
            .sorted { $0.titleList.joined() < $1.titleList.joined() }
    }
}

enum QueueResolutionScope: Equatable {
    case allMatching
    case item(UUID)
}

struct QueueResolutionSelection: Equatable {
    var resolutionID: String?
    var scope: QueueResolutionScope = .allMatching
    var shouldSuggest = false

    var canApply: Bool { resolutionID != nil }
}

struct QueueResolutionApplication: Equatable {
    let resolvedDrafts: [UUID: ConversionDraft]
    let resolution: RouteQualityResolutionOption

    static func apply(
        group: QueueResolutionGroup,
        selection: QueueResolutionSelection
    ) -> Result<QueueResolutionApplication, RouteQualityResolutionError> {
        guard let resolutionID = selection.resolutionID,
              let resolution = group.conflict.resolutions.first(where: { $0.id == resolutionID })
        else {
            return .failure(.init(message: "Choose a resolution before applying it."))
        }
        let targetIDs: Set<UUID> = switch selection.scope {
        case .allMatching: Set(group.candidates.map(\.id))
        case let .item(identifier): [identifier]
        }
        var drafts: [UUID: ConversionDraft] = [:]
        for candidate in group.candidates where targetIDs.contains(candidate.id) {
            switch RouteQualityEngine.resolve(resolution, conflict: candidate.conflict) {
            case let .success(draft): drafts[candidate.id] = ConversionDraft(
                source: candidate.draft.source,
                sourceDetails: candidate.draft.sourceDetails,
                profile: candidate.draft.profile,
                destinationURL: candidate.draft.destinationURL,
                options: draft.options,
                selectedTitle: candidate.draft.selectedTitle
            )
            case let .failure(error): return .failure(error)
            }
        }
        return .success(QueueResolutionApplication(resolvedDrafts: drafts, resolution: resolution))
    }
}

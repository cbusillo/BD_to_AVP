import Foundation

enum PersistentQueueRunState: Equatable {
    case idle
    case running
    case pauseAfterCurrent
    case paused
}

enum PersistentQueueCommandOutcome: Equatable {
    case accepted(PersistentQueueRunState)
    case noChange(PersistentQueueRunState)
    case rejected(PersistentQueueCommandRejection)
}

enum PersistentQueueCommandRejection: Equatable {
    case noEligibleItems
    case unresolvedChoices
    case noActiveItem
    case queueIsNotRunning
    case otherWorkIsActive
}

enum PersistentQueueItemStatus: Equatable {
    case waiting
    case needsChoice(RouteQualityConflict)
    case inspecting
    case processing
    case stopping
    case interrupted
    case attention(DurableQueueDecision)
    case failed(DurableQueueFailure)
    case completed(DurableQueueResult)
    case stopped
    case notStarted
}

struct PersistentQueueItem: Identifiable, Equatable {
    let id: UUID
    let ordinal: Int
    let groupID: UUID?
    let origin: DurableQueueItemOrigin
    let draft: ConversionDraft
    let status: PersistentQueueItemStatus
    let attemptCount: Int
    let resolutionTrace: DurableQueueResolutionTrace?

    init(item: DurableConversionQueueItem) throws {
        guard let sourceKind = ConversionSourceKind(rawValue: item.intent.source.kind) else {
            throw PersistentQueueProjectionError.invalidSourceKind(item.intent.source.kind)
        }
        id = item.id
        ordinal = item.ordinal
        groupID = item.groupID
        origin = item.origin
        draft = ConversionDraft(
            source: ConversionSource(
                kind: sourceKind,
                url: URL(fileURLWithPath: item.intent.source.path),
                displayName: item.intent.source.displayName,
                workerSourcePath: item.intent.source.workerSourcePath,
                mediaIdentifier: item.intent.source.mediaIdentifier
            ),
            sourceDetails: item.inspection,
            profile: EncodingProfile(
                id: item.intent.profile.id,
                name: item.intent.profile.name,
                options: item.intent.options.encoding,
                kind: item.intent.profile.kind,
                systemImage: "slider.horizontal.3"
            ),
            destinationURL: URL(fileURLWithPath: item.intent.destinationPath),
            options: item.intent.options,
            selectedTitle: item.intent.selectedTitle
        )
        let resolvedStatus: PersistentQueueItemStatus
        switch item.state {
        case .waiting:
            guard item.routeQualityConflict == nil else {
                throw PersistentQueueProjectionError.unexpectedRouteQualityConflict(item.id)
            }
            resolvedStatus = .waiting
        case .needsChoice:
            guard let conflict = item.routeQualityConflict?.conflict else {
                throw PersistentQueueProjectionError.missingRouteQualityConflict(item.id)
            }
            resolvedStatus = .needsChoice(conflict)
        case .inspecting:
            resolvedStatus = .inspecting
        case .processing:
            resolvedStatus = .processing
        case .stopping:
            resolvedStatus = .stopping
        case .interrupted:
            resolvedStatus = .interrupted
        case .attention:
            guard let decision = item.decision else {
                throw PersistentQueueProjectionError.missingDecision(item.id)
            }
            resolvedStatus = .attention(decision)
        case .failed:
            guard let failure = item.failure else {
                throw PersistentQueueProjectionError.missingFailure(item.id)
            }
            resolvedStatus = .failed(failure)
        case .completed:
            guard let result = item.result else {
                throw PersistentQueueProjectionError.missingResult(item.id)
            }
            resolvedStatus = .completed(result)
        case .stopped:
            resolvedStatus = .stopped
        case .notStarted:
            resolvedStatus = .notStarted
        }
        status = resolvedStatus
        attemptCount = item.attempts.count
        resolutionTrace = item.resolutionTrace
    }

    var displayName: String {
        draft.selectedTitle?.name ?? draft.source.displayName
    }

    var sourceIdentity: String {
        draft.source.displayName
    }

    var selectedTitleIdentity: String {
        if let selectedTitle = draft.selectedTitle {
            return selectedTitle.name
        }
        return draft.source.kind.isDiscWorkflow ? "Main movie (automatic)" : "Entire source"
    }

    var sourceKindName: String {
        draft.source.kind.title
    }

    var sourceLocation: String {
        draft.source.locationDescription
    }

    var isRestored: Bool {
        switch status {
        case .interrupted:
            true
        case let .attention(decision):
            decision.staleAfterRestore
        default:
            false
        }
    }

    var isEditable: Bool {
        status == .waiting
    }

    var canMove: Bool {
        status == .waiting
    }

    var queueManipulationLockReason: String? {
        switch status {
        case .waiting:
            nil
        case .needsChoice:
            "Resolve this item's required choice before moving or editing it."
        case .inspecting, .processing:
            "This item is active and cannot move or be edited until it finishes."
        case .stopping:
            "This item is stopping and cannot move or be edited until it has stopped."
        case .interrupted:
            "Restart this interrupted item before changing its position or settings."
        case .attention:
            "Resolve the required action before moving or editing this item."
        case .failed:
            "Retry this failed item before changing its position or settings."
        case .completed:
            "Completed items cannot move or be edited."
        case .stopped:
            "Restart this stopped item before changing its position or settings."
        case .notStarted:
            "This item is not ready to move or edit yet."
        }
    }

    var canRemove: Bool {
        switch status {
        case .waiting, .needsChoice, .interrupted, .attention, .failed, .stopped, .notStarted:
            true
        case .inspecting, .processing, .stopping, .completed:
            false
        }
    }

    var canRetry: Bool {
        switch status {
        case .interrupted, .attention:
            true
        case let .failed(failure):
            failure.retryable
        default:
            false
        }
    }
}

struct PersistentQueueRemovalToken: Equatable {
    let items: [DurableConversionQueueItem]
    let revision: Int

    var isEmpty: Bool {
        items.isEmpty
    }
}

struct PersistentQueueAppendResult: Equatable {
    let addedCount: Int
    let duplicateDisplayNames: [String]

    var duplicateCount: Int { duplicateDisplayNames.count }
}

enum PersistentQueueProjectionError: Error, Equatable {
    case invalidSourceKind(String)
    case missingDecision(UUID)
    case missingRouteQualityConflict(UUID)
    case unexpectedRouteQualityConflict(UUID)
    case missingFailure(UUID)
    case missingResult(UUID)
    case unexpected
}

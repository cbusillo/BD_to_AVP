import Foundation

struct ConversionQueueDocument: Codable, Equatable {
    static let currentVersion = 1

    let version: Int
    var items: [DurableConversionQueueItem]

    init(version: Int = currentVersion, items: [DurableConversionQueueItem] = []) {
        self.version = version
        self.items = items
    }

    func restoredAfterLaunch() -> ConversionQueueDocument {
        ConversionQueueDocument(
            version: version,
            items: items.map { $0.restoredAfterLaunch() }
        )
    }
}

struct DurableConversionQueueItem: Codable, Equatable, Identifiable {
    let id: UUID
    var ordinal: Int
    let groupID: UUID?
    let origin: DurableQueueItemOrigin
    var intent: DurableQueueItemIntent
    var inspection: SourceInspection?
    var state: DurableQueueItemState
    var attempts: [DurableQueueAttempt]
    var decision: DurableQueueDecision?
    var failure: DurableQueueFailure?
    var result: DurableQueueResult?

    init(
        id: UUID = UUID(),
        ordinal: Int,
        groupID: UUID? = nil,
        origin: DurableQueueItemOrigin,
        intent: DurableQueueItemIntent,
        inspection: SourceInspection? = nil,
        state: DurableQueueItemState = .waiting,
        attempts: [DurableQueueAttempt] = [],
        decision: DurableQueueDecision? = nil,
        failure: DurableQueueFailure? = nil,
        result: DurableQueueResult? = nil
    ) {
        self.id = id
        self.ordinal = ordinal
        self.groupID = groupID
        self.origin = origin
        self.intent = intent
        self.inspection = inspection
        self.state = state
        self.attempts = attempts
        self.decision = decision
        self.failure = failure
        self.result = result
    }

    func restoredAfterLaunch() -> DurableConversionQueueItem {
        var restored = self
        switch state {
        case .inspecting, .processing, .stopping:
            restored.state = .interrupted
            restored.decision = nil
        case .attention:
            guard let decision, decision.isKnownRecoveryDecision else {
                restored.state = .interrupted
                restored.decision = nil
                return restored
            }
            restored.decision = decision.markedStaleAfterRestore()
        case .waiting, .interrupted, .failed, .completed, .stopped, .notStarted:
            break
        }
        return restored
    }
}

enum DurableQueueItemOrigin: String, Codable, Equatable {
    case singleSource
    case multiTitle
    case sourceFolder
}

enum DurableQueueItemState: String, Codable, Equatable, CaseIterable {
    case waiting
    case inspecting
    case processing
    case stopping
    case interrupted
    case attention
    case failed
    case completed
    case stopped
    case notStarted
}

struct DurableQueueItemIntent: Codable, Equatable {
    var source: DurableQueueSource
    var profile: DurableQueueProfile
    var destinationPath: String
    var options: ConversionOptions
    var selectedTitle: SourceTitle?

    init(
        source: DurableQueueSource,
        profile: DurableQueueProfile,
        destinationPath: String,
        options: ConversionOptions,
        selectedTitle: SourceTitle? = nil
    ) {
        self.source = source
        self.profile = profile
        self.destinationPath = destinationPath
        self.options = options
        self.selectedTitle = selectedTitle
    }

    init(draft: ConversionDraft) {
        source = DurableQueueSource(source: draft.source)
        profile = DurableQueueProfile(profile: draft.profile)
        destinationPath = draft.destinationURL.standardizedFileURL.path
        options = draft.options
        selectedTitle = draft.selectedTitle
    }
}

struct DurableQueueSource: Codable, Equatable {
    let kind: String
    let path: String
    let displayName: String
    let workerSourcePath: String
    let mediaIdentifier: String?

    init(
        kind: String,
        path: String,
        displayName: String,
        workerSourcePath: String,
        mediaIdentifier: String? = nil
    ) {
        self.kind = kind
        self.path = path
        self.displayName = displayName
        self.workerSourcePath = workerSourcePath
        self.mediaIdentifier = mediaIdentifier
    }

    init(source: ConversionSource) {
        kind = source.kind.rawValue
        path = source.url.standardizedFileURL.path
        displayName = source.displayName
        workerSourcePath = source.workerSourcePath
        mediaIdentifier = source.mediaIdentifier
    }
}

struct DurableQueueProfile: Codable, Equatable {
    let id: String
    let name: String
    let kind: EncodingProfile.Kind

    init(id: String, name: String, kind: EncodingProfile.Kind) {
        self.id = id
        self.name = name
        self.kind = kind
    }

    init(profile: EncodingProfile) {
        id = profile.id
        name = profile.name
        kind = profile.kind
    }
}

struct DurableQueueAttempt: Codable, Equatable, Identifiable {
    let id: UUID
    let startedAt: Date
    var endedAt: Date?
    var lastStage: ConversionStage?
    var recoveryChoice: String?

    init(
        id: UUID = UUID(),
        startedAt: Date,
        endedAt: Date? = nil,
        lastStage: ConversionStage? = nil,
        recoveryChoice: String? = nil
    ) {
        self.id = id
        self.startedAt = startedAt
        self.endedAt = endedAt
        self.lastStage = lastStage
        self.recoveryChoice = recoveryChoice
    }
}

struct DurableQueueDecision: Codable, Equatable {
    let identifier: String
    let prompt: String
    let choices: [String]
    let details: String?
    let staleAfterRestore: Bool

    init(
        identifier: String,
        prompt: String,
        choices: [String],
        details: String? = nil,
        staleAfterRestore: Bool = false
    ) {
        self.identifier = identifier
        self.prompt = prompt
        self.choices = choices
        self.details = details
        self.staleAfterRestore = staleAfterRestore
    }

    init(decision: WorkerDecision) {
        identifier = decision.identifier
        prompt = decision.prompt
        choices = decision.choices
        details = decision.details
        staleAfterRestore = false
    }

    var isKnownRecoveryDecision: Bool {
        switch identifier {
        case "mkv_creation_decision_required", "subtitle_decision_required":
            true
        default:
            false
        }
    }

    func markedStaleAfterRestore() -> DurableQueueDecision {
        DurableQueueDecision(
            identifier: identifier,
            prompt: prompt,
            choices: choices,
            details: details,
            staleAfterRestore: true
        )
    }
}

struct DurableQueueFailure: Codable, Equatable {
    let code: String?
    let message: String
    let details: String?
    let retryable: Bool
}

struct DurableQueueResult: Codable, Equatable {
    let outputPath: String
    let durationSeconds: Double?
    let sizeBytes: Int64?
    let titleID: String?

    init(
        outputPath: String,
        durationSeconds: Double? = nil,
        sizeBytes: Int64? = nil,
        titleID: String? = nil
    ) {
        self.outputPath = outputPath
        self.durationSeconds = durationSeconds
        self.sizeBytes = sizeBytes
        self.titleID = titleID
    }

    init(result: ConversionResult) {
        outputPath = result.outputPath
        durationSeconds = result.durationSeconds
        sizeBytes = result.sizeBytes
        titleID = result.titleID
    }
}

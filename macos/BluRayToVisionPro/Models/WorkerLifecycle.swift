import Foundation

enum WorkerPhase: String, Equatable {
    case empty
    case ready
    case inspecting
    case processing
    case stopping
    case decisionRequired
    case completed
    case cancelled
    case failed

    var isRunning: Bool {
        self == .inspecting || self == .processing || self == .stopping
    }

    var isTerminal: Bool {
        self == .decisionRequired || self == .completed || self == .cancelled || self == .failed
    }
}

enum WorkerOperationKind: Equatable {
    case inspection
    case conversion
}

enum ElapsedTimeText {
    static func format(seconds: Int) -> String? {
        guard seconds > 0 else {
            return nil
        }
        let hours = seconds / 3_600
        let minutes = (seconds % 3_600) / 60
        let remainingSeconds = seconds % 60
        if hours > 0 {
            return String(format: "%d:%02d:%02d", hours, minutes, remainingSeconds)
        }
        return String(format: "%d:%02d", minutes, remainingSeconds)
    }
}

enum WorkerLifecycleError: Error, LocalizedError, Equatable {
    case noSource
    case protocolMismatch(received: Int)
    case unexpectedJob(received: UUID)
    case unexpectedSequence(expected: Int, received: Int)
    case eventAfterTerminal
    case missingPayload(event: WorkerEventType)

    var errorDescription: String? {
        switch self {
        case .noSource:
            return "Choose a source before continuing."
        case .protocolMismatch:
            return "The app could not read the engine response."
        case .unexpectedJob:
            return "The app received an unexpected engine response."
        case .unexpectedSequence:
            return "The operation ended unexpectedly."
        case .eventAfterTerminal:
            return "The operation returned extra data after it finished."
        case .missingPayload:
            return "The operation did not return all required details."
        }
    }
}

struct WorkerLifecycleState: Equatable {
    private(set) var phase: WorkerPhase = .empty
    private(set) var operationKind: WorkerOperationKind = .inspection
    private(set) var sourceURL: URL?
    private(set) var jobID: UUID?
    private(set) var lastSequence: Int?
    private(set) var stageMessage: String?
    private(set) var activityMessage: String?
    private(set) var warningMessage: String?
    private(set) var elapsedSeconds = 0
    private(set) var result: SourceInspection?
    private(set) var conversionResult: ConversionResult?
    private(set) var failureMessage: String?
    private(set) var failureDetails: String?
    private(set) var failureCode: String?
    private(set) var failureRetryable = false
    private(set) var recoveryDecision: WorkerDecision?

    var elapsedText: String? {
        ElapsedTimeText.format(seconds: elapsedSeconds)
    }

    mutating func selectSource(_ sourceURL: URL) {
        self.sourceURL = sourceURL
        phase = .ready
        resetJobState()
    }

    mutating func begin(jobID: UUID, operationKind: WorkerOperationKind = .inspection) throws {
        guard sourceURL != nil else {
            throw WorkerLifecycleError.noSource
        }
        let inspectionResult = result
        resetJobState()
        if operationKind == .conversion {
            result = inspectionResult
        }
        self.jobID = jobID
        self.operationKind = operationKind
        phase = operationKind == .inspection ? .inspecting : .processing
        stageMessage = operationKind == .inspection ? "Preparing analysis" : "Preparing conversion"
    }

    mutating func receive(_ event: WorkerEvent) throws {
        guard event.protocolVersion == WorkerJobSpec.protocolVersion else {
            throw WorkerLifecycleError.protocolMismatch(received: event.protocolVersion)
        }
        guard event.jobID == jobID else {
            throw WorkerLifecycleError.unexpectedJob(received: event.jobID)
        }
        guard !phase.isTerminal else {
            throw WorkerLifecycleError.eventAfterTerminal
        }

        let expectedSequence = (lastSequence ?? -1) + 1
        guard event.sequence == expectedSequence else {
            throw WorkerLifecycleError.unexpectedSequence(expected: expectedSequence, received: event.sequence)
        }
        lastSequence = event.sequence

        switch event.type {
        case .workerReady:
            if phase != .stopping {
                phase = operationKind == .inspection ? .inspecting : .processing
            }
            stageMessage = operationKind == .inspection ? "Preparing source" : "Preparing conversion"
        case .jobStarted:
            if phase != .stopping {
                phase = operationKind == .inspection ? .inspecting : .processing
            }
            stageMessage = operationKind == .inspection ? "Reading video details" : "Starting conversion"
        case .stageStarted:
            if phase != .stopping {
                phase = .processing
            }
            stageMessage = event.payload.message ?? event.payload.stage ?? "Processing"
        case .heartbeat:
            if phase != .stopping {
                phase = .processing
            }
            elapsedSeconds = event.payload.elapsedSeconds ?? elapsedSeconds
            activityMessage = event.payload.message
        case .log:
            activityMessage = event.payload.message
        case .warning:
            warningMessage = event.payload.message ?? "The operation reported a warning."
        case .artifactReady:
            activityMessage = "Preview artifact is ready."
        case .jobCompleted:
            switch operationKind {
            case .inspection:
                guard let inspectionResult = event.payload.result else {
                    throw WorkerLifecycleError.missingPayload(event: event.type)
                }
                result = inspectionResult
                stageMessage = "Analysis complete"
            case .conversion:
                guard let convResult = event.payload.conversionResult else {
                    throw WorkerLifecycleError.missingPayload(event: event.type)
                }
                conversionResult = convResult
                stageMessage = "Conversion complete"
            }
            phase = .completed
        case .jobFailed:
            guard let failure = event.payload.error else {
                throw WorkerLifecycleError.missingPayload(event: event.type)
            }
            failureMessage = failure.message
            failureDetails = failure.details
            failureCode = failure.code
            failureRetryable = failure.retryable
            phase = .failed
        case .jobCancelled:
            activityMessage = event.payload.message ?? (operationKind == .inspection ? "Analysis stopped." : "Conversion stopped.")
            phase = .cancelled
        case .jobDecisionRequired:
            guard let decision = event.payload.decision else {
                throw WorkerLifecycleError.missingPayload(event: event.type)
            }
            recoveryDecision = decision
            failureMessage = decision.prompt
            failureDetails = decision.details ?? "Choose how this conversion should continue."
            failureCode = decision.identifier
            failureRetryable = true
            phase = .decisionRequired
        }
    }

    mutating func requestStop() {
        guard phase.isRunning else {
            return
        }
        phase = .stopping
        stageMessage = "Stopping safely"
    }

    mutating func failTransport(message: String, details: String? = nil, retryable: Bool = true) {
        failureMessage = message
        failureDetails = details
        failureCode = nil
        failureRetryable = retryable
        recoveryDecision = nil
        phase = .failed
    }

    mutating func completeStop() {
        activityMessage = operationKind == .inspection ? "Inspection stopped." : "Conversion stopped."
        phase = .cancelled
    }

    mutating func prepareForRetry() {
        guard sourceURL != nil else {
            phase = .empty
            return
        }
        let inspectionResult = operationKind == .conversion ? result : nil
        phase = .ready
        resetJobState()
        result = inspectionResult
    }

    mutating func cancelRecoveryDecision() {
        guard phase == .decisionRequired else {
            return
        }
        recoveryDecision = nil
        failureRetryable = false
        phase = .failed
    }

    mutating func clear() {
        self = WorkerLifecycleState()
    }

    private mutating func resetJobState() {
        jobID = nil
        lastSequence = nil
        stageMessage = nil
        activityMessage = nil
        warningMessage = nil
        elapsedSeconds = 0
        result = nil
        conversionResult = nil
        failureMessage = nil
        failureDetails = nil
        failureCode = nil
        failureRetryable = false
        recoveryDecision = nil
    }
}

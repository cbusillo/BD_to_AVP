import Foundation

enum WorkerPhase: String, Equatable {
    case empty
    case ready
    case inspecting
    case processing
    case stopping
    case completed
    case cancelled
    case failed

    var isRunning: Bool {
        self == .inspecting || self == .processing || self == .stopping
    }

    var isTerminal: Bool {
        self == .completed || self == .cancelled || self == .failed
    }
}

enum WorkerOperationKind: Equatable {
    case inspection
    case conversion
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
    private(set) var failureRetryable = false

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
            failureRetryable = failure.retryable
            phase = .failed
        case .jobCancelled:
            activityMessage = event.payload.message ?? (operationKind == .inspection ? "Analysis stopped." : "Conversion stopped.")
            phase = .cancelled
        case .jobDecisionRequired:
            failureMessage = event.payload.decision?.prompt ?? event.payload.message ?? "This source needs a choice before it can continue."
            failureDetails = event.payload.decision?.details ?? "Adjust the conversion settings and try again."
            failureRetryable = true
            phase = .failed
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
        failureRetryable = retryable
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
        phase = .ready
        resetJobState()
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
        failureRetryable = false
    }
}

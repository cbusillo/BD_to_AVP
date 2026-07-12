import Foundation

struct ConversionResult: Decodable, Equatable {
    let outputPath: String
    let durationSeconds: Double?

    enum CodingKeys: String, CodingKey {
        case outputPath = "output_path"
        case durationSeconds = "duration_seconds"
    }
}

enum WorkerEventType: String, Decodable, Equatable {
    case workerReady = "worker.ready"
    case jobStarted = "job.started"
    case stageStarted = "stage.started"
    case heartbeat
    case log
    case warning
    case jobCompleted = "job.completed"
    case jobFailed = "job.failed"
    case jobCancelled = "job.cancelled"
    case jobDecisionRequired = "job.decision_required"

    var isTerminal: Bool {
        switch self {
        case .jobCompleted, .jobFailed, .jobCancelled, .jobDecisionRequired:
            return true
        default:
            return false
        }
    }
}

struct WorkerFailure: Decodable, Equatable {
    let code: String
    let message: String
    let details: String?
    let retryable: Bool
}

struct WorkerDecision: Decodable, Equatable {
    let identifier: String
    let prompt: String
    let choices: [String]

    enum CodingKeys: String, CodingKey {
        case identifier = "id"
        case prompt
        case choices
    }
}

struct WorkerEventPayload: Decodable, Equatable {
    let workerVersion: String?
    let processGroupID: Int32?
    let operation: String?
    let stage: String?
    let message: String?
    let elapsedSeconds: Int?
    let level: String?
    let result: SourceInspection?
    let conversionResult: ConversionResult?
    let error: WorkerFailure?
    let decision: WorkerDecision?

    init(
        workerVersion: String? = nil,
        processGroupID: Int32? = nil,
        operation: String? = nil,
        stage: String? = nil,
        message: String? = nil,
        elapsedSeconds: Int? = nil,
        level: String? = nil,
        result: SourceInspection? = nil,
        conversionResult: ConversionResult? = nil,
        error: WorkerFailure? = nil,
        decision: WorkerDecision? = nil
    ) {
        self.workerVersion = workerVersion
        self.processGroupID = processGroupID
        self.operation = operation
        self.stage = stage
        self.message = message
        self.elapsedSeconds = elapsedSeconds
        self.level = level
        self.result = result
        self.conversionResult = conversionResult
        self.error = error
        self.decision = decision
    }

    enum CodingKeys: String, CodingKey {
        case workerVersion = "worker_version"
        case processGroupID = "process_group_id"
        case operation
        case stage
        case message
        case elapsedSeconds = "elapsed_seconds"
        case level
        case result
        case conversionResult = "conversion_result"
        case error
        case decision
    }
}

struct WorkerEvent: Decodable, Equatable {
    let protocolVersion: Int
    let type: WorkerEventType
    let jobID: UUID
    let sequence: Int
    let payload: WorkerEventPayload

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case type
        case jobID = "job_id"
        case sequence
        case payload
    }
}

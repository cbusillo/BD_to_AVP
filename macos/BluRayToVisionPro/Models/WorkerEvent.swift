import Foundation

struct ConversionResult: Decodable, Equatable {
    let outputPath: String
    let durationSeconds: Double?
    let sizeBytes: Int64?

    var outputURL: URL {
        URL(fileURLWithPath: outputPath)
    }

    init(outputPath: String, durationSeconds: Double? = nil, sizeBytes: Int64? = nil) {
        self.outputPath = outputPath
        self.durationSeconds = durationSeconds
        self.sizeBytes = sizeBytes
    }

    enum CodingKeys: String, CodingKey {
        case outputPath = "output_path"
        case durationSeconds = "duration_seconds"
        case sizeBytes = "size_bytes"
    }
}

struct PreviewArtifact: Decodable, Equatable {
    let sourcePath: String
    let destinationPath: String
    let outputPath: String
    let sizeBytes: Int64
    let parentJobID: UUID
    let position: String
    let startSeconds: Double
    let durationSeconds: Double
    let sourceDurationSeconds: Double

    var outputURL: URL {
        URL(fileURLWithPath: outputPath)
    }

    enum CodingKeys: String, CodingKey {
        case sourcePath = "source_path"
        case destinationPath = "destination_path"
        case outputPath = "output_path"
        case sizeBytes = "size_bytes"
        case parentJobID = "parent_job_id"
        case position
        case startSeconds = "start_seconds"
        case durationSeconds = "duration_seconds"
        case sourceDurationSeconds = "source_duration_seconds"
    }
}

enum WorkerEventType: String, Decodable, Equatable {
    case workerReady = "worker.ready"
    case jobStarted = "job.started"
    case stageStarted = "stage.started"
    case heartbeat
    case log
    case warning
    case artifactReady = "artifact.ready"
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
    let details: String?

    enum CodingKeys: String, CodingKey {
        case identifier = "id"
        case prompt
        case choices
        case details
    }
}

enum WorkerRecoveryChoice: String, Identifiable, Equatable {
    case retryContinueOnError = "retry_continue_on_error"
    case retryWithoutSubtitles = "retry_without_subtitles"
    case cancel

    var id: String { rawValue }

    var title: String {
        switch self {
        case .retryContinueOnError:
            "Continue From Created MKV"
        case .retryWithoutSubtitles:
            "Continue Without Subtitles"
        case .cancel:
            "Cancel"
        }
    }

    var accessibilityHint: String {
        switch self {
        case .retryContinueOnError:
            "Uses the intermediate MKV and resumes at MVC and audio extraction."
        case .retryWithoutSubtitles:
            "Skips subtitle extraction and resumes the remaining conversion stages."
        case .cancel:
            "Leaves the conversion stopped without starting another job."
        }
    }
}

extension WorkerDecision {
    var supportedChoices: [WorkerRecoveryChoice] {
        choices.compactMap { rawChoice in
            guard let choice = WorkerRecoveryChoice(rawValue: rawChoice) else {
                return nil
            }
            switch (identifier, choice) {
            case ("mkv_creation_decision_required", .retryContinueOnError),
                 ("subtitle_decision_required", .retryWithoutSubtitles),
                 (_, .cancel):
                return choice
            default:
                return nil
            }
        }
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
    let artifact: PreviewArtifact?
    let previewResult: PreviewArtifact?
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
        artifact: PreviewArtifact? = nil,
        previewResult: PreviewArtifact? = nil,
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
        self.artifact = artifact
        self.previewResult = previewResult
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
        case artifact
        case previewResult = "preview_result"
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

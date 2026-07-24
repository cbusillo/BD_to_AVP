import Foundation

struct VideoRouteReport: Decodable, Equatable {
    let intent: String
    let selected: String
    let reason: String
    let bitrateMbps: Int?
    let eyeBitrateMbps: Int?
    let mergeQuality: Int?
    let crf: Int?
    let fallbackReason: String?
    let fallbackTiming: String?
    let rateControl: String?
    let quality: Double?

    init(
        intent: String,
        selected: String,
        reason: String,
        bitrateMbps: Int?,
        eyeBitrateMbps: Int?,
        mergeQuality: Int?,
        crf: Int?,
        fallbackReason: String?,
        fallbackTiming: String?,
        rateControl: String? = nil,
        quality: Double? = nil
    ) {
        self.intent = intent
        self.selected = selected
        self.reason = reason
        self.bitrateMbps = bitrateMbps
        self.eyeBitrateMbps = eyeBitrateMbps
        self.mergeQuality = mergeQuality
        self.crf = crf
        self.fallbackReason = fallbackReason
        self.fallbackTiming = fallbackTiming
        self.rateControl = rateControl
        self.quality = quality
    }

    enum CodingKeys: String, CodingKey {
        case intent
        case selected
        case reason
        case bitrateMbps = "bitrate_mbps"
        case eyeBitrateMbps = "eye_bitrate_mbps"
        case mergeQuality = "merge_quality"
        case crf
        case fallbackReason = "fallback_reason"
        case fallbackTiming = "fallback_timing"
        case rateControl = "rate_control"
        case quality
    }
}

struct ConversionResult: Decodable, Equatable {
    let outputPath: String
    let durationSeconds: Double?
    let sizeBytes: Int64?
    let titleID: String?
    let videoRoute: VideoRouteReport?

    var outputURL: URL {
        URL(fileURLWithPath: outputPath)
    }

    init(
        outputPath: String,
        durationSeconds: Double? = nil,
        sizeBytes: Int64? = nil,
        titleID: String? = nil,
        videoRoute: VideoRouteReport? = nil
    ) {
        self.outputPath = outputPath
        self.durationSeconds = durationSeconds
        self.sizeBytes = sizeBytes
        self.titleID = titleID
        self.videoRoute = videoRoute
    }

    enum CodingKeys: String, CodingKey {
        case outputPath = "output_path"
        case durationSeconds = "duration_seconds"
        case sizeBytes = "size_bytes"
        case titleID = "title_id"
        case videoRoute = "video_route"
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
    let titleID: String?
    let videoRoute: VideoRouteReport?

    init(
        sourcePath: String,
        destinationPath: String,
        outputPath: String,
        sizeBytes: Int64,
        parentJobID: UUID,
        position: String,
        startSeconds: Double,
        durationSeconds: Double,
        sourceDurationSeconds: Double,
        titleID: String? = nil,
        videoRoute: VideoRouteReport? = nil
    ) {
        self.sourcePath = sourcePath
        self.destinationPath = destinationPath
        self.outputPath = outputPath
        self.sizeBytes = sizeBytes
        self.parentJobID = parentJobID
        self.position = position
        self.startSeconds = startSeconds
        self.durationSeconds = durationSeconds
        self.sourceDurationSeconds = sourceDurationSeconds
        self.titleID = titleID
        self.videoRoute = videoRoute
    }

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
        case titleID = "title_id"
        case videoRoute = "video_route"
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
    case observability
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

struct WorkerWarning: Decodable, Equatable {
    let code: String?
    let sourceCodecs: [String]?
    let action: String?
    let preferredLanguage: String?
    let selectedLanguage: String?
    let selectedStreamIndex: Int?
    let selectedAudioPosition: Int?
    let fallbackReason: String?

    init(
        code: String? = nil,
        sourceCodecs: [String]? = nil,
        action: String? = nil,
        preferredLanguage: String? = nil,
        selectedLanguage: String? = nil,
        selectedStreamIndex: Int? = nil,
        selectedAudioPosition: Int? = nil,
        fallbackReason: String? = nil
    ) {
        self.code = code
        self.sourceCodecs = sourceCodecs
        self.action = action
        self.preferredLanguage = preferredLanguage
        self.selectedLanguage = selectedLanguage
        self.selectedStreamIndex = selectedStreamIndex
        self.selectedAudioPosition = selectedAudioPosition
        self.fallbackReason = fallbackReason
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        if let code = try container.decodeIfPresent(String.self, forKey: .code) {
            self.code = code
        } else {
            self.code = try container.decodeIfPresent(String.self, forKey: .warningCode)
        }
        sourceCodecs = try container.decodeIfPresent([String].self, forKey: .sourceCodecs)
        if let action = try container.decodeIfPresent(String.self, forKey: .action) {
            self.action = action
        } else if let action = try container.decodeIfPresent(String.self, forKey: .actualAction) {
            self.action = action
        } else {
            self.action = try container.decodeIfPresent(String.self, forKey: .audioAction)
        }
        preferredLanguage = try container.decodeIfPresent(String.self, forKey: .preferredLanguage)
        selectedLanguage = try container.decodeIfPresent(String.self, forKey: .selectedLanguage)
        selectedStreamIndex = try container.decodeIfPresent(Int.self, forKey: .selectedStreamIndex)
        selectedAudioPosition = try container.decodeIfPresent(Int.self, forKey: .selectedAudioPosition)
        fallbackReason = try container.decodeIfPresent(String.self, forKey: .fallbackReason)
    }

    var isEmpty: Bool {
        code == nil
            && sourceCodecs == nil
            && action == nil
            && preferredLanguage == nil
            && selectedLanguage == nil
            && selectedStreamIndex == nil
            && selectedAudioPosition == nil
            && fallbackReason == nil
    }

    enum CodingKeys: String, CodingKey {
        case code
        case warningCode = "warning_code"
        case sourceCodecs = "source_codecs"
        case action
        case actualAction = "actual_action"
        case audioAction = "audio_action"
        case preferredLanguage = "preferred_language"
        case selectedLanguage = "selected_language"
        case selectedStreamIndex = "selected_stream_index"
        case selectedAudioPosition = "selected_audio_position"
        case fallbackReason = "fallback_reason"
    }
}

struct WorkerProgress: Decodable, Equatable {
    let currentStage: Int
    let totalStages: Int
    let stageFraction: Double?

    enum CodingKeys: String, CodingKey {
        case currentStage = "current_stage"
        case totalStages = "total_stages"
        case stageFraction = "stage_fraction"
    }

    var normalized: WorkerProgress? {
        guard totalStages > 0, currentStage > 0, currentStage <= totalStages else {
            return nil
        }
        let normalizedFraction = stageFraction.map { min(1, max(0, $0)) }
        return WorkerProgress(
            currentStage: currentStage,
            totalStages: totalStages,
            stageFraction: normalizedFraction
        )
    }

    var stagePositionText: String {
        "Stage \(currentStage) of \(totalStages)"
    }

    var percentageText: String? {
        guard let stageFraction else {
            return nil
        }
        return "\(Int((stageFraction * 100).rounded()))%"
    }

    var detailText: String {
        guard let percentageText else {
            return stagePositionText
        }
        return "\(percentageText) of current stage · \(stagePositionText)"
    }

    var compactText: String {
        guard let percentageText else {
            return "Stage \(currentStage)/\(totalStages)"
        }
        return "Stage \(currentStage)/\(totalStages) · \(percentageText) of stage"
    }

    var accessibilityValue: String {
        guard let percentageText else {
            return stagePositionText
        }
        return "\(stagePositionText), \(percentageText) of current stage"
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
    let progress: WorkerProgress?
    let level: String?
    let warning: WorkerWarning?
    let result: SourceInspection?
    let conversionResult: ConversionResult?
    let artifact: PreviewArtifact?
    let previewResult: PreviewArtifact?
    let error: WorkerFailure?
    let decision: WorkerDecision?
    let observabilityEvent: ObservabilityEvent?
    let videoRoute: VideoRouteReport?

    init(
        workerVersion: String? = nil,
        processGroupID: Int32? = nil,
        operation: String? = nil,
        stage: String? = nil,
        message: String? = nil,
        elapsedSeconds: Int? = nil,
        progress: WorkerProgress? = nil,
        level: String? = nil,
        warning: WorkerWarning? = nil,
        result: SourceInspection? = nil,
        conversionResult: ConversionResult? = nil,
        artifact: PreviewArtifact? = nil,
        previewResult: PreviewArtifact? = nil,
        error: WorkerFailure? = nil,
        decision: WorkerDecision? = nil,
        observabilityEvent: ObservabilityEvent? = nil,
        videoRoute: VideoRouteReport? = nil
    ) {
        self.workerVersion = workerVersion
        self.processGroupID = processGroupID
        self.operation = operation
        self.stage = stage
        self.message = message
        self.elapsedSeconds = elapsedSeconds
        self.progress = progress
        self.level = level
        self.warning = warning
        self.result = result
        self.conversionResult = conversionResult
        self.artifact = artifact
        self.previewResult = previewResult
        self.error = error
        self.decision = decision
        self.observabilityEvent = observabilityEvent
        self.videoRoute = videoRoute
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        workerVersion = try container.decodeIfPresent(String.self, forKey: .workerVersion)
        processGroupID = try container.decodeIfPresent(Int32.self, forKey: .processGroupID)
        operation = try container.decodeIfPresent(String.self, forKey: .operation)
        stage = try container.decodeIfPresent(String.self, forKey: .stage)
        message = try container.decodeIfPresent(String.self, forKey: .message)
        elapsedSeconds = try container.decodeIfPresent(Int.self, forKey: .elapsedSeconds)
        progress = try container.decodeIfPresent(WorkerProgress.self, forKey: .progress)
        level = try container.decodeIfPresent(String.self, forKey: .level)
        if let nestedWarning = try container.decodeIfPresent(WorkerWarning.self, forKey: .warning) {
            warning = nestedWarning
        } else {
            let flatWarning = try WorkerWarning(from: decoder)
            warning = flatWarning.isEmpty ? nil : flatWarning
        }
        result = try container.decodeIfPresent(SourceInspection.self, forKey: .result)
        conversionResult = try container.decodeIfPresent(ConversionResult.self, forKey: .conversionResult)
        artifact = try container.decodeIfPresent(PreviewArtifact.self, forKey: .artifact)
        previewResult = try container.decodeIfPresent(PreviewArtifact.self, forKey: .previewResult)
        error = try container.decodeIfPresent(WorkerFailure.self, forKey: .error)
        decision = try container.decodeIfPresent(WorkerDecision.self, forKey: .decision)
        observabilityEvent = try container.decodeIfPresent(ObservabilityEvent.self, forKey: .observabilityEvent)
        videoRoute = try container.decodeIfPresent(VideoRouteReport.self, forKey: .videoRoute)
    }

    var warningCode: String? { warning?.code }
    var sourceCodecs: [String]? { warning?.sourceCodecs }
    var audioAction: String? { warning?.action }
    var preferredAudioLanguage: String? { warning?.preferredLanguage }
    var selectedAudioLanguage: String? { warning?.selectedLanguage }
    var selectedAudioStreamIndex: Int? { warning?.selectedStreamIndex }
    var selectedAudioPosition: Int? { warning?.selectedAudioPosition }
    var audioFallbackReason: String? { warning?.fallbackReason }

    enum CodingKeys: String, CodingKey {
        case workerVersion = "worker_version"
        case processGroupID = "process_group_id"
        case operation
        case stage
        case message
        case elapsedSeconds = "elapsed_seconds"
        case progress
        case level
        case warning
        case result
        case conversionResult = "conversion_result"
        case artifact
        case previewResult = "preview_result"
        case error
        case decision
        case observabilityEvent = "event"
        case videoRoute = "video_route"
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

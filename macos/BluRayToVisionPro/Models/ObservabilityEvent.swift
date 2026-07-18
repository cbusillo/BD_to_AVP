import Foundation

struct ObservabilityEvent: Codable, Equatable, Sendable {
    static let currentSchema = "bd_to_avp.observability"
    static let currentSchemaVersion = 1
    static let maximumMessageBytes = 4 * 1_024
    static let maximumDetailBytes = 64 * 1_024

    private static let supportedEmitters: Set<String> = ["app", "worker"]
    private static let supportedSeverities: Set<String> = ["debug", "info", "warning", "error"]
    private static let supportedPrivacy: Set<String> = ["public", "private"]
    private static let supportedRedaction: Set<String> = ["raw", "redacted", "omitted"]

    let schema: String
    let schemaVersion: Int
    let emitter: String
    let streamID: String
    let sequence: Int64
    let occurredAt: String
    let elapsedMilliseconds: Int64?
    let kind: String
    let severity: String
    let privacy: String
    let redaction: String
    let context: ObservabilityEventContext
    let data: ObservabilityEventData

    enum CodingKeys: String, CodingKey {
        case schema
        case schemaVersion = "schema_version"
        case emitter
        case streamID = "stream_id"
        case sequence
        case occurredAt = "occurred_at"
        case elapsedMilliseconds = "elapsed_ms"
        case kind
        case severity
        case privacy
        case redaction
        case context
        case data
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schema = try container.decode(String.self, forKey: .schema)
        schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        guard schema == Self.currentSchema else {
            throw DecodingError.dataCorruptedError(
                forKey: .schema,
                in: container,
                debugDescription: "Unsupported observability schema: \(schema)"
            )
        }
        guard schemaVersion == Self.currentSchemaVersion else {
            throw DecodingError.dataCorruptedError(
                forKey: .schemaVersion,
                in: container,
                debugDescription: "Unsupported observability schema version: \(schemaVersion)"
            )
        }
        emitter = try container.decode(String.self, forKey: .emitter)
        streamID = try container.decode(String.self, forKey: .streamID)
        sequence = try container.decode(Int64.self, forKey: .sequence)
        occurredAt = try container.decode(String.self, forKey: .occurredAt)
        elapsedMilliseconds = try container.decodeIfPresent(Int64.self, forKey: .elapsedMilliseconds)
        kind = try container.decode(String.self, forKey: .kind)
        severity = try container.decode(String.self, forKey: .severity)
        privacy = try container.decode(String.self, forKey: .privacy)
        redaction = try container.decode(String.self, forKey: .redaction)
        context = try container.decode(ObservabilityEventContext.self, forKey: .context)
        data = try container.decode(ObservabilityEventData.self, forKey: .data)
        guard Self.supportedEmitters.contains(emitter) else {
            throw DecodingError.dataCorruptedError(
                forKey: .emitter,
                in: container,
                debugDescription: "Unsupported observability emitter: \(emitter)"
            )
        }
        guard Self.supportedSeverities.contains(severity) else {
            throw DecodingError.dataCorruptedError(
                forKey: .severity,
                in: container,
                debugDescription: "Unsupported observability severity: \(severity)"
            )
        }
        guard Self.supportedPrivacy.contains(privacy) else {
            throw DecodingError.dataCorruptedError(
                forKey: .privacy,
                in: container,
                debugDescription: "Unsupported or secret observability privacy: \(privacy)"
            )
        }
        guard Self.supportedRedaction.contains(redaction) else {
            throw DecodingError.dataCorruptedError(
                forKey: .redaction,
                in: container,
                debugDescription: "Unsupported observability redaction state: \(redaction)"
            )
        }
        if let message = data.message,
           message.value.utf8.count > Self.maximumMessageBytes
        {
            throw DecodingError.dataCorruptedError(
                forKey: .data,
                in: container,
                debugDescription: "Observability message exceeds its UTF-8 byte limit"
            )
        }
    }
}

struct ObservabilityEventContext: Codable, Equatable, Sendable {
    let correlation: ObservabilityCorrelationContext
    let stage: ObservabilityStageContext?
    let tool: ObservabilityToolContext?
    let process: ObservabilityProcessContext?
}

struct ObservabilityCorrelationContext: Codable, Equatable, Sendable {
    let jobID: String?
    let parentJobID: String?

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case parentJobID = "parent_job_id"
    }
}

struct ObservabilityStageContext: Codable, Equatable, Sendable {
    let id: String
    let index: Int32?
    let count: Int32?
}

struct ObservabilityToolContext: Codable, Equatable, Sendable {
    let id: String
    let runID: String?
    let version: String?

    enum CodingKeys: String, CodingKey {
        case id
        case runID = "run_id"
        case version
    }
}

struct ObservabilityProcessContext: Codable, Equatable, Sendable {
    let processID: Int32?
    let processGroupID: Int32?
    let exitCode: Int32?
    let signal: Int32?

    enum CodingKeys: String, CodingKey {
        case processID = "pid"
        case processGroupID = "process_group_id"
        case exitCode = "exit_code"
        case signal
    }
}

struct ObservabilityEventData: Codable, Equatable, Sendable {
    let message: ObservabilityText?
    let detail: ObservabilityText?
    let progress: ObservabilityProgress?
    let artifact: ObservabilityArtifact?
    let storage: ObservabilityStorage?
    let failure: ObservabilityFailure?
    let cancellation: ObservabilityCancellation?
    let counters: ObservabilityCounters?
}

struct ObservabilityText: Codable, Equatable, Sendable {
    let value: String
    let privacy: String
    let truncated: Bool

    private enum CodingKeys: String, CodingKey {
        case value
        case privacy
        case truncated
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        value = try container.decode(String.self, forKey: .value)
        privacy = try container.decode(String.self, forKey: .privacy)
        truncated = try container.decode(Bool.self, forKey: .truncated)
        guard privacy == "public" || privacy == "private" else {
            throw DecodingError.dataCorruptedError(
                forKey: .privacy,
                in: container,
                debugDescription: "Secret observability text must be omitted"
            )
        }
        guard value.utf8.count <= ObservabilityEvent.maximumDetailBytes else {
            throw DecodingError.dataCorruptedError(
                forKey: .value,
                in: container,
                debugDescription: "Observability text exceeds its UTF-8 byte limit"
            )
        }
    }
}

struct ObservabilityProgress: Codable, Equatable, Sendable {
    let fraction: Double?
    let completedUnits: Double?
    let totalUnits: Double?
    let unit: String?

    enum CodingKeys: String, CodingKey {
        case fraction
        case completedUnits = "completed_units"
        case totalUnits = "total_units"
        case unit
    }
}

struct ObservabilityArtifact: Codable, Equatable, Sendable {
    let role: String
    let state: String?
    let location: ObservabilityText?
    let sizeBytes: Int64?
    let modificationAgeSeconds: Int64?
    let growthBytesPerSecond: Int64?

    enum CodingKeys: String, CodingKey {
        case role
        case state
        case location
        case sizeBytes = "size_bytes"
        case modificationAgeSeconds = "modification_age_seconds"
        case growthBytesPerSecond = "growth_bytes_per_second"
    }
}

struct ObservabilityStorage: Codable, Equatable, Sendable {
    let role: String
    let status: String
    let location: ObservabilityText?
    let sizeBytes: Int64?
    let modificationAgeSeconds: Int64?
    let availableBytes: Int64?
    let totalBytes: Int64?
    let readOnly: Bool?
    let writable: Bool?

    enum CodingKeys: String, CodingKey {
        case role
        case status
        case location
        case sizeBytes = "size_bytes"
        case modificationAgeSeconds = "modification_age_seconds"
        case availableBytes = "available_bytes"
        case totalBytes = "total_bytes"
        case readOnly = "read_only"
        case writable
    }
}

struct ObservabilityFailure: Codable, Equatable, Sendable {
    let code: String
    let retryable: Bool?
}

struct ObservabilityCancellation: Codable, Equatable, Sendable {
    let requested: Bool
    let forced: Bool?
}

struct ObservabilityCounters: Codable, Equatable, Sendable {
    let totalBytes: Int64?
    let retainedBytes: Int64?
    let droppedBytes: Int64?
    let decodeReplacements: Int64?

    enum CodingKeys: String, CodingKey {
        case totalBytes = "total_bytes"
        case retainedBytes = "retained_bytes"
        case droppedBytes = "dropped_bytes"
        case decodeReplacements = "decode_replacements"
    }
}

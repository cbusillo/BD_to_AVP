import Foundation

struct WorkerWaitCommand: Encodable, Equatable, Sendable {
    let protocolVersion = WorkerJobSpec.protocolVersion
    let type = "job.keep_waiting"
    let commandID: UUID
    let jobID: UUID
    let toolRunID: UUID
    let stallEpisodeID: UUID

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case type
        case commandID = "command_id"
        case jobID = "job_id"
        case toolRunID = "tool_run_id"
        case stallEpisodeID = "stall_episode_id"
    }
}

struct WorkerStallEvent: Codable, Equatable, Sendable {
    enum State: String, Codable, Sendable {
        case stalled, extended, recovered, timedOut = "timed_out", ended
    }

    struct Artifact: Codable, Equatable, Sendable {
        let role: String
        let state: String
        let sizeBytes: Int64?
        let noProgressAgeSeconds: Double

        enum CodingKeys: String, CodingKey {
            case role, state
            case sizeBytes = "size_bytes"
            case noProgressAgeSeconds = "no_progress_age_seconds"
        }
    }

    let toolRunID: UUID
    let stallEpisodeID: UUID
    let tool: String
    let state: State
    let canExtend: Bool
    let grantsUsed: Int
    let maxGrants: Int
    let grantSeconds: Int
    let artifacts: [Artifact]
    let artifactsOmitted: Int?
    let toolProgress: [String: Double]?

    enum CodingKeys: String, CodingKey {
        case toolRunID = "tool_run_id"
        case stallEpisodeID = "stall_episode_id"
        case tool, state, artifacts
        case artifactsOmitted = "artifacts_omitted"
        case canExtend = "can_extend"
        case grantsUsed = "grants_used"
        case maxGrants = "max_grants"
        case grantSeconds = "grant_seconds"
        case toolProgress = "tool_progress"
    }

    var isActive: Bool { state == .stalled || state == .extended }

    var diagnosticDetails: String? {
        try? String(data: JSONEncoder().encode(self), encoding: .utf8)
    }
}

struct WorkerControlResult: Codable, Equatable, Sendable {
    let commandID: UUID?
    let toolRunID: UUID?
    let stallEpisodeID: UUID?
    let accepted: Bool
    let code: String
    let duplicate: Bool
    let grantsUsed: Int?
    let grantSeconds: Int?

    enum CodingKeys: String, CodingKey {
        case commandID = "command_id"
        case toolRunID = "tool_run_id"
        case stallEpisodeID = "stall_episode_id"
        case accepted, code, duplicate
        case grantsUsed = "grants_used"
        case grantSeconds = "grant_seconds"
    }

    var diagnosticDetails: String? {
        try? String(data: JSONEncoder().encode(self), encoding: .utf8)
    }
}

/// UI choices stay attached to their original job, tool run and stall episode.
struct StallRecoveryState: Equatable {
    struct Notice: Equatable, Identifiable {
        let jobID: UUID
        var stall: WorkerStallEvent
        var pendingCommand: WorkerWaitCommand?
        var message: String?

        var id: UUID { stall.toolRunID }
        var canRequestWait: Bool { stall.canExtend && pendingCommand == nil }
    }

    private(set) var jobID: UUID?
    private(set) var supportsWaiting = false
    private(set) var notices: [Notice] = []

    mutating func begin(jobID: UUID) {
        self = StallRecoveryState()
        self.jobID = jobID
    }

    mutating func receive(_ event: WorkerEvent) {
        guard event.jobID == jobID else { return }
        if event.type.isTerminal {
            self = StallRecoveryState()
            return
        }
        switch event.type {
        case .workerReady:
            supportsWaiting = event.payload.controlCapabilities?.contains("keep_waiting_v1") == true
        case .stageStarted:
            notices.removeAll()
        case .toolStall:
            guard let stall = event.payload.stall else { return }
            let index = notices.firstIndex { $0.stall.toolRunID == stall.toolRunID }
            if !stall.isActive {
                if let index, notices[index].stall.stallEpisodeID == stall.stallEpisodeID {
                    notices.remove(at: index)
                }
            } else if let index {
                if notices[index].stall.stallEpisodeID == stall.stallEpisodeID {
                    notices[index].stall = stall
                } else {
                    notices[index] = Notice(jobID: event.jobID, stall: stall)
                }
            } else if notices.count < 8 {
                notices.append(Notice(jobID: event.jobID, stall: stall))
            }
        case .controlResult:
            guard let result = event.payload.controlResult,
                  let index = notices.firstIndex(where: {
                      $0.pendingCommand?.commandID == result.commandID
                          && $0.stall.toolRunID == result.toolRunID
                          && $0.stall.stallEpisodeID == result.stallEpisodeID
                  }), notices[index].pendingCommand != nil else { return }
            notices[index].pendingCommand = nil
            notices[index].message = result.accepted
                ? "Extra time added. Waiting for video output to resume."
                : "Extra time was not added. The conversion may already have recovered or moved on."
        default:
            break
        }
    }

    mutating func requestWait(toolRunID: UUID, episodeID: UUID) -> WorkerWaitCommand? {
        guard supportsWaiting, let jobID,
              let index = notices.firstIndex(where: {
                  $0.id == toolRunID && $0.stall.stallEpisodeID == episodeID
              }), notices[index].canRequestWait else { return nil }
        let command = WorkerWaitCommand(
            commandID: UUID(), jobID: jobID, toolRunID: toolRunID, stallEpisodeID: episodeID
        )
        notices[index].pendingCommand = command
        notices[index].message = nil
        return command
    }

    mutating func writeFailed(_ command: WorkerWaitCommand) {
        guard jobID == command.jobID,
              let index = notices.firstIndex(where: { $0.pendingCommand == command }) else { return }
        notices[index].pendingCommand = nil
        notices[index].message = "The request could not be sent. Extra time has not been confirmed."
    }
}

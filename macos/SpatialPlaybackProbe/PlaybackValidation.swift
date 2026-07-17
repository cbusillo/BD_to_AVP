import CryptoKit
import Foundation

enum PlaybackValidationPhase: Equatable {
    case selectMovie
    case preparing
    case ready
    case running
    case observations
    case complete
}

enum PlaybackCheckID: String, Codable, CaseIterable {
    case stereoDecode
    case playerReady
    case renderingReady
    case spatialPresentation
    case beginningSeek
    case middleSeek
    case endSeek

    var title: String {
        switch self {
        case .stereoDecode:
            return "Vision Pro can decode 3D video"
        case .playerReady:
            return "Movie opens for playback"
        case .renderingReady:
            return "Picture is ready"
        case .spatialPresentation:
            return "3D presentation is active"
        case .beginningSeek:
            return "Beginning plays"
        case .middleSeek:
            return "Middle plays"
        case .endSeek:
            return "End plays"
        }
    }
}

enum PlaybackCheckStatus: String, Codable {
    case pending
    case running
    case passed
    case failed
}

struct PlaybackCheck: Identifiable, Codable, Equatable {
    let id: PlaybackCheckID
    var status: PlaybackCheckStatus
    var detail: String

    init(id: PlaybackCheckID, status: PlaybackCheckStatus = .pending, detail: String = "Not checked yet") {
        self.id = id
        self.status = status
        self.detail = detail
    }
}

enum PlaybackObservationAnswer: String, Codable, CaseIterable {
    case unanswered
    case yes
    case no
    case unsure

    var label: String {
        switch self {
        case .unanswered:
            return "Not answered"
        case .yes:
            return "Yes"
        case .no:
            return "No"
        case .unsure:
            return "Not sure"
        }
    }
}

struct PlaybackObservations: Codable, Equatable {
    var videoRemainedVisible: PlaybackObservationAnswer = .unanswered
    var appearedThreeDimensional: PlaybackObservationAnswer = .unanswered

    var isComplete: Bool {
        videoRemainedVisible != .unanswered && appearedThreeDimensional != .unanswered
    }
}

enum PlaybackValidationResult: String, Codable {
    case passed
    case needsReview
    case failed

    var title: String {
        switch self {
        case .passed:
            return "Playback check passed"
        case .needsReview:
            return "One result needs review"
        case .failed:
            return "Playback check found a problem"
        }
    }

    var summary: String {
        switch self {
        case .passed:
            return "The movie played spatially, survived all three seeks, and matched what you saw."
        case .needsReview:
            return "The automatic checks completed, but one observation was uncertain. The report preserves the details without treating this as approval."
        case .failed:
            return "At least one automatic check or visible playback observation failed. Review the details before using this movie as release evidence."
        }
    }

    var symbolName: String {
        switch self {
        case .passed:
            return "checkmark.seal.fill"
        case .needsReview:
            return "questionmark.diamond.fill"
        case .failed:
            return "xmark.octagon.fill"
        }
    }
}

struct PlaybackSourceSummary: Codable, Equatable {
    let fileName: String
    let sha256: String
    let sizeBytes: Int64?
    let durationSeconds: Double
    let audioOptionCount: Int
    let subtitleOptionCount: Int
}

struct PlaybackValidationReport: Codable, Equatable {
    let schemaVersion: Int
    let validatorVersion: String
    let validatorBuild: String
    let generatedAt: String
    let operatingSystem: String
    let source: PlaybackSourceSummary
    let automaticChecks: [PlaybackCheck]
    let observations: PlaybackObservations
    let result: PlaybackValidationResult
}

struct PlaybackSeekEvidence: Equatable {
    let seekFinished: Bool
    let targetErrorSeconds: Double
    let allowedTargetErrorSeconds: Double
    let playbackAdvanceSeconds: Double
    let requiredPlaybackAdvanceSeconds: Double
    let renderingReady: Bool
    let spatialPresentation: Bool
}

enum PlaybackValidationRules {
    static func result(
        checks: [PlaybackCheck],
        observations: PlaybackObservations
    ) -> PlaybackValidationResult {
        if checks.contains(where: { $0.status == .failed })
            || observations.videoRemainedVisible == .no
            || observations.appearedThreeDimensional == .no
        {
            return .failed
        }

        if checks.contains(where: { $0.status != .passed })
            || observations.videoRemainedVisible != .yes
            || observations.appearedThreeDimensional != .yes
        {
            return .needsReview
        }

        return .passed
    }

    static func seekPassed(_ evidence: PlaybackSeekEvidence) -> Bool {
        evidence.seekFinished
            && evidence.targetErrorSeconds <= evidence.allowedTargetErrorSeconds
            && evidence.playbackAdvanceSeconds >= evidence.requiredPlaybackAdvanceSeconds
            && evidence.renderingReady
            && evidence.spatialPresentation
    }
}

enum PlaybackArtifactHasher {
    static func sha256Hex(at url: URL) async throws -> String {
        try await Task.detached(priority: .utility) {
            let fileHandle = try FileHandle(forReadingFrom: url)
            defer {
                try? fileHandle.close()
            }

            var hasher = SHA256()
            while true {
                if Task.isCancelled {
                    throw CancellationError()
                }
                let data = try fileHandle.read(upToCount: 4 * 1_024 * 1_024) ?? Data()
                if data.isEmpty {
                    break
                }
                hasher.update(data: data)
            }

            return hasher.finalize().map { String(format: "%02x", $0) }.joined()
        }.value
    }
}

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

enum PlaybackPresentationExpectation: String, Codable, Equatable {
    static let environmentKey = "BD_TO_AVP_PROBE_EXPECTED_PRESENTATION"

    case stereo
    case spatial

    static func resolve(environment: [String: String]) -> Self {
        environment[environmentKey]?.lowercased() == spatial.rawValue ? .spatial : .stereo
    }

    var requiresSpatialPresentation: Bool {
        self == .spatial
    }

    func matches(isStereo: Bool, isSpatial: Bool) -> Bool {
        switch self {
        case .stereo:
            return isStereo && !isSpatial
        case .spatial:
            return isSpatial
        }
    }

    var technicalDescription: String {
        switch self {
        case .stereo:
            return "Stereo · Screen"
        case .spatial:
            return "Stereo · Spatial · Portal"
        }
    }

    var guidance: String {
        switch self {
        case .stereo:
            return "This run expects normal stereoscopic screen playback for a converted Blu-ray movie."
        case .spatial:
            return "This run expects the controlled spatial calibration fixture to enter portal presentation."
        }
    }
}

enum PlaybackCheckID: String, Codable, CaseIterable {
    case stereoDecode
    case playerReady
    case renderingReady
    case stereoPresentation
    case presentationMode
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
        case .stereoPresentation:
            return "Stereoscopic playback is active"
        case .presentationMode:
            return "3D presentation matches the movie type"
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
            return "The movie played in the expected 3D mode, survived all three seeks, and matched what you saw."
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

struct PlaybackPresentationSummary: Codable, Equatable {
    let expectation: PlaybackPresentationExpectation
    let viewingMode: String
    let spatialVideoMode: String
    let immersiveViewingMode: String
}

struct PlaybackValidationReport: Codable, Equatable {
    let schemaVersion: Int
    let validatorVersion: String
    let validatorBuild: String
    let generatedAt: String
    let operatingSystem: String
    let source: PlaybackSourceSummary
    let presentation: PlaybackPresentationSummary
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
    let stereoPresentation: Bool
    let spatialPresentation: Bool
    let requiresSpatialPresentation: Bool
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
            && evidence.stereoPresentation
            && (!evidence.requiresSpatialPresentation || evidence.spatialPresentation)
    }
}

struct PlaybackReportFiles: Equatable {
    let archiveURL: URL
    let latestURL: URL
}

enum PlaybackReportStore {
    static let directoryName = "PlaybackValidatorReports"
    static let latestFileName = "Latest-Playback-Report.json"

    static func write(
        _ data: Data,
        sourceFileName: String,
        generatedAt: Date,
        documentsDirectory: URL
    ) throws -> PlaybackReportFiles {
        let fileManager = FileManager.default
        let reportDirectory = documentsDirectory.appendingPathComponent(directoryName, isDirectory: true)
        try fileManager.createDirectory(at: reportDirectory, withIntermediateDirectories: true)

        let archiveURL = reportDirectory.appendingPathComponent(
            archiveFileName(sourceFileName: sourceFileName, generatedAt: generatedAt)
        )
        let latestURL = reportDirectory.appendingPathComponent(latestFileName)
        try data.write(to: archiveURL, options: .atomic)
        try data.write(to: latestURL, options: .atomic)
        return PlaybackReportFiles(archiveURL: archiveURL, latestURL: latestURL)
    }

    static func archiveFileName(sourceFileName: String, generatedAt: Date) -> String {
        let sourceBaseName = URL(fileURLWithPath: sourceFileName)
            .deletingPathExtension()
            .lastPathComponent
        let sanitizedSourceName = sanitizedFileNameComponent(sourceBaseName)

        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyyMMdd-HHmmss"

        return "BD-to-AVP-Playback-Check-\(sanitizedSourceName)-\(formatter.string(from: generatedAt)).json"
    }

    private static func sanitizedFileNameComponent(_ value: String) -> String {
        let replacedCharacters = value.map { character in
            character.isLetter || character.isNumber ? character : "-"
        }
        let collapsedValue = String(replacedCharacters)
            .split(separator: "-", omittingEmptySubsequences: true)
            .joined(separator: "-")
        return collapsedValue.isEmpty ? "Movie" : String(collapsedValue.prefix(64))
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

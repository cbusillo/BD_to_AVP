import Foundation

struct WorkerJobSpec: Encodable, Equatable {
    static let protocolVersion = 1

    struct Source: Encodable, Equatable {
        let path: String
    }

    struct ConversionSettings: Encodable, Equatable {
        struct Destination: Encodable, Equatable {
            let path: String
        }

        struct Video: Encodable, Equatable {
            let hevcQuality: Int
            let leftRightBitrate: Int
            let upscaleEnabled: Bool
            let upscaleQuality: Int
            let linkQuality: Bool
            let fieldOfView: Int
            let frameRateOverride: String?
            let resolutionOverride: String?
            let cropBlackBars: Bool
            let swapEyes: Bool

            enum CodingKeys: String, CodingKey {
                case hevcQuality = "hevc_quality"
                case leftRightBitrate = "left_right_bitrate"
                case upscaleEnabled = "upscale_enabled"
                case upscaleQuality = "upscale_quality"
                case linkQuality = "link_quality"
                case fieldOfView = "field_of_view"
                case frameRateOverride = "frame_rate_override"
                case resolutionOverride = "resolution_override"
                case cropBlackBars = "crop_black_bars"
                case swapEyes = "swap_eyes"
            }
        }

        struct Audio: Encodable, Equatable {
            let handling: String
            let bitrate: Int
            let language: String
            let includeSubtitles: Bool
            let keepExtraLanguages: Bool

            enum CodingKeys: String, CodingKey {
                case handling
                case bitrate
                case language
                case includeSubtitles = "include_subtitles"
                case keepExtraLanguages = "keep_extra_languages"
            }
        }

        struct Job: Encodable, Equatable {
            let startStage: Int
            let keepStageFiles: Bool
            let overwriteExisting: Bool
            let removeOriginalAfterSuccess: Bool
            let continueOnError: Bool
            let softwareEncoder: Bool
            let outputCommands: Bool
            let keepAwake: Bool
            let playSound: Bool

            enum CodingKeys: String, CodingKey {
                case startStage = "start_stage"
                case keepStageFiles = "keep_stage_files"
                case overwriteExisting = "overwrite_existing"
                case removeOriginalAfterSuccess = "remove_original_after_success"
                case continueOnError = "continue_on_error"
                case softwareEncoder = "software_encoder"
                case outputCommands = "output_commands"
                case keepAwake = "keep_awake"
                case playSound = "play_sound"
            }
        }

        let destination: Destination
        let outputLength: String
        let video: Video
        let audio: Audio
        let job: Job

        enum CodingKeys: String, CodingKey {
            case destination
            case outputLength = "output_length"
            case video
            case audio
            case job
        }
    }

    let protocolVersion: Int
    let type: String
    let jobID: UUID
    let operation: String
    let source: Source
    let conversionSettings: ConversionSettings?

    init(sourceURL: URL, jobID: UUID = UUID()) {
        protocolVersion = Self.protocolVersion
        type = "job.start"
        self.jobID = jobID
        operation = "inspect_source"
        source = Source(path: sourceURL.path)
        conversionSettings = nil
    }

    init(draft: ConversionDraft, jobID: UUID = UUID()) {
        protocolVersion = Self.protocolVersion
        type = "job.start"
        self.jobID = jobID
        operation = "convert_source"
        source = Source(path: draft.source.url.path)
        let enc = draft.options.encoding
        let job = draft.options.job
        conversionSettings = ConversionSettings(
            destination: ConversionSettings.Destination(path: draft.destinationURL.path),
            outputLength: "full_movie",
            video: ConversionSettings.Video(
                hevcQuality: enc.hevcQuality,
                leftRightBitrate: enc.leftRightBitrate,
                upscaleEnabled: enc.upscaleEnabled,
                upscaleQuality: enc.upscaleQuality,
                linkQuality: enc.linkQuality,
                fieldOfView: enc.fieldOfView,
                frameRateOverride: enc.frameRateOverride.isEmpty ? nil : enc.frameRateOverride,
                resolutionOverride: enc.resolutionOverride.isEmpty ? nil : enc.resolutionOverride,
                cropBlackBars: enc.cropBlackBars,
                swapEyes: enc.swapEyes
            ),
            audio: ConversionSettings.Audio(
                handling: enc.audioHandling == .transcodeAAC ? "transcode_aac" : "preserve",
                bitrate: enc.audioBitrate,
                language: enc.language.rawValue,
                includeSubtitles: enc.includeSubtitles,
                keepExtraLanguages: enc.keepExtraLanguages
            ),
            job: ConversionSettings.Job(
                startStage: job.startStage.rawValue,
                keepStageFiles: job.keepStageFiles,
                overwriteExisting: job.overwriteExisting,
                removeOriginalAfterSuccess: job.removeOriginalAfterSuccess,
                continueOnError: job.continueOnError,
                softwareEncoder: job.softwareEncoder,
                outputCommands: job.outputCommands,
                keepAwake: job.keepAwake,
                playSound: job.playSound
            )
        )
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(protocolVersion, forKey: .protocolVersion)
        try container.encode(type, forKey: .type)
        try container.encode(jobID, forKey: .jobID)
        try container.encode(operation, forKey: .operation)
        try container.encode(source, forKey: .source)
        try container.encodeIfPresent(conversionSettings, forKey: .conversionSettings)
    }

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case type
        case jobID = "job_id"
        case operation
        case source
        case conversionSettings = "conversion_settings"
    }
}

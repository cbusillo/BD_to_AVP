import Foundation

struct WorkerJobSpec: Encodable, Equatable {
    static let protocolVersion = 2

    struct Source: Encodable, Equatable {
        enum Kind: String, Encodable {
            case directFile = "direct_file"
            case discImage = "disc_image"
            case bluRayFolder = "blu_ray_folder"
        }

        let kind: Kind
        let path: String

        init(source: ConversionSource) {
            switch source.kind {
            case .discImage:
                kind = .discImage
            case .bluRayFolder:
                kind = .bluRayFolder
            case .matroska, .transportStream:
                kind = .directFile
            case .physicalDisc, .sourceFolder:
                preconditionFailure("Unsupported worker source kind: \(source.kind)")
            }
            path = source.url.path
        }

        init(fileURL: URL) {
            if fileURL.hasDirectoryPath || DiscSourceDetector.isBluRayFolder(fileURL) {
                kind = .bluRayFolder
            } else {
                kind = fileURL.pathExtension.lowercased() == "iso" ? .discImage : .directFile
            }
            path = fileURL.path
        }
    }

    struct Destination: Encodable, Equatable {
        let path: String
    }

    struct Encoding: Encodable, Equatable {
        let transcodeAudio: Bool
        let audioBitrate: Int
        let leftRightBitrate: Int
        let linkQuality: Bool
        let mvHEVCQuality: Int
        let upscaleQuality: Int
        let fieldOfView: Int
        let frameRate: String
        let resolution: String
        let skipSubtitles: Bool
        let cropBlackBars: Bool
        let swapEyes: Bool
        let fxUpscale: Bool
        let languageCode: String
        let removeExtraLanguages: Bool

        enum CodingKeys: String, CodingKey {
            case transcodeAudio = "transcode_audio"
            case audioBitrate = "audio_bitrate"
            case leftRightBitrate = "left_right_bitrate"
            case linkQuality = "link_quality"
            case mvHEVCQuality = "mv_hevc_quality"
            case upscaleQuality = "upscale_quality"
            case fieldOfView = "fov"
            case frameRate = "frame_rate"
            case resolution
            case skipSubtitles = "skip_subtitles"
            case cropBlackBars = "crop_black_bars"
            case swapEyes = "swap_eyes"
            case fxUpscale = "fx_upscale"
            case languageCode = "language_code"
            case removeExtraLanguages = "remove_extra_languages"
        }
    }

    struct Job: Encodable, Equatable {
        let startStage: Int
        let keepFiles: Bool
        let overwrite: Bool
        let removeOriginal: Bool
        let continueOnError: Bool
        let softwareEncoder: Bool
        let outputCommands: Bool
        let keepAwake: Bool
        let outputLength: String

        enum CodingKeys: String, CodingKey {
            case startStage = "start_stage"
            case keepFiles = "keep_files"
            case overwrite
            case removeOriginal = "remove_original"
            case continueOnError = "continue_on_error"
            case softwareEncoder = "software_encoder"
            case outputCommands = "output_commands"
            case keepAwake = "keep_awake"
            case outputLength = "output_length"
        }
    }

    let protocolVersion: Int
    let type: String
    let jobID: UUID
    let operation: String
    let source: Source
    let destination: Destination?
    let encoding: Encoding?
    let job: Job?

    init(source: ConversionSource, jobID: UUID = UUID()) {
        protocolVersion = Self.protocolVersion
        type = "job.start"
        self.jobID = jobID
        operation = "inspect_source"
        self.source = Source(source: source)
        destination = nil
        encoding = nil
        job = nil
    }

    init(sourceURL: URL, jobID: UUID = UUID()) {
        protocolVersion = Self.protocolVersion
        type = "job.start"
        self.jobID = jobID
        operation = "inspect_source"
        source = Source(fileURL: sourceURL)
        destination = nil
        encoding = nil
        job = nil
    }

    init(draft: ConversionDraft, jobID: UUID = UUID()) {
        protocolVersion = Self.protocolVersion
        type = "job.start"
        self.jobID = jobID
        operation = "convert_source"
        source = Source(source: draft.source)
        destination = Destination(path: draft.destinationURL.path)

        let encodingOptions = draft.options.encoding
        encoding = Encoding(
            transcodeAudio: encodingOptions.audioHandling == .transcodeAAC,
            audioBitrate: encodingOptions.audioBitrate,
            leftRightBitrate: encodingOptions.leftRightBitrate,
            linkQuality: encodingOptions.linkQuality,
            mvHEVCQuality: encodingOptions.hevcQuality,
            upscaleQuality: encodingOptions.upscaleQuality,
            fieldOfView: encodingOptions.fieldOfView,
            frameRate: encodingOptions.frameRateOverride,
            resolution: encodingOptions.resolutionOverride,
            skipSubtitles: !encodingOptions.includeSubtitles,
            cropBlackBars: encodingOptions.cropBlackBars,
            swapEyes: encodingOptions.swapEyes,
            fxUpscale: encodingOptions.upscaleEnabled,
            languageCode: encodingOptions.language.rawValue,
            removeExtraLanguages: !encodingOptions.keepExtraLanguages
        )

        let jobOptions = draft.options.job
        job = Job(
            startStage: jobOptions.startStage.rawValue,
            keepFiles: jobOptions.keepStageFiles,
            overwrite: jobOptions.overwriteExisting,
            removeOriginal: jobOptions.removeOriginalAfterSuccess,
            continueOnError: jobOptions.continueOnError,
            softwareEncoder: jobOptions.softwareEncoder,
            outputCommands: jobOptions.outputCommands,
            keepAwake: jobOptions.keepAwake,
            outputLength: "full_movie"
        )
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(protocolVersion, forKey: .protocolVersion)
        try container.encode(type, forKey: .type)
        try container.encode(jobID, forKey: .jobID)
        try container.encode(operation, forKey: .operation)
        try container.encode(source, forKey: .source)
        try container.encodeIfPresent(destination, forKey: .destination)
        try container.encodeIfPresent(encoding, forKey: .encoding)
        try container.encodeIfPresent(job, forKey: .job)
    }

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case type
        case jobID = "job_id"
        case operation
        case source
        case destination
        case encoding
        case job
    }
}

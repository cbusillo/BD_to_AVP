import Foundation

struct WorkerJobSpec: Encodable, Equatable {
    static let protocolVersion = 3

    struct Source: Encodable, Equatable {
        enum Kind: String, Encodable {
            case directFile = "direct_file"
            case discImage = "disc_image"
            case bluRayFolder = "blu_ray_folder"
            case physicalDisc = "physical_disc"
        }

        let kind: Kind
        let path: String

        init(source: ConversionSource) {
            switch source.kind {
            case .physicalDisc:
                kind = .physicalDisc
            case .discImage:
                kind = .discImage
            case .bluRayFolder:
                kind = .bluRayFolder
            case .matroska, .transportStream:
                kind = .directFile
            case .sourceFolder:
                preconditionFailure("Unsupported worker source kind: \(source.kind)")
            }
            path = source.workerSourcePath
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

        enum CodingKeys: String, CodingKey {
            case startStage = "start_stage"
            case keepFiles = "keep_files"
            case overwrite
            case removeOriginal = "remove_original"
            case continueOnError = "continue_on_error"
            case softwareEncoder = "software_encoder"
            case outputCommands = "output_commands"
            case keepAwake = "keep_awake"
        }
    }

    struct Preview: Encodable, Equatable {
        let parentJobID: UUID
        let position: String
        let durationSeconds: Int

        enum CodingKeys: String, CodingKey {
            case parentJobID = "parent_job_id"
            case position
            case durationSeconds = "duration_seconds"
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
    let preview: Preview?

    init(source: ConversionSource, jobID: UUID = UUID()) {
        protocolVersion = Self.protocolVersion
        type = "job.start"
        self.jobID = jobID
        operation = "inspect_source"
        self.source = Source(source: source)
        destination = nil
        encoding = nil
        job = nil
        preview = nil
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
        preview = nil
    }

    init(draft: ConversionDraft, jobID: UUID = UUID()) {
        protocolVersion = Self.protocolVersion
        type = "job.start"
        self.jobID = jobID
        operation = "convert_source"
        source = Source(source: draft.source)
        destination = Destination(path: draft.destinationURL.path)

        encoding = Self.encoding(from: draft.options.encoding)
        job = Self.conversionJob(from: draft)
        preview = nil
    }

    init(
        previewDraft: PreviewDraft,
        destinationURL: URL,
        jobID: UUID = UUID()
    ) {
        let conversion = previewDraft.conversion
        protocolVersion = Self.protocolVersion
        type = "job.start"
        self.jobID = jobID
        operation = "preview_source"
        source = Source(source: conversion.source)
        destination = Destination(path: destinationURL.path)
        encoding = Self.encoding(from: conversion.options.encoding)

        let jobOptions = conversion.options.job
        job = Job(
            startStage: 1,
            keepFiles: false,
            overwrite: true,
            removeOriginal: false,
            continueOnError: false,
            softwareEncoder: jobOptions.softwareEncoder,
            outputCommands: jobOptions.outputCommands,
            keepAwake: jobOptions.keepAwake
        )
        preview = Preview(
            parentJobID: previewDraft.parentJobID,
            position: previewDraft.samplePosition.rawValue,
            durationSeconds: previewDraft.durationSeconds
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
        try container.encodeIfPresent(preview, forKey: .preview)
    }

    private static func encoding(from options: EncodingOptions) -> Encoding {
        Encoding(
            transcodeAudio: options.audioHandling == .transcodeAAC,
            audioBitrate: options.audioBitrate,
            leftRightBitrate: options.leftRightBitrate,
            linkQuality: options.linkQuality,
            mvHEVCQuality: options.hevcQuality,
            upscaleQuality: options.upscaleQuality,
            fieldOfView: options.fieldOfView,
            frameRate: options.frameRateOverride,
            resolution: options.resolutionOverride,
            skipSubtitles: !options.includeSubtitles,
            cropBlackBars: options.cropBlackBars,
            swapEyes: options.swapEyes,
            fxUpscale: options.upscaleEnabled,
            languageCode: options.language.rawValue,
            removeExtraLanguages: !options.keepExtraLanguages
        )
    }

    private static func conversionJob(from draft: ConversionDraft) -> Job {
        let options = draft.options.job
        return Job(
            startStage: options.startStage.rawValue,
            keepFiles: options.keepStageFiles,
            overwrite: options.overwriteExisting,
            removeOriginal: draft.source.kind == .physicalDisc ? false : options.removeOriginalAfterSuccess,
            continueOnError: options.continueOnError,
            softwareEncoder: options.softwareEncoder,
            outputCommands: options.outputCommands,
            keepAwake: options.keepAwake
        )
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
        case preview
    }
}

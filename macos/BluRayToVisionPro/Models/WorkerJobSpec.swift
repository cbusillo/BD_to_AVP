import Foundation

struct WorkerJobSpec: Encodable, Equatable {
    static let protocolVersion = 10

    struct Source: Encodable, Equatable {
        enum Kind: String, Encodable {
            case directFile = "direct_file"
            case discImage = "disc_image"
            case bluRayFolder = "blu_ray_folder"
            case physicalDisc = "physical_disc"
        }

        let kind: Kind
        let path: String
        let titleID: String?

        init(source: ConversionSource, titleID: String? = nil) {
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
            self.titleID = titleID
        }

        init(fileURL: URL) {
            if fileURL.hasDirectoryPath || DiscSourceDetector.isBluRayFolder(fileURL) {
                kind = .bluRayFolder
            } else {
                kind = fileURL.pathExtension.lowercased() == "iso" ? .discImage : .directFile
            }
            path = fileURL.path
            titleID = nil
        }

        enum CodingKeys: String, CodingKey {
            case kind
            case path
            case titleID = "title_id"
        }
    }

    struct Destination: Encodable, Equatable {
        let path: String
    }

    struct Encoding: Encodable, Equatable {
        struct Audio: Encodable, Equatable {
            enum Mode: String, Encodable, Equatable {
                case automatic
                case convertAAC = "convert_aac"
                case pcm
            }

            let mode: Mode
            let bitrate: Int
            let preferredLanguage: String?

            func encode(to encoder: Encoder) throws {
                var container = encoder.container(keyedBy: CodingKeys.self)
                try container.encode(mode, forKey: .mode)
                try container.encode(bitrate, forKey: .bitrate)
                if let preferredLanguage {
                    try container.encode(preferredLanguage, forKey: .preferredLanguage)
                } else {
                    try container.encodeNil(forKey: .preferredLanguage)
                }
            }

            enum CodingKeys: String, CodingKey {
                case mode
                case bitrate
                case preferredLanguage = "preferred_language"
            }
        }

        struct Subtitles: Encodable, Equatable {
            let mode: SubtitleMode
            let preferredLanguage: String?

            func encode(to encoder: Encoder) throws {
                var container = encoder.container(keyedBy: CodingKeys.self)
                try container.encode(mode, forKey: .mode)
                if let preferredLanguage {
                    try container.encode(preferredLanguage, forKey: .preferredLanguage)
                } else {
                    try container.encodeNil(forKey: .preferredLanguage)
                }
            }

            enum CodingKeys: String, CodingKey {
                case mode
                case preferredLanguage = "preferred_language"
            }
        }

        struct Bitrate: Encodable, Equatable {
            let mode: BitrateMode
            let mbps: Int?

            func encode(to encoder: Encoder) throws {
                var container = encoder.container(keyedBy: CodingKeys.self)
                try container.encode(mode, forKey: .mode)
                if mode == .custom {
                    guard let mbps else {
                        throw EncodingError.invalidValue(
                            self,
                            EncodingError.Context(
                                codingPath: encoder.codingPath,
                                debugDescription: "Custom bitrate mode requires an Mbps value."
                            )
                        )
                    }
                    try container.encode(mbps, forKey: .mbps)
                }
            }

            enum CodingKeys: String, CodingKey {
                case mode
                case mbps
            }
        }

        struct Video: Encodable, Equatable {
            enum RouteIntent: String, Encodable, Equatable {
                case automatic
                case generated
                case encode
                case existingArtifact = "existing_artifact"
            }

            let mode: VideoOutputMode
            let routeIntent: RouteIntent
            let directBitrate: Bitrate?
            let generatedEyeBitrate: Bitrate?
            let generatedMergeQuality: Int?
            let av1CRF: Int?

            func encode(to encoder: Encoder) throws {
                var container = encoder.container(keyedBy: CodingKeys.self)
                try container.encode(mode, forKey: .mode)
                try container.encode(routeIntent, forKey: .routeIntent)
                try container.encodeIfPresent(directBitrate, forKey: .directBitrate)
                try container.encodeIfPresent(generatedEyeBitrate, forKey: .generatedEyeBitrate)
                try container.encodeIfPresent(generatedMergeQuality, forKey: .generatedMergeQuality)
                try container.encodeIfPresent(av1CRF, forKey: .av1CRF)
            }

            enum CodingKeys: String, CodingKey {
                case mode
                case routeIntent = "route_intent"
                case directBitrate = "direct_bitrate"
                case generatedEyeBitrate = "generated_eye_bitrate"
                case generatedMergeQuality = "generated_merge_quality"
                case av1CRF = "crf"
            }
        }

        struct Upscale: Encodable, Equatable {
            let enabled: Bool
            let quality: Int?

            func encode(to encoder: Encoder) throws {
                var container = encoder.container(keyedBy: CodingKeys.self)
                try container.encode(enabled, forKey: .enabled)
                if enabled {
                    guard let quality else {
                        throw EncodingError.invalidValue(
                            self,
                            EncodingError.Context(
                                codingPath: encoder.codingPath,
                                debugDescription: "Enabled upscale mode requires a quality value."
                            )
                        )
                    }
                    try container.encode(quality, forKey: .quality)
                }
            }

            enum CodingKeys: String, CodingKey {
                case enabled
                case quality
            }
        }

        let audio: Audio
        let video: Video
        let upscale: Upscale
        let fieldOfView: Int
        let frameRate: String
        let resolution: String
        let cropBlackBars: Bool
        let swapEyes: Bool
        let subtitles: Subtitles

        enum CodingKeys: String, CodingKey {
            case audio
            case video
            case upscale
            case fieldOfView = "fov"
            case frameRate = "frame_rate"
            case resolution
            case cropBlackBars = "crop_black_bars"
            case swapEyes = "swap_eyes"
            case subtitles
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
        source = Source(source: draft.source, titleID: Self.titleID(for: draft))
        destination = Destination(path: draft.destinationURL.path)

        let job = Self.conversionJob(from: draft)
        encoding = Self.encoding(from: draft.options.encoding, job: job)
        self.job = job
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
        source = Source(source: conversion.source, titleID: Self.titleID(for: conversion))
        destination = Destination(path: destinationURL.path)
        let jobOptions = conversion.options.job
        let job = Job(
            startStage: 1,
            keepFiles: false,
            overwrite: true,
            removeOriginal: false,
            continueOnError: false,
            softwareEncoder: jobOptions.softwareEncoder,
            outputCommands: jobOptions.outputCommands,
            keepAwake: jobOptions.keepAwake
        )
        encoding = Self.encoding(
            from: conversion.options.encoding,
            job: job,
            routeStartStage: jobOptions.startStage.rawValue,
            routeKeepFiles: jobOptions.intermediatePolicy.createsReusableArtifacts,
            allowsExistingArtifact: false
        )
        self.job = job
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

    private static func encoding(
        from options: EncodingOptions,
        job: Job,
        routeStartStage: Int? = nil,
        routeKeepFiles: Bool? = nil,
        allowsExistingArtifact: Bool = true
    ) -> Encoding {
        let isAV1Stereo = options.videoOutputMode == .av1Stereo
        return Encoding(
            audio: Encoding.Audio(
                mode: audioMode(from: options.audioHandling),
                bitrate: options.audioBitrate,
                preferredLanguage: audioPreferredLanguage(from: options.audioLanguages)
            ),
            video: videoOptions(
                from: options,
                job: job,
                routeStartStage: routeStartStage,
                routeKeepFiles: routeKeepFiles,
                allowsExistingArtifact: allowsExistingArtifact
            ),
            upscale: Encoding.Upscale(
                enabled: isAV1Stereo ? false : options.upscaleEnabled,
                quality: isAV1Stereo || !options.upscaleEnabled ? nil : options.upscaleQuality
            ),
            fieldOfView: options.fieldOfView,
            frameRate: options.frameRateOverride,
            resolution: isAV1Stereo ? "" : options.resolutionOverride,
            cropBlackBars: options.cropBlackBars,
            swapEyes: options.swapEyes,
            subtitles: subtitleOptions(from: options)
        )
    }

    private static func videoOptions(
        from options: EncodingOptions,
        job: Job,
        routeStartStage: Int?,
        routeKeepFiles: Bool?,
        allowsExistingArtifact: Bool
    ) -> Encoding.Video {
        let requestedStartStage = routeStartStage ?? job.startStage
        let keepsReusableArtifacts = routeKeepFiles ?? job.keepFiles
        if options.videoOutputMode == .av1Stereo {
            if allowsExistingArtifact, requestedStartStage > ConversionStage.combineToMVHEVC.rawValue {
                return Encoding.Video(
                    mode: .av1Stereo,
                    routeIntent: .existingArtifact,
                    directBitrate: nil,
                    generatedEyeBitrate: nil,
                    generatedMergeQuality: nil,
                    av1CRF: nil
                )
            }
            return Encoding.Video(
                mode: .av1Stereo,
                routeIntent: .encode,
                directBitrate: nil,
                generatedEyeBitrate: nil,
                generatedMergeQuality: nil,
                av1CRF: options.av1CRF
            )
        }

        if allowsExistingArtifact, requestedStartStage > ConversionStage.combineToMVHEVC.rawValue {
            return Encoding.Video(
                mode: .mvHEVC,
                routeIntent: .existingArtifact,
                directBitrate: nil,
                generatedEyeBitrate: nil,
                generatedMergeQuality: nil,
                av1CRF: nil
            )
        }

        let requiresGeneratedRoute = requestedStartStage >= ConversionStage.createLeftRightFiles.rawValue
            || keepsReusableArtifacts
            || job.softwareEncoder
            || options.upscaleEnabled
            || !(1 ... 180).contains(options.fieldOfView)
        if requiresGeneratedRoute {
            return Encoding.Video(
                mode: .mvHEVC,
                routeIntent: .generated,
                directBitrate: nil,
                generatedEyeBitrate: bitrate(from: options.mvHEVC.generatedEyeBitrate),
                generatedMergeQuality: options.mvHEVC.generatedMergeQuality,
                av1CRF: nil
            )
        }

        return Encoding.Video(
            mode: .mvHEVC,
            routeIntent: .automatic,
            directBitrate: bitrate(from: options.mvHEVC.directFinalBitrate),
            generatedEyeBitrate: nil,
            generatedMergeQuality: nil,
            av1CRF: nil
        )
    }

    private static func bitrate(from preference: BitratePreference) -> Encoding.Bitrate {
        switch preference.mode {
        case .automatic:
            Encoding.Bitrate(mode: .automatic, mbps: nil)
        case .custom:
            Encoding.Bitrate(mode: .custom, mbps: preference.customMbps)
        }
    }

    private static func audioMode(from handling: AudioHandling) -> Encoding.Audio.Mode {
        switch handling {
        case .automatic:
            .automatic
        case .convertAAC:
            .convertAAC
        case .pcm:
            .pcm
        }
    }

    private static func audioPreferredLanguage(from policy: AudioLanguagePolicy) -> String? {
        switch policy.mode {
        case .allLanguages:
            nil
        case .preferredOnly:
            policy.preferredLanguage.code
        }
    }

    private static func subtitleOptions(from options: EncodingOptions) -> Encoding.Subtitles {
        guard options.subtitles.mode != .off else {
            return Encoding.Subtitles(mode: .off, preferredLanguage: nil)
        }
        return Encoding.Subtitles(
            mode: options.subtitles.mode,
            preferredLanguage: options.subtitles.preferredLanguage.code
        )
    }

    private static func conversionJob(from draft: ConversionDraft) -> Job {
        let options = draft.options.job
        return Job(
            startStage: options.startStage.rawValue,
            keepFiles: options.intermediatePolicy.createsReusableArtifacts,
            overwrite: options.overwriteExisting,
            removeOriginal: draft.source.kind == .physicalDisc ? false : options.removeOriginalAfterSuccess,
            continueOnError: options.continueOnError,
            softwareEncoder: options.softwareEncoder,
            outputCommands: options.outputCommands,
            keepAwake: options.keepAwake
        )
    }

    private static func titleID(for draft: ConversionDraft) -> String? {
        guard draft.source.kind.isDiscWorkflow else {
            return nil
        }
        return draft.selectedTitle?.id ?? draft.sourceDetails?.mainTitle?.id
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

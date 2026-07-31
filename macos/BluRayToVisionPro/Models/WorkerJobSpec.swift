import Foundation

struct WorkerJobSpec: Encodable, Equatable {
    static let protocolVersion = 11

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

            struct QualityIntent: Encodable, Equatable {
                let mode: QualityIntentMode
                let step: QualityStep?
                let mappingVersion: Int?

                init(_ intent: VideoQualityIntent) {
                    mode = intent.mode
                    switch intent.mode {
                    case .ladder:
                        step = intent.lastLadderStep
                        mappingVersion = VideoQualityCatalog.mappingVersion
                    case .custom:
                        step = nil
                        mappingVersion = nil
                    }
                }

                func encode(to encoder: Encoder) throws {
                    var container = encoder.container(keyedBy: CodingKeys.self)
                    try container.encode(mode, forKey: .mode)
                    switch mode {
                    case .ladder:
                        guard let step,
                              mappingVersion == VideoQualityCatalog.mappingVersion,
                              VideoQualityCatalog.supportsCompleteMVHEVCIntent(step)
                        else {
                            throw EncodingError.invalidValue(
                                self,
                                EncodingError.Context(
                                    codingPath: encoder.codingPath,
                                    debugDescription: "Guided quality intent requires a supported step and mapping version."
                                )
                            )
                        }
                        try container.encode(step, forKey: .step)
                        try container.encode(mappingVersion, forKey: .mappingVersion)
                    case .custom:
                        guard step == nil, mappingVersion == nil else {
                            throw EncodingError.invalidValue(
                                self,
                                EncodingError.Context(
                                    codingPath: encoder.codingPath,
                                    debugDescription: "Custom quality intent cannot include a guided step or mapping version."
                                )
                            )
                        }
                    }
                }

                enum CodingKeys: String, CodingKey {
                    case mode
                    case step
                    case mappingVersion = "mapping_version"
                }
            }

            struct GeneratedFallback: Encodable, Equatable {
                let eyeBitrate: Bitrate
                let mergeQuality: Int

                func encode(to encoder: Encoder) throws {
                    guard (0 ... 100).contains(mergeQuality) else {
                        throw EncodingError.invalidValue(
                            self,
                            EncodingError.Context(
                                codingPath: encoder.codingPath,
                                debugDescription: "Generated fallback merge quality must be between 0 and 100."
                            )
                        )
                    }
                    var container = encoder.container(keyedBy: CodingKeys.self)
                    try container.encode(eyeBitrate, forKey: .eyeBitrate)
                    try container.encode(mergeQuality, forKey: .mergeQuality)
                }

                enum CodingKeys: String, CodingKey {
                    case eyeBitrate = "eye_bitrate"
                    case mergeQuality = "merge_quality"
                }
            }

            let mode: VideoOutputMode
            let routeIntent: RouteIntent
            let qualityIntent: QualityIntent?
            let directBitrate: Bitrate?
            let generatedEyeBitrate: Bitrate?
            let generatedMergeQuality: Int?
            let generatedFallback: GeneratedFallback?
            let av1CRF: Int?
            let existingArtifactUpscaleQuality: Int?
            let qualityStateError: VideoQualityStateError?

            func encode(to encoder: Encoder) throws {
                try validateShape(codingPath: encoder.codingPath)
                var container = encoder.container(keyedBy: CodingKeys.self)
                try container.encode(mode, forKey: .mode)
                try container.encode(routeIntent, forKey: .routeIntent)
                try container.encodeIfPresent(qualityIntent, forKey: .qualityIntent)
                try container.encodeIfPresent(directBitrate, forKey: .directBitrate)
                try container.encodeIfPresent(generatedEyeBitrate, forKey: .generatedEyeBitrate)
                try container.encodeIfPresent(generatedMergeQuality, forKey: .generatedMergeQuality)
                try container.encodeIfPresent(generatedFallback, forKey: .generatedFallback)
                try container.encodeIfPresent(av1CRF, forKey: .av1CRF)
            }

            private func validateShape(codingPath: [CodingKey]) throws {
                if let qualityStateError {
                    throw EncodingError.invalidValue(
                        self,
                        EncodingError.Context(
                            codingPath: codingPath,
                            debugDescription: qualityStateError.localizedDescription
                        )
                    )
                }
                let isValid: Bool
                switch (mode, routeIntent) {
                case (.mvHEVC, .automatic):
                    isValid = qualityIntent != nil
                        && directBitrate != nil
                        && generatedEyeBitrate == nil
                        && generatedMergeQuality == nil
                        && generatedFallback != nil
                        && av1CRF == nil
                case (.mvHEVC, .generated):
                    isValid = qualityIntent != nil
                        && directBitrate == nil
                        && generatedEyeBitrate != nil
                        && generatedMergeQuality != nil
                        && generatedFallback == nil
                        && av1CRF == nil
                case (.av1Stereo, .encode):
                    isValid = qualityIntent?.mode == .custom
                        && directBitrate == nil
                        && generatedEyeBitrate == nil
                        && generatedMergeQuality == nil
                        && generatedFallback == nil
                        && av1CRF != nil
                case (_, .existingArtifact):
                    isValid = directBitrate == nil
                        && generatedEyeBitrate == nil
                        && generatedMergeQuality == nil
                        && generatedFallback == nil
                        && av1CRF == nil
                        && ((qualityIntent == nil && existingArtifactUpscaleQuality == nil)
                            || (qualityIntent != nil && existingArtifactUpscaleQuality != nil))
                default:
                    isValid = false
                }
                guard isValid else {
                    throw EncodingError.invalidValue(
                        self,
                        EncodingError.Context(
                            codingPath: codingPath,
                            debugDescription: "Video route intent contains incompatible quality controls."
                        )
                    )
                }

                guard qualityIntent?.mode != .ladder
                    || ladderControlsMatchBalanced
                else {
                    throw EncodingError.invalidValue(
                        self,
                        EncodingError.Context(
                            codingPath: codingPath,
                            debugDescription: "Guided Balanced quality intent does not match its concrete route controls."
                        )
                    )
                }
            }

            private var ladderControlsMatchBalanced: Bool {
                switch routeIntent {
                case .automatic:
                    directBitrate?.mode == .automatic
                        && generatedFallback?.eyeBitrate.mode == .automatic
                        && generatedFallback?.mergeQuality == VideoQualityCatalog.balancedGeneratedMergeQuality
                case .generated:
                    generatedEyeBitrate?.mode == .automatic
                        && generatedMergeQuality == VideoQualityCatalog.balancedGeneratedMergeQuality
                case .existingArtifact:
                    existingArtifactUpscaleQuality == VideoQualityCatalog.balancedUpscaleQuality
                case .encode:
                    false
                }
            }

            enum CodingKeys: String, CodingKey {
                case mode
                case routeIntent = "route_intent"
                case qualityIntent = "quality_intent"
                case directBitrate = "direct_bitrate"
                case generatedEyeBitrate = "generated_eye_bitrate"
                case generatedMergeQuality = "generated_merge_quality"
                case generatedFallback = "generated_fallback"
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
        let requestedStartStage = routeStartStage ?? job.startStage
        let requestedKeepFiles = routeKeepFiles ?? job.keepFiles
        let requestedRoute = VideoRoutePlan(
            encoding: options,
            startStage: requestedStartStage,
            keepsReusableArtifacts: requestedKeepFiles,
            softwareEncoder: job.softwareEncoder,
            allowsExistingArtifact: allowsExistingArtifact
        )
        let usesExistingArtifactUpscale = requestedRoute.kind == .existingArtifact
            && requestedStartStage == ConversionStage.upscaleVideo.rawValue
            && options.videoOutputMode == .mvHEVC
            && options.upscaleEnabled
        let resolvedOptions: EncodingOptions
        let qualityStateError: VideoQualityStateError?
        do {
            if requestedRoute.kind == .existingArtifact {
                resolvedOptions = usesExistingArtifactUpscale
                    ? try normalizedExistingArtifactUpscaleQualityState(options)
                    : options
            } else {
                resolvedOptions = try options.normalizedQualityState()
            }
            qualityStateError = nil
        } catch let error as VideoQualityStateError {
            resolvedOptions = options
            qualityStateError = error
        } catch {
            resolvedOptions = options
            qualityStateError = .inconsistentMirrors
        }
        let isAV1Stereo = resolvedOptions.videoOutputMode == .av1Stereo
        let activeUpscaleEnabled = !isAV1Stereo
            && (usesExistingArtifactUpscale
                || (requestedRoute.kind != .existingArtifact && resolvedOptions.upscaleEnabled))
        return Encoding(
            audio: Encoding.Audio(
                mode: audioMode(from: resolvedOptions.audioHandling),
                bitrate: resolvedOptions.audioBitrate,
                preferredLanguage: audioPreferredLanguage(from: resolvedOptions.audioLanguages)
            ),
            video: videoOptions(
                from: resolvedOptions,
                job: job,
                routeStartStage: routeStartStage,
                routeKeepFiles: routeKeepFiles,
                allowsExistingArtifact: allowsExistingArtifact,
                qualityStateError: qualityStateError
            ),
            upscale: Encoding.Upscale(
                enabled: activeUpscaleEnabled,
                quality: activeUpscaleEnabled ? resolvedOptions.upscaleQuality : nil
            ),
            fieldOfView: resolvedOptions.fieldOfView,
            frameRate: resolvedOptions.frameRateOverride,
            resolution: isAV1Stereo ? "" : resolvedOptions.resolutionOverride,
            cropBlackBars: resolvedOptions.cropBlackBars,
            swapEyes: resolvedOptions.swapEyes,
            subtitles: subtitleOptions(from: resolvedOptions)
        )
    }

    private static func normalizedExistingArtifactUpscaleQualityState(
        _ options: EncodingOptions
    ) throws -> EncodingOptions {
        guard (0 ... 100).contains(options.upscaleQuality) else {
            throw VideoQualityStateError.invalidCustomValues
        }

        var normalized = options
        switch options.videoQuality.mode {
        case .custom:
            normalized.videoQuality.custom.upscaleQuality = options.upscaleQuality
        case .ladder:
            guard case let .upscale(quality) = VideoQualityCatalog.mapping(
                for: options.videoQuality.lastLadderStep,
                target: .fileUpscale
            ) else {
                throw VideoQualityStateError.unsupportedStep(options.videoQuality.lastLadderStep)
            }
            if quality != options.upscaleQuality {
                normalized.videoQuality.mode = .custom
                normalized.videoQuality.custom.upscaleQuality = options.upscaleQuality
            }
        }
        return normalized
    }

    private static func videoOptions(
        from options: EncodingOptions,
        job: Job,
        routeStartStage: Int?,
        routeKeepFiles: Bool?,
        allowsExistingArtifact: Bool,
        qualityStateError: VideoQualityStateError?
    ) -> Encoding.Video {
        let requestedStartStage = routeStartStage ?? job.startStage
        let keepsReusableArtifacts = routeKeepFiles ?? job.keepFiles
        let route = VideoRoutePlan(
            encoding: options,
            startStage: requestedStartStage,
            keepsReusableArtifacts: keepsReusableArtifacts,
            softwareEncoder: job.softwareEncoder,
            allowsExistingArtifact: allowsExistingArtifact
        )

        switch route.kind {
        case .av1Stereo:
            return Encoding.Video(
                mode: .av1Stereo,
                routeIntent: .encode,
                qualityIntent: Encoding.Video.QualityIntent(options.videoQuality),
                directBitrate: nil,
                generatedEyeBitrate: nil,
                generatedMergeQuality: nil,
                generatedFallback: nil,
                av1CRF: options.av1CRF,
                existingArtifactUpscaleQuality: nil,
                qualityStateError: qualityStateError
            )
        case .existingArtifact:
            let activeUpscaleQuality = requestedStartStage == ConversionStage.upscaleVideo.rawValue
                && options.videoOutputMode == .mvHEVC
                && options.upscaleEnabled
                ? options.upscaleQuality
                : nil
            return Encoding.Video(
                mode: options.videoOutputMode,
                routeIntent: .existingArtifact,
                qualityIntent: activeUpscaleQuality == nil
                    ? nil
                    : Encoding.Video.QualityIntent(options.videoQuality),
                directBitrate: nil,
                generatedEyeBitrate: nil,
                generatedMergeQuality: nil,
                generatedFallback: nil,
                av1CRF: nil,
                existingArtifactUpscaleQuality: activeUpscaleQuality,
                qualityStateError: qualityStateError
            )
        case .generatedMVHEVC:
            return Encoding.Video(
                mode: .mvHEVC,
                routeIntent: .generated,
                qualityIntent: Encoding.Video.QualityIntent(options.videoQuality),
                directBitrate: nil,
                generatedEyeBitrate: bitrate(from: options.mvHEVC.generatedEyeBitrate),
                generatedMergeQuality: options.mvHEVC.generatedMergeQuality,
                generatedFallback: nil,
                av1CRF: nil,
                existingArtifactUpscaleQuality: nil,
                qualityStateError: qualityStateError
            )
        case .directMVHEVC:
            return Encoding.Video(
                mode: .mvHEVC,
                routeIntent: .automatic,
                qualityIntent: Encoding.Video.QualityIntent(options.videoQuality),
                directBitrate: bitrate(from: options.mvHEVC.directFinalBitrate),
                generatedEyeBitrate: nil,
                generatedMergeQuality: nil,
                generatedFallback: Encoding.Video.GeneratedFallback(
                    eyeBitrate: bitrate(from: options.mvHEVC.generatedEyeBitrate),
                    mergeQuality: options.mvHEVC.generatedMergeQuality
                ),
                av1CRF: nil,
                existingArtifactUpscaleQuality: nil,
                qualityStateError: qualityStateError
            )
        }
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

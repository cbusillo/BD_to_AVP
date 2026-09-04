import Foundation

struct WorkerJobSpec: Encodable, Equatable {
    static let protocolVersion = 12

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
                              VideoQualityCatalog.supportsProfileMVHEVCIntent(step)
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
            let directQuality: Double?
            let generatedEyeBitrate: Bitrate?
            let generatedMergeQuality: Int?
            let generatedFallback: GeneratedFallback?
            let av1CRF: Int?
            let activeQualityTarget: VideoQualityTarget?
            let includesUpscale: Bool
            let activeUpscaleQuality: Int?
            let qualityStateError: VideoQualityStateError?

            func encode(to encoder: Encoder) throws {
                try validateShape(codingPath: encoder.codingPath)
                var container = encoder.container(keyedBy: CodingKeys.self)
                try container.encode(mode, forKey: .mode)
                try container.encode(routeIntent, forKey: .routeIntent)
                try container.encodeIfPresent(qualityIntent, forKey: .qualityIntent)
                try container.encodeIfPresent(directBitrate, forKey: .directBitrate)
                try container.encodeIfPresent(directQuality, forKey: .directQuality)
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
                        && (directBitrate?.mode == .automatic ? directQuality != nil : directQuality == nil)
                        && generatedEyeBitrate == nil
                        && generatedMergeQuality == nil
                        && av1CRF == nil
                        && activeQualityTarget == (includesUpscale ? .directMVHEVCMetalFX2x : .directMVHEVC)
                case (.mvHEVC, .generated):
                    isValid = qualityIntent != nil
                        && directBitrate == nil
                        && directQuality == nil
                        && generatedEyeBitrate != nil
                        && generatedMergeQuality != nil
                        && generatedFallback == nil
                        && av1CRF == nil
                        && activeQualityTarget == .generatedMVHEVC
                case (.av1Stereo, .encode):
                    isValid = qualityIntent?.mode == .custom
                        && directBitrate == nil
                        && directQuality == nil
                        && generatedEyeBitrate == nil
                        && generatedMergeQuality == nil
                        && generatedFallback == nil
                        && av1CRF != nil
                        && activeQualityTarget == nil
                        && !includesUpscale
                        && activeUpscaleQuality == nil
                case (_, .existingArtifact):
                    isValid = directBitrate == nil
                        && directQuality == nil
                        && generatedEyeBitrate == nil
                        && generatedMergeQuality == nil
                        && generatedFallback == nil
                        && av1CRF == nil
                        && ((qualityIntent == nil
                                && activeQualityTarget == nil
                                && !includesUpscale
                                && activeUpscaleQuality == nil)
                            || (qualityIntent != nil
                                && activeQualityTarget == .fileUpscale
                                && includesUpscale
                                && activeUpscaleQuality != nil))
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

                guard qualityIntent?.mode != .custom || customControlsAreComplete else {
                    throw EncodingError.invalidValue(
                        self,
                        EncodingError.Context(
                            codingPath: codingPath,
                            debugDescription: "Custom quality intent is missing active concrete route controls."
                        )
                    )
                }
                guard qualityIntent?.mode != .ladder || ladderControlsMatchRoute else {
                    throw EncodingError.invalidValue(
                        self,
                        EncodingError.Context(
                            codingPath: codingPath,
                            debugDescription: "Guided quality intent does not match the checked active-route mapping."
                        )
                    )
                }
            }

            private var customControlsAreComplete: Bool {
                switch routeIntent {
                case .automatic:
                    generatedFallback != nil && (!includesUpscale || activeUpscaleQuality != nil)
                case .generated:
                    !includesUpscale || activeUpscaleQuality != nil
                case .existingArtifact:
                    activeUpscaleQuality != nil
                case .encode:
                    true
                }
            }

            private var ladderControlsMatchRoute: Bool {
                guard let step = qualityIntent?.step,
                      let target = activeQualityTarget,
                      let mapping = VideoQualityCatalog.mapping(for: step, target: target)
                else {
                    return false
                }
                switch (routeIntent, mapping) {
                case let (.automatic, .direct(quality)):
                    guard directBitrate?.mode == .automatic,
                          directQuality == quality
                    else {
                        return false
                    }
                    if step == .balanced {
                        guard case let .generated(eyeBitrateMbps, mergeQuality) = VideoQualityCatalog.mapping(
                            for: step,
                            target: .generatedMVHEVC
                        ) else {
                            return false
                        }
                        let fallbackMatches = generatedFallback?.eyeBitrate.mode == .automatic
                            && generatedFallback?.eyeBitrate.mbps == nil
                            && eyeBitrateMbps == VideoQualityCatalog.balancedGeneratedEyeBitrateMbps
                            && generatedFallback?.mergeQuality == mergeQuality
                        let upscaleMatches = !includesUpscale
                            || activeUpscaleQuality == VideoQualityCatalog.balancedUpscaleQuality
                        return fallbackMatches && upscaleMatches
                    }
                    return generatedFallback == nil && activeUpscaleQuality == nil
                case let (.generated, .generated(eyeBitrateMbps, mergeQuality)):
                    let generatedMatches = generatedEyeBitrate?.mode == .automatic
                        && generatedEyeBitrate?.mbps == nil
                        && eyeBitrateMbps == VideoQualityCatalog.balancedGeneratedEyeBitrateMbps
                        && generatedMergeQuality == mergeQuality
                    guard generatedMatches else {
                        return false
                    }
                    if includesUpscale {
                        guard case let .upscale(quality) = VideoQualityCatalog.mapping(
                            for: step,
                            target: .fileUpscale
                        ) else {
                            return false
                        }
                        return activeUpscaleQuality == quality
                    }
                    return activeUpscaleQuality == nil
                case let (.existingArtifact, .upscale(quality)):
                    return activeUpscaleQuality == quality
                case (.encode, _), (.automatic, _), (.generated, _), (.existingArtifact, _):
                    return false
                }
            }

            enum CodingKeys: String, CodingKey {
                case mode
                case routeIntent = "route_intent"
                case qualityIntent = "quality_intent"
                case directBitrate = "direct_bitrate"
                case directQuality = "direct_quality"
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
                if enabled, let quality {
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

    struct LiveSource: Encodable, Equatable {
        let playlist: Int
        let audioPID: Int
        let replayWindowSeconds: Int
        let replayMaxBytes: Int
        let maximumPairs: Int?

        enum CodingKeys: String, CodingKey {
            case playlist
            case audioPID = "audio_pid"
            case replayWindowSeconds = "replay_window_seconds"
            case replayMaxBytes = "replay_max_bytes"
            case maximumPairs = "maximum_pairs"
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
    let liveSource: LiveSource?

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
        liveSource = nil
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
        liveSource = nil
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
        liveSource = nil
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
        liveSource = nil
    }

    init(
        liveSource source: ConversionSource,
        destinationURL: URL,
        playlist: Int,
        audioPID: Int,
        replayWindowSeconds: Int = 120,
        replayMaxBytes: Int = 512 * 1_024 * 1_024,
        maximumPairs: Int? = nil,
        jobID: UUID = UUID()
    ) {
        precondition(source.kind == .discImage || source.kind == .bluRayFolder)
        protocolVersion = Self.protocolVersion
        type = "job.start"
        self.jobID = jobID
        operation = "start_live_source"
        self.source = Source(source: source)
        destination = Destination(path: destinationURL.path)
        encoding = nil
        job = nil
        preview = nil
        liveSource = LiveSource(
            playlist: playlist,
            audioPID: audioPID,
            replayWindowSeconds: replayWindowSeconds,
            replayMaxBytes: replayMaxBytes,
            maximumPairs: maximumPairs
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
        try container.encodeIfPresent(liveSource, forKey: .liveSource)
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
        let normalizationError: VideoQualityStateError?
        do {
            if requestedRoute.kind == .existingArtifact {
                resolvedOptions = usesExistingArtifactUpscale
                    ? try normalizedExistingArtifactUpscaleQualityState(options)
                    : options
            } else {
                resolvedOptions = try options.normalizedQualityState()
            }
            normalizationError = nil
        } catch let error as VideoQualityStateError {
            resolvedOptions = options
            normalizationError = error
        } catch {
            resolvedOptions = options
            normalizationError = .inconsistentMirrors
        }
        let isAV1Stereo = resolvedOptions.videoOutputMode == .av1Stereo
        let activeUpscaleEnabled = !isAV1Stereo
            && (usesExistingArtifactUpscale
                || (requestedRoute.kind != .existingArtifact && resolvedOptions.upscaleEnabled))
        let qualityStateError = normalizationError
            ?? routeQualityStateError(
                for: resolvedOptions,
                route: requestedRoute,
                activeUpscaleEnabled: activeUpscaleEnabled
            )
        let activeUpscaleQuality = resolvedActiveUpscaleQuality(
            for: resolvedOptions,
            route: requestedRoute,
            activeUpscaleEnabled: activeUpscaleEnabled
        )
        return Encoding(
            audio: Encoding.Audio(
                mode: audioMode(from: resolvedOptions.audioHandling),
                bitrate: resolvedOptions.audioBitrate,
                preferredLanguage: audioPreferredLanguage(from: resolvedOptions.audioLanguages)
            ),
            video: videoOptions(
                from: resolvedOptions,
                route: requestedRoute,
                activeUpscaleEnabled: activeUpscaleEnabled,
                activeUpscaleQuality: activeUpscaleQuality,
                qualityStateError: qualityStateError
            ),
            upscale: Encoding.Upscale(
                enabled: activeUpscaleEnabled,
                quality: activeUpscaleQuality
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
        route: VideoRoutePlan,
        activeUpscaleEnabled: Bool,
        activeUpscaleQuality: Int?,
        qualityStateError: VideoQualityStateError?
    ) -> Encoding.Video {
        switch route.kind {
        case .av1Stereo:
            return Encoding.Video(
                mode: .av1Stereo,
                routeIntent: .encode,
                qualityIntent: Encoding.Video.QualityIntent(options.videoQuality),
                directBitrate: nil,
                directQuality: nil,
                generatedEyeBitrate: nil,
                generatedMergeQuality: nil,
                generatedFallback: nil,
                av1CRF: options.av1CRF,
                activeQualityTarget: nil,
                includesUpscale: false,
                activeUpscaleQuality: nil,
                qualityStateError: qualityStateError
            )
        case .existingArtifact:
            return Encoding.Video(
                mode: options.videoOutputMode,
                routeIntent: .existingArtifact,
                qualityIntent: !activeUpscaleEnabled
                    ? nil
                    : Encoding.Video.QualityIntent(options.videoQuality),
                directBitrate: nil,
                directQuality: nil,
                generatedEyeBitrate: nil,
                generatedMergeQuality: nil,
                generatedFallback: nil,
                av1CRF: nil,
                activeQualityTarget: activeUpscaleEnabled ? .fileUpscale : nil,
                includesUpscale: activeUpscaleEnabled,
                activeUpscaleQuality: activeUpscaleQuality,
                qualityStateError: qualityStateError
            )
        case .generatedMVHEVC:
            let generatedEyeBitrate: Encoding.Bitrate
            let generatedMergeQuality: Int
            switch options.videoQuality.mode {
            case .custom:
                generatedEyeBitrate = bitrate(from: options.mvHEVC.generatedEyeBitrate)
                generatedMergeQuality = options.mvHEVC.generatedMergeQuality
            case .ladder:
                if case let .generated(_, mergeQuality) = VideoQualityCatalog.mapping(
                    for: options.videoQuality.lastLadderStep,
                    target: .generatedMVHEVC
                ) {
                    generatedEyeBitrate = Encoding.Bitrate(mode: .automatic, mbps: nil)
                    generatedMergeQuality = mergeQuality
                } else {
                    generatedEyeBitrate = bitrate(from: options.mvHEVC.generatedEyeBitrate)
                    generatedMergeQuality = options.mvHEVC.generatedMergeQuality
                }
            }
            return Encoding.Video(
                mode: .mvHEVC,
                routeIntent: .generated,
                qualityIntent: Encoding.Video.QualityIntent(options.videoQuality),
                directBitrate: nil,
                directQuality: nil,
                generatedEyeBitrate: generatedEyeBitrate,
                generatedMergeQuality: generatedMergeQuality,
                generatedFallback: nil,
                av1CRF: nil,
                activeQualityTarget: .generatedMVHEVC,
                includesUpscale: activeUpscaleEnabled,
                activeUpscaleQuality: activeUpscaleQuality,
                qualityStateError: qualityStateError
            )
        case .directMVHEVC:
            let directTarget: VideoQualityTarget = activeUpscaleEnabled
                ? .directMVHEVCMetalFX2x
                : .directMVHEVC
            let directBitrate: Encoding.Bitrate
            let directQuality: Double?
            let generatedFallback: Encoding.Video.GeneratedFallback?
            switch options.videoQuality.mode {
            case .custom:
                directBitrate = bitrate(from: options.mvHEVC.directFinalBitrate)
                if options.mvHEVC.directFinalBitrate.mode == .automatic,
                   case let .direct(quality) = VideoQualityCatalog.mapping(for: .balanced, target: directTarget)
                {
                    directQuality = quality
                } else {
                    directQuality = nil
                }
                generatedFallback = Encoding.Video.GeneratedFallback(
                    eyeBitrate: bitrate(from: options.mvHEVC.generatedEyeBitrate),
                    mergeQuality: options.mvHEVC.generatedMergeQuality
                )
            case .ladder:
                directBitrate = Encoding.Bitrate(mode: .automatic, mbps: nil)
                if case let .direct(quality) = VideoQualityCatalog.mapping(
                    for: options.videoQuality.lastLadderStep,
                    target: directTarget
                ) {
                    directQuality = quality
                } else {
                    directQuality = nil
                }
                if case let .generated(_, mergeQuality) = VideoQualityCatalog.mapping(
                    for: options.videoQuality.lastLadderStep,
                    target: .generatedMVHEVC
                ) {
                    generatedFallback = Encoding.Video.GeneratedFallback(
                        eyeBitrate: Encoding.Bitrate(mode: .automatic, mbps: nil),
                        mergeQuality: mergeQuality
                    )
                } else {
                    generatedFallback = nil
                }
            }
            return Encoding.Video(
                mode: .mvHEVC,
                routeIntent: .automatic,
                qualityIntent: Encoding.Video.QualityIntent(options.videoQuality),
                directBitrate: directBitrate,
                directQuality: directQuality,
                generatedEyeBitrate: nil,
                generatedMergeQuality: nil,
                generatedFallback: generatedFallback,
                av1CRF: nil,
                activeQualityTarget: directTarget,
                includesUpscale: activeUpscaleEnabled,
                activeUpscaleQuality: activeUpscaleQuality,
                qualityStateError: qualityStateError
            )
        }
    }

    private static func routeQualityStateError(
        for options: EncodingOptions,
        route: VideoRoutePlan,
        activeUpscaleEnabled: Bool
    ) -> VideoQualityStateError? {
        guard options.videoQuality.mode == .ladder else {
            return nil
        }
        let step = options.videoQuality.lastLadderStep
        let supported: Bool
        switch route.kind {
        case .directMVHEVC:
            supported = VideoQualityCatalog.supports(
                step,
                for: activeUpscaleEnabled ? .directMVHEVCMetalFX2x : .directMVHEVC
            )
        case .generatedMVHEVC:
            supported = VideoQualityCatalog.supports(step, for: .generatedMVHEVC)
                && (!activeUpscaleEnabled || VideoQualityCatalog.supports(step, for: .fileUpscale))
        case .existingArtifact:
            supported = !activeUpscaleEnabled || VideoQualityCatalog.supports(step, for: .fileUpscale)
        case .av1Stereo:
            supported = false
        }
        return supported ? nil : .unsupportedStep(step)
    }

    private static func resolvedActiveUpscaleQuality(
        for options: EncodingOptions,
        route: VideoRoutePlan,
        activeUpscaleEnabled: Bool
    ) -> Int? {
        guard activeUpscaleEnabled else {
            return nil
        }
        if options.videoQuality.mode == .custom {
            return options.upscaleQuality
        }
        let step = options.videoQuality.lastLadderStep
        if route.kind == .directMVHEVC, step != .balanced {
            return nil
        }
        guard case let .upscale(quality) = VideoQualityCatalog.mapping(for: step, target: .fileUpscale) else {
            return nil
        }
        return quality
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
        case liveSource = "live_source"
    }
}

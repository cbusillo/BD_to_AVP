import Foundation

enum ConversionSetupTab: String, CaseIterable, Identifiable {
    case video
    case audioAndSubtitles
    case filesAndRecovery

    var id: String { rawValue }

    var title: String {
        switch self {
        case .video:
            "Video"
        case .audioAndSubtitles:
            "Audio & Subtitles"
        case .filesAndRecovery:
            "Files & Recovery"
        }
    }
}

enum AudioHandling: String, CaseIterable, Codable, Identifiable {
    case automatic
    case convertAAC = "transcodeAAC"
    case pcm = "preserve"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .automatic:
            "Automatic"
        case .convertAAC:
            "Convert to AAC"
        case .pcm:
            "Uncompressed PCM"
        }
    }

    var detail: String {
        switch self {
        case .automatic:
            "Copies the retained audio tracks when every retained track is qualified AAC; otherwise converts the retained set to AAC."
        case .convertAAC:
            "Converts the retained audio tracks to AAC."
        case .pcm:
            "Decodes the retained audio tracks to uncompressed PCM."
        }
    }

    var bitrateLabel: String? {
        switch self {
        case .automatic:
            "AAC fallback bitrate"
        case .convertAAC:
            "AAC bitrate"
        case .pcm:
            nil
        }
    }

    func compactSummary(bitrate: Int) -> String {
        switch self {
        case .automatic:
            "automatic audio (AAC fallback \(bitrate) kbps)"
        case .convertAAC:
            "AAC \(bitrate) kbps"
        case .pcm:
            "uncompressed PCM audio"
        }
    }
}

enum AudioLanguageMode: String, CaseIterable, Codable, Identifiable {
    case allLanguages = "all_languages"
    case preferredOnly = "preferred_only"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .allLanguages:
            "All Languages"
        case .preferredOnly:
            "Preferred Language Only"
        }
    }

    var detail: String {
        switch self {
        case .allLanguages:
            "Retains every source audio track in source order."
        case .preferredOnly:
            "Retains every track whose metadata language matches the preferred audio language. If none match, the source-default track is retained, or the first track when no default is declared."
        }
    }
}

struct AudioLanguagePolicy: Codable, Equatable {
    var mode = AudioLanguageMode.preferredOnly
    var preferredLanguage = MediaLanguage.english

    var summary: String {
        switch mode {
        case .allLanguages:
            "all languages"
        case .preferredOnly:
            "\(preferredLanguage.name) only"
        }
    }
}

enum SubtitleMode: String, CaseIterable, Codable, Identifiable {
    case off
    case preferredOnly = "preferred_only"
    case preferredPlusOthers = "preferred_plus_others"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .off:
            "Off"
        case .preferredOnly:
            "Preferred Only"
        case .preferredPlusOthers:
            "Preferred + Others"
        }
    }

    var detail: String {
        switch self {
        case .off:
            "No subtitle tracks will be extracted."
        case .preferredOnly:
            "Only the preferred language is retained when matching subtitles are available."
        case .preferredPlusOthers:
            "The preferred language is prioritized while other available subtitle tracks are retained."
        }
    }
}

struct SubtitlePolicy: Codable, Equatable {
    var mode = SubtitleMode.preferredPlusOthers
    var preferredLanguage = MediaLanguage.english

    var summary: String {
        switch mode {
        case .off:
            "off"
        case .preferredOnly:
            "\(preferredLanguage.name) only"
        case .preferredPlusOthers:
            "\(preferredLanguage.name) + others"
        }
    }
}

enum VideoOutputMode: String, CaseIterable, Codable, Identifiable {
    case mvHEVC = "mv_hevc"
    case av1Stereo = "av1_sbs"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .mvHEVC:
            "Apple Spatial (MV-HEVC)"
        case .av1Stereo:
            "AV1 Stereo (Software)"
        }
    }

    var detail: String {
        switch self {
        case .mvHEVC:
            "Native Apple spatial-video output using hardware HEVC encoding by default."
        case .av1Stereo:
            "Full-resolution side-by-side AV1 with Apple stereo metadata. Encoding is software-only and may take substantially longer."
        }
    }

    var outputFileTag: String {
        switch self {
        case .mvHEVC:
            "_AVP"
        case .av1Stereo:
            "_AV1_Stereo"
        }
    }
}

enum ConversionStage: Int, CaseIterable, Codable, Identifiable {
    case createMKV = 1
    case extractMVCAndAudio
    case extractSubtitles
    case createLeftRightFiles
    case combineToMVHEVC
    case upscaleVideo
    case transcodeAudio
    case createFinalFile
    case moveFiles

    var id: Int { rawValue }

    var title: String {
        switch self {
        case .createMKV:
            "1 — Create MKV"
        case .extractMVCAndAudio:
            "2 — Extract MVC and Audio"
        case .extractSubtitles:
            "3 — Extract Subtitles"
        case .createLeftRightFiles:
            "4 — Encode Stereo Video"
        case .combineToMVHEVC:
            "5 — Finalize Stereo Video"
        case .upscaleVideo:
            "6 — Upscale Video"
        case .transcodeAudio:
            "7 — Prepare Audio"
        case .createFinalFile:
            "8 — Create Final File"
        case .moveFiles:
            "9 — Move Finished File"
        }
    }
}

enum BitrateMode: String, Codable {
    case automatic
    case custom
}

struct BitratePreference: Codable, Equatable {
    var mode: BitrateMode
    var customMbps: Int?

    init(mode: BitrateMode = .automatic, customMbps: Int? = nil) {
        self.mode = mode
        self.customMbps = customMbps
    }
}

struct MVHEVCOptions: Codable, Equatable {
    static let defaultGeneratedEyeBitrate = 20

    var directFinalBitrate = BitratePreference()
    var generatedEyeBitrate = BitratePreference(
        mode: .automatic,
        customMbps: defaultGeneratedEyeBitrate
    )
    var generatedMergeQuality = 75
    var linkGeneratedAndUpscaleQuality = true

    static func migrated(
        generatedMergeQuality: Int,
        generatedEyeBitrate: Int,
        linkGeneratedAndUpscaleQuality: Bool
    ) -> MVHEVCOptions {
        MVHEVCOptions(
            generatedEyeBitrate: BitratePreference(
                mode: generatedEyeBitrate == defaultGeneratedEyeBitrate ? .automatic : .custom,
                customMbps: generatedEyeBitrate
            ),
            generatedMergeQuality: generatedMergeQuality,
            linkGeneratedAndUpscaleQuality: linkGeneratedAndUpscaleQuality
        )
    }
}

enum IntermediatePolicy: String, Codable {
    case automatic
    case reusable

    init(legacyKeepStageFiles: Bool) {
        self = legacyKeepStageFiles ? .reusable : .automatic
    }

    var createsReusableArtifacts: Bool {
        self == .reusable
    }
}

struct EncodingOptions: Codable, Equatable {
    var videoOutputMode: VideoOutputMode
    var av1CRF: Int
    var mvHEVC: MVHEVCOptions
    var upscaleEnabled: Bool
    var upscaleQuality: Int
    var fieldOfView: Int
    var frameRateOverride: String
    var resolutionOverride: String
    var cropBlackBars: Bool
    var swapEyes: Bool

    var audioHandling: AudioHandling
    var audioBitrate: Int
    var audioLanguages: AudioLanguagePolicy
    var subtitles: SubtitlePolicy

    init(
        videoOutputMode: VideoOutputMode = .mvHEVC,
        av1CRF: Int = 32,
        mvHEVC: MVHEVCOptions = MVHEVCOptions(),
        upscaleEnabled: Bool = false,
        upscaleQuality: Int = 75,
        fieldOfView: Int = 90,
        frameRateOverride: String = "",
        resolutionOverride: String = "",
        cropBlackBars: Bool = false,
        swapEyes: Bool = false,
        audioHandling: AudioHandling = .automatic,
        audioBitrate: Int = 384,
        audioLanguages: AudioLanguagePolicy = AudioLanguagePolicy(),
        subtitles: SubtitlePolicy = SubtitlePolicy()
    ) {
        self.videoOutputMode = videoOutputMode
        self.av1CRF = av1CRF
        self.mvHEVC = mvHEVC
        self.upscaleEnabled = upscaleEnabled
        self.upscaleQuality = upscaleQuality
        self.fieldOfView = fieldOfView
        self.frameRateOverride = frameRateOverride
        self.resolutionOverride = resolutionOverride
        self.cropBlackBars = cropBlackBars
        self.swapEyes = swapEyes
        self.audioHandling = audioHandling
        self.audioBitrate = audioBitrate
        self.audioLanguages = audioLanguages
        self.subtitles = subtitles
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        videoOutputMode = try container.decode(VideoOutputMode.self, forKey: .videoOutputMode)
        av1CRF = try container.decode(Int.self, forKey: .av1CRF)
        let legacyMergeQuality = try container.decode(Int.self, forKey: .hevcQuality)
        let legacyEyeBitrate = try container.decode(Int.self, forKey: .leftRightBitrate)
        let legacyLinkQuality = try container.decode(Bool.self, forKey: .linkQuality)
        if let currentMVHEVC = try container.decodeIfPresent(MVHEVCOptions.self, forKey: .mvHEVC) {
            guard currentMVHEVC.directFinalBitrate.mode != .custom
                    || currentMVHEVC.directFinalBitrate.customMbps != nil,
                  currentMVHEVC.generatedEyeBitrate.mode != .custom
                    || currentMVHEVC.generatedEyeBitrate.customMbps != nil,
                  currentMVHEVC.generatedMergeQuality == legacyMergeQuality,
                  (currentMVHEVC.generatedEyeBitrate.customMbps
                    ?? MVHEVCOptions.defaultGeneratedEyeBitrate) == legacyEyeBitrate,
                  currentMVHEVC.linkGeneratedAndUpscaleQuality == legacyLinkQuality
            else {
                throw DecodingError.dataCorruptedError(
                    forKey: .mvHEVC,
                    in: container,
                    debugDescription: "MV-HEVC intent does not match its version-4 compatibility keys."
                )
            }
            mvHEVC = currentMVHEVC
        } else {
            mvHEVC = MVHEVCOptions.migrated(
                generatedMergeQuality: legacyMergeQuality,
                generatedEyeBitrate: legacyEyeBitrate,
                linkGeneratedAndUpscaleQuality: legacyLinkQuality
            )
        }
        upscaleEnabled = try container.decode(Bool.self, forKey: .upscaleEnabled)
        upscaleQuality = try container.decode(Int.self, forKey: .upscaleQuality)
        fieldOfView = try container.decode(Int.self, forKey: .fieldOfView)
        frameRateOverride = try container.decode(String.self, forKey: .frameRateOverride)
        resolutionOverride = try container.decode(String.self, forKey: .resolutionOverride)
        cropBlackBars = try container.decode(Bool.self, forKey: .cropBlackBars)
        swapEyes = try container.decode(Bool.self, forKey: .swapEyes)
        audioHandling = try container.decode(AudioHandling.self, forKey: .audioHandling)
        audioBitrate = try container.decode(Int.self, forKey: .audioBitrate)
        audioLanguages = try container.decode(AudioLanguagePolicy.self, forKey: .audioLanguages)
        subtitles = try container.decode(SubtitlePolicy.self, forKey: .subtitles)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(videoOutputMode, forKey: .videoOutputMode)
        try container.encode(av1CRF, forKey: .av1CRF)
        try container.encode(mvHEVC, forKey: .mvHEVC)
        try container.encode(mvHEVC.generatedMergeQuality, forKey: .hevcQuality)
        try container.encode(generatedEyeCustomBitrateMbps, forKey: .leftRightBitrate)
        try container.encode(mvHEVC.linkGeneratedAndUpscaleQuality, forKey: .linkQuality)
        try container.encode(upscaleEnabled, forKey: .upscaleEnabled)
        try container.encode(upscaleQuality, forKey: .upscaleQuality)
        try container.encode(fieldOfView, forKey: .fieldOfView)
        try container.encode(frameRateOverride, forKey: .frameRateOverride)
        try container.encode(resolutionOverride, forKey: .resolutionOverride)
        try container.encode(cropBlackBars, forKey: .cropBlackBars)
        try container.encode(swapEyes, forKey: .swapEyes)
        try container.encode(audioHandling, forKey: .audioHandling)
        try container.encode(audioBitrate, forKey: .audioBitrate)
        try container.encode(audioLanguages, forKey: .audioLanguages)
        try container.encode(subtitles, forKey: .subtitles)
    }

    var generatedEyeCustomBitrateMbps: Int {
        mvHEVC.generatedEyeBitrate.customMbps ?? MVHEVCOptions.defaultGeneratedEyeBitrate
    }

    var audioSummary: String {
        "\(audioHandling.compactSummary(bitrate: audioBitrate)), \(audioLanguages.summary)"
    }

    var subtitleSummary: String {
        subtitles.summary
    }

    var videoSummary: String {
        let resolution = upscaleEnabled ? "2× upscale" : "source resolution"
        return switch videoOutputMode {
        case .mvHEVC:
            "MV-HEVC \(mvHEVC.generatedMergeQuality) · \(generatedEyeCustomBitrateMbps) Mbps eyes · \(resolution)"
        case .av1Stereo:
            "AV1 stereo CRF \(av1CRF) · full side-by-side · source resolution"
        }
    }

    var compactSummary: String {
        "\(videoSummary) · Audio: \(audioSummary) · Subtitles: \(subtitleSummary)"
    }

    private enum CodingKeys: String, CodingKey {
        case videoOutputMode
        case av1CRF
        case mvHEVC
        case hevcQuality
        case leftRightBitrate
        case upscaleEnabled
        case upscaleQuality
        case linkQuality
        case fieldOfView
        case frameRateOverride
        case resolutionOverride
        case cropBlackBars
        case swapEyes
        case audioHandling
        case audioBitrate
        case audioLanguages
        case subtitles
    }
}

struct JobOptions: Codable, Equatable {
    var startStage: ConversionStage
    var intermediatePolicy: IntermediatePolicy
    var overwriteExisting: Bool
    var removeOriginalAfterSuccess: Bool
    var continueOnError: Bool
    var softwareEncoder: Bool
    var outputCommands: Bool
    var keepAwake: Bool
    var playSound: Bool

    init(
        startStage: ConversionStage = .createMKV,
        intermediatePolicy: IntermediatePolicy = .automatic,
        overwriteExisting: Bool = false,
        removeOriginalAfterSuccess: Bool = false,
        continueOnError: Bool = false,
        softwareEncoder: Bool = false,
        outputCommands: Bool = false,
        keepAwake: Bool = true,
        playSound: Bool = true
    ) {
        self.startStage = startStage
        self.intermediatePolicy = intermediatePolicy
        self.overwriteExisting = overwriteExisting
        self.removeOriginalAfterSuccess = removeOriginalAfterSuccess
        self.continueOnError = continueOnError
        self.softwareEncoder = softwareEncoder
        self.outputCommands = outputCommands
        self.keepAwake = keepAwake
        self.playSound = playSound
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        startStage = try container.decode(ConversionStage.self, forKey: .startStage)
        let legacyKeepStageFiles = try container.decode(Bool.self, forKey: .keepStageFiles)
        if let currentPolicy = try container.decodeIfPresent(IntermediatePolicy.self, forKey: .intermediatePolicy) {
            guard currentPolicy.createsReusableArtifacts == legacyKeepStageFiles else {
                throw DecodingError.dataCorruptedError(
                    forKey: .intermediatePolicy,
                    in: container,
                    debugDescription: "Intermediate policy does not match its compatibility key."
                )
            }
            intermediatePolicy = currentPolicy
        } else {
            intermediatePolicy = IntermediatePolicy(legacyKeepStageFiles: legacyKeepStageFiles)
        }
        overwriteExisting = try container.decode(Bool.self, forKey: .overwriteExisting)
        removeOriginalAfterSuccess = try container.decode(Bool.self, forKey: .removeOriginalAfterSuccess)
        continueOnError = try container.decode(Bool.self, forKey: .continueOnError)
        softwareEncoder = try container.decode(Bool.self, forKey: .softwareEncoder)
        outputCommands = try container.decode(Bool.self, forKey: .outputCommands)
        keepAwake = try container.decode(Bool.self, forKey: .keepAwake)
        playSound = try container.decode(Bool.self, forKey: .playSound)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(startStage, forKey: .startStage)
        try container.encode(intermediatePolicy, forKey: .intermediatePolicy)
        try container.encode(intermediatePolicy.createsReusableArtifacts, forKey: .keepStageFiles)
        try container.encode(overwriteExisting, forKey: .overwriteExisting)
        try container.encode(removeOriginalAfterSuccess, forKey: .removeOriginalAfterSuccess)
        try container.encode(continueOnError, forKey: .continueOnError)
        try container.encode(softwareEncoder, forKey: .softwareEncoder)
        try container.encode(outputCommands, forKey: .outputCommands)
        try container.encode(keepAwake, forKey: .keepAwake)
        try container.encode(playSound, forKey: .playSound)
    }

    private enum CodingKeys: String, CodingKey {
        case startStage
        case intermediatePolicy
        case keepStageFiles
        case overwriteExisting
        case removeOriginalAfterSuccess
        case continueOnError
        case softwareEncoder
        case outputCommands
        case keepAwake
        case playSound
    }
}

struct ConversionOptions: Codable, Equatable {
    var encoding = EncodingOptions()
    var job = JobOptions()

    var compactSummary: String {
        encoding.compactSummary
    }
}

extension BuiltInProfile {
    var options: EncodingOptions {
        switch self {
        case .balanced:
            EncodingOptions()
        case .originalResolution:
            EncodingOptions(
                mvHEVC: MVHEVCOptions(
                    generatedMergeQuality: 85,
                    linkGeneratedAndUpscaleQuality: true
                ),
                upscaleEnabled: false,
                upscaleQuality: 85,
                fieldOfView: 90
            )
        case .fourKUpscale:
            EncodingOptions(
                mvHEVC: MVHEVCOptions(
                    generatedMergeQuality: 80,
                    linkGeneratedAndUpscaleQuality: true
                ),
                upscaleEnabled: true,
                upscaleQuality: 80,
                fieldOfView: 90,
                resolutionOverride: "3840x2160"
            )
        }
    }
}

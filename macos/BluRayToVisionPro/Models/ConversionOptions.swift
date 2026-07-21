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
    var mode = AudioLanguageMode.allLanguages
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

struct EncodingOptions: Codable, Equatable {
    var videoOutputMode = VideoOutputMode.mvHEVC
    var av1CRF = 32
    var hevcQuality = 75
    var leftRightBitrate = 20
    var upscaleEnabled = false
    var upscaleQuality = 75
    var linkQuality = true
    var fieldOfView = 90
    var frameRateOverride = ""
    var resolutionOverride = ""
    var cropBlackBars = false
    var swapEyes = false

    var audioHandling = AudioHandling.automatic
    var audioBitrate = 384
    var audioLanguages = AudioLanguagePolicy()
    var subtitles = SubtitlePolicy()

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
            "MV-HEVC \(hevcQuality) · \(leftRightBitrate) Mbps eyes · \(resolution)"
        case .av1Stereo:
            "AV1 stereo CRF \(av1CRF) · full side-by-side · source resolution"
        }
    }

    var compactSummary: String {
        "\(videoSummary) · Audio: \(audioSummary) · Subtitles: \(subtitleSummary)"
    }
}

struct JobOptions: Codable, Equatable {
    var startStage = ConversionStage.createMKV
    var keepStageFiles = false
    var overwriteExisting = false
    var removeOriginalAfterSuccess = false
    var continueOnError = false
    var softwareEncoder = false
    var outputCommands = false
    var keepAwake = true
    var playSound = true
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
                hevcQuality: 85,
                upscaleEnabled: false,
                upscaleQuality: 85,
                linkQuality: true,
                fieldOfView: 90
            )
        case .fourKUpscale:
            EncodingOptions(
                hevcQuality: 80,
                upscaleEnabled: true,
                upscaleQuality: 80,
                linkQuality: true,
                fieldOfView: 90,
                resolutionOverride: "3840x2160"
            )
        }
    }
}

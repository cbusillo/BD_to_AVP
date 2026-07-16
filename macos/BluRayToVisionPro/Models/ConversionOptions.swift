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
    case preserve
    case transcodeAAC

    var id: String { rawValue }

    var title: String {
        switch self {
        case .preserve:
            "Uncompressed PCM"
        case .transcodeAAC:
            "Transcode to AAC"
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
    var preferredLanguage = SubtitleLanguage.english
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
            "4 — Create Left / Right Video"
        case .combineToMVHEVC:
            "5 — Create Spatial Video"
        case .upscaleVideo:
            "6 — Upscale Video"
        case .transcodeAudio:
            "7 — Transcode Audio"
        case .createFinalFile:
            "8 — Create Final File"
        case .moveFiles:
            "9 — Move Finished File"
        }
    }
}

struct EncodingOptions: Codable, Equatable {
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

    var audioHandling = AudioHandling.preserve
    var audioBitrate = 384
    var subtitles = SubtitlePolicy()

    var compactSummary: String {
        let resolution = upscaleEnabled ? "2× upscale" : "source resolution"
        let audio = audioHandling == .preserve ? "uncompressed PCM audio" : "AAC \(audioBitrate) kbps"
        return "HEVC \(hevcQuality) · \(leftRightBitrate) Mbps eyes · \(resolution) · \(audio)"
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

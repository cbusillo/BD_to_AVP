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

enum SubtitleLanguage: String, CaseIterable, Codable, Identifiable {
    case english = "eng"
    case spanish = "spa"
    case french = "fre"
    case german = "ger"
    case chinese = "chi"
    case japanese = "jpn"
    case portuguese = "por"
    case russian = "rus"
    case italian = "ita"
    case korean = "kor"

    var id: String { rawValue }

    var name: String {
        switch self {
        case .english: "English"
        case .spanish: "Spanish"
        case .french: "French"
        case .german: "German"
        case .chinese: "Chinese"
        case .japanese: "Japanese"
        case .portuguese: "Portuguese"
        case .russian: "Russian"
        case .italian: "Italian"
        case .korean: "Korean"
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
    var language = SubtitleLanguage.english
    var includeSubtitles = true
    var keepExtraLanguages = true

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

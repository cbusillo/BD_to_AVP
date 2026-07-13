import Foundation

enum BuiltInProfile: String, CaseIterable, Identifiable {
    case balanced = "builtin.balanced"
    case originalResolution = "builtin.original-resolution"
    case fourKUpscale = "builtin.4k-upscale"

    var id: String { rawValue }

    var name: String {
        switch self {
        case .balanced:
            "Balanced"
        case .originalResolution:
            "Original Resolution"
        case .fourKUpscale:
            "4K Upscale"
        }
    }

    var summary: String {
        switch self {
        case .balanced:
            "HEVC 75, source resolution, original audio, and subtitles."
        case .originalResolution:
            "Higher quality while preserving the source resolution."
        case .fourKUpscale:
            "2× AI upscale with linked HEVC and upscale quality."
        }
    }

    var systemImage: String {
        switch self {
        case .balanced:
            "slider.horizontal.3"
        case .originalResolution:
            "rectangle.inset.filled"
        case .fourKUpscale:
            "sparkles.rectangle.stack"
        }
    }

    var profile: EncodingProfile {
        EncodingProfile(
            id: id,
            name: name,
            options: options,
            kind: .builtIn,
            systemImage: systemImage
        )
    }
}

struct EncodingProfile: Identifiable, Equatable {
    enum Kind: String, Codable {
        case builtIn
        case custom
    }

    let id: String
    var name: String
    var options: EncodingOptions
    let kind: Kind
    let systemImage: String

    var isBuiltIn: Bool { kind == .builtIn }
    var isCustom: Bool { kind == .custom }
    var summary: String { options.compactSummary }
}

enum OutputLength: String, CaseIterable, Identifiable {
    case fullMovie
    case oneMinute
    case threeMinutes
    case fiveMinutes

    var id: String { rawValue }

    var name: String {
        switch self {
        case .fullMovie:
            "Full Movie"
        case .oneMinute:
            "1-Minute Sample"
        case .threeMinutes:
            "3-Minute Sample"
        case .fiveMinutes:
            "5-Minute Sample"
        }
    }

    var summary: String {
        switch self {
        case .fullMovie:
            "Convert the entire source."
        case .oneMinute, .threeMinutes, .fiveMinutes:
            "Create a shorter file to review before committing to the full conversion."
        }
    }
}

enum SamplePosition: String, CaseIterable, Identifiable {
    case beginning
    case middle
    case ending

    var id: String { rawValue }

    var name: String {
        switch self {
        case .beginning:
            "Beginning"
        case .middle:
            "Middle"
        case .ending:
            "End"
        }
    }
}

struct AppCapabilities: Equatable {
    let conversionAvailable: Bool
    let automaticUpdateChecksAvailable: Bool

    static let current = AppCapabilities(
        conversionAvailable: true,
        automaticUpdateChecksAvailable: false
    )

    var conversionUnavailableReason: String {
        "Conversion requires an MKV, MTS, or M2TS source."
    }

    var automaticUpdatesUnavailableReason: String {
        "Automatic update checks aren’t available in this version."
    }
}

struct ConversionDraft: Equatable {
    let source: ConversionSource
    let sourceDetails: SourceInspection?
    let profile: EncodingProfile
    let destinationURL: URL
    let outputLength: OutputLength
    let samplePosition: SamplePosition
    let options: ConversionOptions

    var proposedOutputURL: URL {
        destinationURL.appendingPathComponent("\(outputStem)_AVP.mov")
    }

    private var outputStem: String {
        let inspectedName = sourceDetails?.name.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !inspectedName.isEmpty {
            return URL(fileURLWithPath: inspectedName).deletingPathExtension().lastPathComponent
        }
        return source.proposedOutputStem
    }
}

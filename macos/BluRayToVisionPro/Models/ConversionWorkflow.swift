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
            "HEVC 75, source resolution, uncompressed PCM audio, and subtitles."
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

    var durationSeconds: Int? {
        switch self {
        case .fullMovie:
            nil
        case .oneMinute:
            60
        case .threeMinutes:
            180
        case .fiveMinutes:
            300
        }
    }

    static var previewCases: [OutputLength] {
        allCases.filter { $0 != .fullMovie }
    }
}

enum SamplePosition: String, CaseIterable, Identifiable {
    case beginning
    case middle
    case ending = "end"

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

    static let current = AppCapabilities(
        conversionAvailable: true
    )

    var conversionUnavailableReason: String {
        "Conversion requires a Blu-ray disc, Blu-ray folder, ISO, MKV, MTS, or M2TS source."
    }

}

struct ConversionDraft: Equatable {
    let source: ConversionSource
    let sourceDetails: SourceInspection?
    let profile: EncodingProfile
    let destinationURL: URL
    let options: ConversionOptions
    let selectedTitle: SourceTitle?

    init(
        source: ConversionSource,
        sourceDetails: SourceInspection?,
        profile: EncodingProfile,
        destinationURL: URL,
        options: ConversionOptions,
        selectedTitle: SourceTitle? = nil
    ) {
        self.source = source
        self.sourceDetails = sourceDetails
        self.profile = profile
        self.destinationURL = destinationURL
        self.options = options
        self.selectedTitle = selectedTitle
    }

    var proposedOutputURL: URL {
        destinationURL.appendingPathComponent("\(outputStem)_AVP.mov")
    }

    func withSourceDetails(_ sourceDetails: SourceInspection?) -> ConversionDraft {
        ConversionDraft(
            source: source,
            sourceDetails: sourceDetails,
            profile: profile,
            destinationURL: destinationURL,
            options: options,
            selectedTitle: selectedTitle
        )
    }

    func retrying(
        decision: WorkerDecision,
        choice: WorkerRecoveryChoice
    ) -> ConversionDraft? {
        guard decision.choices.contains(choice.rawValue) else {
            return nil
        }

        var retryOptions = options
        switch (decision.identifier, choice) {
        case ("mkv_creation_decision_required", .retryContinueOnError):
            retryOptions.job.startStage = .extractMVCAndAudio
            retryOptions.job.continueOnError = true
        case ("subtitle_decision_required", .retryWithoutSubtitles):
            retryOptions.job.startStage = .extractSubtitles
            retryOptions.encoding.includeSubtitles = false
        default:
            return nil
        }

        return ConversionDraft(
            source: source,
            sourceDetails: sourceDetails,
            profile: profile,
            destinationURL: destinationURL,
            options: retryOptions,
            selectedTitle: selectedTitle
        )
    }

    private var outputStem: String {
        if let selectedTitle {
            return selectedTitle.outputName
        }
        let inspectedName = sourceDetails?.name.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !inspectedName.isEmpty {
            let inspectedURL = URL(fileURLWithPath: inspectedName)
            let sourceExtension = source.url.pathExtension
            if !sourceExtension.isEmpty,
               inspectedURL.pathExtension.caseInsensitiveCompare(sourceExtension) == .orderedSame
            {
                return inspectedURL.deletingPathExtension().lastPathComponent
            }
            return inspectedURL.lastPathComponent
        }
        return source.proposedOutputStem
    }
}

struct PreviewDraft: Equatable {
    let parentJobID: UUID
    let conversion: ConversionDraft
    let outputLength: OutputLength
    let samplePosition: SamplePosition

    init?(
        parentJobID: UUID = UUID(),
        conversion: ConversionDraft,
        outputLength: OutputLength,
        samplePosition: SamplePosition
    ) {
        guard outputLength.durationSeconds != nil else {
            return nil
        }
        self.parentJobID = parentJobID
        self.conversion = conversion
        self.outputLength = outputLength
        self.samplePosition = samplePosition
    }

    var durationSeconds: Int {
        outputLength.durationSeconds ?? 0
    }
}

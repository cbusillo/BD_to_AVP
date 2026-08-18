import Foundation

enum BuiltInProfile: String, CaseIterable, Identifiable {
    case balanced = "builtin.balanced"
    case originalResolution = "builtin.original-resolution"
    case fourKUpscale = "builtin.4k-upscale"

    var id: String { rawValue }

    var name: String {
        switch self {
        case .balanced:
            "Standard Movie"
        case .originalResolution:
            "Original Resolution"
        case .fourKUpscale:
            "4K Upscale"
        }
    }

    var summary: String {
        switch self {
        case .balanced:
            "HEVC 75, source resolution, automatic audio, English audio only, and subtitles."
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
    var pipelineDefaults: ProfilePipelineDefaults? = nil
    let kind: Kind
    let systemImage: String

    init(
        id: String,
        name: String,
        options: EncodingOptions,
        pipelineDefaults: ProfilePipelineDefaults? = nil,
        kind: Kind,
        systemImage: String
    ) {
        self.id = id
        self.name = name
        self.options = options
        self.pipelineDefaults = pipelineDefaults
        self.kind = kind
        self.systemImage = systemImage
    }

    var isBuiltIn: Bool { kind == .builtIn }
    var isCustom: Bool { kind == .custom }
    var summary: String { options.compactSummary }
}

enum ProfilePersistenceCopy {
    static let summary = "Profiles save video, audio, subtitle, reusable-file, and encoder settings. Restart, overwrite, delete-source, recovery, command-output, sound, and keep-awake choices are not saved."
}

enum StandardMovieRenameNotice {
    static let message = "The Balanced profile is now called Standard Movie. Balanced is still a video quality choice. Your saved profiles are unchanged."
}

enum ReusableFileOutcome: String, CaseIterable, Identifiable {
    case finishedMovieOnly
    case finishedMovieAndReusableFiles

    var id: String { rawValue }

    var title: String {
        switch self {
        case .finishedMovieOnly:
            "Just the finished movie"
        case .finishedMovieAndReusableFiles:
            "The finished movie plus reusable files"
        }
    }

    var policy: IntermediatePolicy {
        switch self {
        case .finishedMovieOnly:
            .automatic
        case .finishedMovieAndReusableFiles:
            .reusable
        }
    }

    init(policy: IntermediatePolicy) {
        self = policy.createsReusableArtifacts
            ? .finishedMovieAndReusableFiles
            : .finishedMovieOnly
    }

    var detail: String {
        switch self {
        case .finishedMovieOnly:
            "You get one finished movie. Temporary processing files are removed after a successful conversion."
        case .finishedMovieAndReusableFiles:
            "You get the finished movie plus reusable left- and right-eye movies that can be used later without rereading the disc. This needs more storage and time, and some quality choices may be unavailable."
        }
    }
}

struct SetupEditSession: Equatable {
    private(set) var originalProfileID: String
    private(set) var originalProfileName: String
    private(set) var originalProfileKind: EncodingProfile.Kind
    private(set) var originalOptions: ConversionOptions

    private(set) var profileID: String
    private(set) var profileName: String
    private(set) var draftOptions: ConversionOptions

    init(profile: EncodingProfile, options: ConversionOptions) {
        originalProfileID = profile.id
        originalProfileName = profile.name
        originalProfileKind = profile.kind
        originalOptions = options
        profileID = profile.id
        profileName = profile.name
        draftOptions = options
    }

    var isDirty: Bool {
        draftOptions != originalOptions || profileName != originalProfileName
    }

    var canUpdateProfile: Bool {
        originalProfileKind == .custom && profileID == originalProfileID
    }

    var profilePipelineDefaults: ProfilePipelineDefaults {
        draftOptions.job.profilePipelineDefaults
    }

    mutating func updateName(_ name: String) {
        profileName = name
    }

    mutating func updateOptions(_ options: ConversionOptions) {
        draftOptions = options
    }

    mutating func load(profile: EncodingProfile, options: ConversionOptions) {
        originalProfileID = profile.id
        originalProfileName = profile.name
        originalProfileKind = profile.kind
        originalOptions = options
        profileID = profile.id
        profileName = profile.name
        draftOptions = options
    }

    mutating func discard() {
        profileID = originalProfileID
        profileName = originalProfileName
        draftOptions = originalOptions
    }

    func profileWriteValues() -> (name: String, options: EncodingOptions, pipelineDefaults: ProfilePipelineDefaults) {
        (profileName, draftOptions.encoding, profilePipelineDefaults)
    }

    func switching(to profile: EncodingProfile, options: ConversionOptions) -> SetupEditSession {
        SetupEditSession(profile: profile, options: options)
    }
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
        destinationURL.appendingPathComponent("\(outputStem)\(options.encoding.videoOutputMode.outputFileTag).mov")
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
            retryOptions.encoding.subtitles.mode = .off
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

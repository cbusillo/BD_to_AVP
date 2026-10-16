import Foundation

/// Builds queue drafts from the app's saved defaults without depending on a window or view.
///
/// The main window and background queue automation both use it, so a file admitted from a
/// watch folder gets the same profile, destination, and job options a manual add would.
struct QueueDraftFactory: Equatable {
    let profile: EncodingProfile
    let destinationURL: URL
    let options: ConversionOptions

    init(profile: EncodingProfile, destinationURL: URL, options: ConversionOptions) {
        self.profile = profile
        self.destinationURL = destinationURL
        self.options = options
    }

    /// Defaults for a profile as a fresh window would configure them.
    init(profile: EncodingProfile, destinationURL: URL, jobOptions: JobOptions) {
        self.init(
            profile: profile,
            destinationURL: destinationURL,
            options: Self.initialOptions(profile: profile, jobOptions: jobOptions)
        )
    }

    /// Defaults from saved settings, using `profileID` when given and the saved default profile otherwise.
    @MainActor
    init(settings: AppSettings, profileStore: ProfileStore, profileID: String? = nil) {
        let profile = profileStore.profile(
            withID: profileStore.normalizedProfileID(profileID ?? settings.selectedProfileID)
        )
        self.init(
            profile: profile,
            destinationURL: settings.destinationURL,
            jobOptions: Self.jobOptions(from: settings)
        )
    }

    @MainActor
    static func jobOptions(from settings: AppSettings) -> JobOptions {
        JobOptions(
            intermediatePolicy: settings.intermediatePolicy,
            softwareEncoder: settings.useSoftwareEncoder,
            keepAwake: settings.keepAwake,
            playSound: settings.playSound
        )
    }

    static func initialOptions(profile: EncodingProfile, jobOptions: JobOptions) -> ConversionOptions {
        var job = jobOptions
        if let pipelineDefaults = profile.pipelineDefaults {
            job.applyProfilePipelineDefaults(pipelineDefaults)
        }
        return ConversionOptions(encoding: profile.options, job: job)
    }

    func drafts(for sources: [ConversionSource]) -> [ConversionDraft] {
        sources.map(draft(for:))
    }

    func draft(for source: ConversionSource) -> ConversionDraft {
        var sourceOptions = options
        if source.kind == .physicalDisc {
            sourceOptions.job.removeOriginalAfterSuccess = false
        }
        return ConversionDraft(
            source: source,
            sourceDetails: nil,
            profile: profile,
            destinationURL: destinationURL,
            options: sourceOptions
        )
    }
}

/// A draft admitted without a person adding it, with its provenance and review policy.
struct AutomatedQueueDraft: Equatable {
    let draft: ConversionDraft
    let admission: DurableQueueAdmission
    let holdForReview: Bool
}

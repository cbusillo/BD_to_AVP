import Combine
import Foundation

enum RouteQualityTrigger: String, CaseIterable, Codable, Identifiable {
    case generatedRouteRequirement = "generated_route_requirement"
    case reusableIntermediates = "reusable_intermediates"
    case softwareEncoder = "software_encoder"
    case restartStage = "restart_stage"
    case upscaleCrop = "upscale_crop"
    case fieldOfView = "field_of_view"
    case resolutionOverride = "resolution_override"
    case outputMode = "output_mode"
    case qualitySettings = "quality_settings"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .generatedRouteRequirement:
            "Selected quality"
        case .reusableIntermediates:
            "Reusable files"
        case .softwareEncoder:
            "Software encoder"
        case .restartStage:
            "Restart stage"
        case .upscaleCrop:
            "Upscale and crop"
        case .fieldOfView:
            "Field of view"
        case .resolutionOverride:
            "Output resolution"
        case .outputMode:
            "Output format"
        case .qualitySettings:
            "Quality settings"
        }
    }
}

enum RouteQualityEdit: Equatable {
    case outputMode(VideoOutputMode)
    case restartStage(ConversionStage)
    case reusableIntermediates(Bool)
    case softwareEncoder(Bool)
    case upscaleEnabled(Bool)
    case cropBlackBars(Bool)
    case fieldOfView(Int)
    case resolutionOverride(String)
    case qualityStep(QualityStep)
    case customQuality(VideoQualityCustomValues)
    case linkGeneratedAndUpscaleQuality(Bool)

    var trigger: RouteQualityTrigger {
        switch self {
        case .outputMode:
            .outputMode
        case .restartStage:
            .restartStage
        case .reusableIntermediates:
            .reusableIntermediates
        case .softwareEncoder:
            .softwareEncoder
        case .upscaleEnabled, .cropBlackBars:
            .upscaleCrop
        case .fieldOfView:
            .fieldOfView
        case .resolutionOverride:
            .resolutionOverride
        case .qualityStep:
            .generatedRouteRequirement
        case .customQuality:
            .qualitySettings
        case .linkGeneratedAndUpscaleQuality:
            .qualitySettings
        }
    }
}

enum RouteQualityResolutionChoice: String, CaseIterable, Codable, Identifiable {
    case preservePriorIntent = "preserve_prior_intent"
    case keepRequestedWorkflow = "keep_requested_workflow"
    case useCustomExactSettings = "custom_exact_settings"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .preservePriorIntent:
            "Preserve prior intent"
        case .keepRequestedWorkflow:
            "Keep requested workflow"
        case .useCustomExactSettings:
            "Use Custom exact settings"
        }
    }
}

struct RouteQualityResolutionOption: Codable, Equatable, Identifiable {
    let choice: RouteQualityResolutionChoice
    let mappingVersion: Int
    let mappedStep: QualityStep?
    let isAvailable: Bool

    var id: String {
        "\(choice.rawValue):v\(mappingVersion)"
    }

    var title: String { choice.title }

    var detail: String {
        switch choice {
        case .preservePriorIntent:
            return "Leave the current video and quality choices unchanged."
        case .keepRequestedWorkflow:
            if let mappedStep {
                return "Use the requested file outcome with \(mappedStep.title) quality."
            }
            return "No guided quality choice is available for the requested file outcome."
        case .useCustomExactSettings:
            return "Use the requested file outcome with the exact values shown in Advanced Settings."
        }
    }
}

struct RouteQualityConflict: Equatable {
    let trigger: RouteQualityTrigger
    let reason: String
    let priorOptions: ConversionOptions
    let proposedOptions: ConversionOptions
    let priorRoute: VideoRoutePlan
    let proposedRoute: VideoRoutePlan
    let resolutions: [RouteQualityResolutionOption]
    let mappingVersion: Int
    var selectedResolutionID: String?

    /// A durable identity for a held decision. It deliberately excludes display copy.
    var stableID: String {
        [
            trigger.rawValue,
            priorRoute.kind.rawValue,
            proposedRoute.kind.rawValue,
            proposedRoute.includesUpscale ? "upscale" : "no-upscale",
            proposedRoute.generatedRequirement?.identifier ?? "none",
            "mapping-v\(mappingVersion)",
            resolutions.filter(\.isAvailable).map(\.id).sorted().joined(separator: ","),
        ].joined(separator: "|")
    }

    var blockReason: String {
        "Resolve \(trigger.title.lowercased()) before starting, previewing, or adding this conversion to the queue."
    }
}

struct RouteQualityDraft: Equatable {
    let options: ConversionOptions
    let mappingVersion: Int

    var route: VideoRoutePlan {
        VideoRoutePlan(options: options)
    }
}

enum RouteQualityProposal: Equatable {
    case resolved(RouteQualityDraft)
    case conflict(RouteQualityConflict)
    case invalid(String)
}

struct RouteQualityResolutionError: Error, Equatable, LocalizedError {
    let message: String

    var errorDescription: String? { message }
}

enum RouteQualityEngine {
    static let mappingVersion = VideoQualityCatalog.mappingVersion

    static func propose(
        options: ConversionOptions,
        edit: RouteQualityEdit
    ) -> RouteQualityProposal {
        var proposed = options
        do {
            try apply(edit, to: &proposed)
        } catch {
            return .invalid(error.localizedDescription)
        }

        let priorRoute = VideoRoutePlan(options: options)
        let proposedRoute = VideoRoutePlan(options: proposed)
        let routeChanged = priorRoute.kind != proposedRoute.kind
            || priorRoute.includesUpscale != proposedRoute.includesUpscale
            || priorRoute.generatedRequirement != proposedRoute.generatedRequirement
        let qualityDecisionRequired = routeChanged
            && options.encoding.videoQuality.mode == .ladder
        if !qualityDecisionRequired, isValid(proposed, route: proposedRoute) {
            return .resolved(RouteQualityDraft(options: proposed, mappingVersion: mappingVersion))
        }

        let resolutions = resolutionOptions(
            prior: options,
            proposed: proposed,
            proposedRoute: proposedRoute
        )
        let reason = conflictReason(
            for: proposed,
            priorRoute: priorRoute,
            proposedRoute: proposedRoute
        )
        return .conflict(
            RouteQualityConflict(
                trigger: trigger(for: edit, priorRoute: priorRoute, proposedRoute: proposedRoute),
                reason: reason,
                priorOptions: options,
                proposedOptions: proposed,
                priorRoute: priorRoute,
                proposedRoute: proposedRoute,
                resolutions: resolutions,
                mappingVersion: mappingVersion,
                selectedResolutionID: nil
            )
        )
    }

    static func resolve(
        _ option: RouteQualityResolutionOption,
        conflict: RouteQualityConflict
    ) -> Result<RouteQualityDraft, RouteQualityResolutionError> {
        guard option.isAvailable,
              conflict.resolutions.contains(option)
        else {
            return .failure(.init(message: "That video and quality choice is not available."))
        }

        var resolved: ConversionOptions
        switch option.choice {
        case .preservePriorIntent:
            resolved = conflict.priorOptions
        case .keepRequestedWorkflow:
            guard let mappedStep = option.mappedStep else {
                return .failure(.init(message: "No guided quality choice is available for the requested file outcome."))
            }
            resolved = conflict.proposedOptions
            do {
                try applyMappedQuality(mappedStep, to: &resolved)
            } catch {
                return .failure(.init(message: error.localizedDescription))
            }
        case .useCustomExactSettings:
            resolved = conflict.proposedOptions
            resolved.encoding.videoQuality = .custom(
                lastLadderStep: conflict.priorOptions.encoding.videoQuality.lastLadderStep,
                values: concreteQualityValues(from: conflict.priorOptions.encoding)
            )
            do {
                try resolved.encoding.applyVideoQualityIntent()
            } catch {
                return .failure(.init(message: error.localizedDescription))
            }
        }

        let route = VideoRoutePlan(options: resolved)
        guard isValid(resolved, route: route) else {
            return .failure(.init(message: validationReason(for: resolved, route: route)))
        }
        return .success(RouteQualityDraft(options: resolved, mappingVersion: mappingVersion))
    }

    static func validate(_ options: ConversionOptions) -> Result<RouteQualityDraft, RouteQualityResolutionError> {
        let route = VideoRoutePlan(options: options)
        guard isValid(options, route: route) else {
            return .failure(.init(message: validationReason(for: options, route: route)))
        }
        return .success(RouteQualityDraft(options: options, mappingVersion: mappingVersion))
    }

    private static func apply(
        _ edit: RouteQualityEdit,
        to options: inout ConversionOptions
    ) throws {
        switch edit {
        case let .outputMode(mode):
            options.encoding.selectVideoOutputMode(mode)
        case let .restartStage(stage):
            options.job.startStage = stage
        case let .reusableIntermediates(enabled):
            options.job.intermediatePolicy = enabled ? .reusable : .automatic
        case let .softwareEncoder(enabled):
            options.job.softwareEncoder = enabled
        case let .upscaleEnabled(enabled):
            options.encoding.upscaleEnabled = enabled
        case let .cropBlackBars(enabled):
            options.encoding.cropBlackBars = enabled
        case let .fieldOfView(value):
            options.encoding.fieldOfView = value
        case let .resolutionOverride(value):
            options.encoding.resolutionOverride = value
        case let .qualityStep(step):
            try options.encoding.selectQualityStep(step)
        case let .customQuality(values):
            options.encoding.videoQuality = .custom(
                lastLadderStep: options.encoding.videoQuality.lastLadderStep,
                values: values
            )
            try options.encoding.applyVideoQualityIntent()
        case let .linkGeneratedAndUpscaleQuality(linked):
            var values = options.encoding.videoQuality.custom
            if linked {
                values.upscaleQuality = values.generatedMergeQuality
            }
            options.encoding.videoQuality = .custom(
                lastLadderStep: options.encoding.videoQuality.lastLadderStep,
                values: values
            )
            options.encoding.mvHEVC.linkGeneratedAndUpscaleQuality = linked
            try options.encoding.applyVideoQualityIntent()
        }
    }

    private static func applyMappedQuality(
        _ step: QualityStep,
        to options: inout ConversionOptions
    ) throws {
        guard options.encoding.videoOutputMode == .mvHEVC else {
            throw VideoQualityStateError.incompatibleOutputMode
        }
        options.encoding.videoQuality.mode = .ladder
        options.encoding.videoQuality.lastLadderStep = step
        try options.encoding.applyVideoQualityIntent()
    }

    private static func isValid(
        _ options: ConversionOptions,
        route: VideoRoutePlan
    ) -> Bool {
        guard (try? options.encoding.validateQualityMirrors()) != nil else {
            return false
        }
        guard options.encoding.videoQuality.mode == .ladder else {
            return true
        }
        if route.kind == .existingArtifact, !route.includesUpscale {
            return true
        }
        guard let target = qualityTarget(for: route) else {
            return false
        }
        guard VideoQualityCatalog.supports(
            options.encoding.videoQuality.lastLadderStep,
            for: target
        ) else {
            return false
        }
        if route.kind == .generatedMVHEVC, route.includesUpscale {
            return VideoQualityCatalog.supports(
                options.encoding.videoQuality.lastLadderStep,
                for: .fileUpscale
            )
        }
        return true
    }

    private static func validationReason(
        for options: ConversionOptions,
        route: VideoRoutePlan
    ) -> String {
        do {
            try options.encoding.validateQualityMirrors()
        } catch {
            return "The current quality choice no longer matches its detailed values. Review it before continuing."
        }
        if options.encoding.videoQuality.mode == .ladder,
           route.kind != .existingArtifact || route.includesUpscale,
           let target = qualityTarget(for: route),
           !VideoQualityCatalog.supports(options.encoding.videoQuality.lastLadderStep, for: target)
        {
            return "\(options.encoding.videoQuality.lastLadderStep.title) quality is not available with the requested file outcome."
        }
        return "The selected video and quality choices need review before continuing."
    }

    private static func conflictReason(
        for options: ConversionOptions,
        priorRoute: VideoRoutePlan,
        proposedRoute: VideoRoutePlan
    ) -> String {
        guard isValid(options, route: proposedRoute) else {
            return validationReason(for: options, route: proposedRoute)
        }
        switch proposedRoute.kind {
        case .generatedMVHEVC:
            return "This change requires generated left- and right-eye files. Choose how the current quality choice should carry over."
        case .directMVHEVC:
            return "This change uses the source video directly. Choose how the current quality choice should carry over."
        case .av1Stereo:
            return "This output format uses exact Custom quality settings. Choose how to continue."
        case .existingArtifact:
            return priorRoute.kind == .existingArtifact
                ? "This restart changes which retained quality settings are applied. Choose how to continue."
                : "This restart reuses an existing video. Choose how retained quality settings should be handled."
        }
    }

    private static func resolutionOptions(
        prior: ConversionOptions,
        proposed: ConversionOptions,
        proposedRoute: VideoRoutePlan
    ) -> [RouteQualityResolutionOption] {
        let mappedStep = mappedStep(
            from: prior.encoding.videoQuality,
            for: proposedRoute
        )
        return RouteQualityResolutionChoice.allCases.map { choice in
            RouteQualityResolutionOption(
                choice: choice,
                mappingVersion: mappingVersion,
                mappedStep: choice == .keepRequestedWorkflow ? mappedStep : nil,
                isAvailable: choice != .keepRequestedWorkflow || mappedStep != nil
            )
        }
    }

    private static func mappedStep(
        from intent: VideoQualityIntent,
        for route: VideoRoutePlan
    ) -> QualityStep? {
        guard intent.mode == .ladder else {
            return nil
        }
        let supported: [QualityStep]
        switch route.kind {
        case .directMVHEVC:
            supported = VideoQualityCatalog.supportedSteps(
                for: route.includesUpscale ? .directMVHEVCMetalFX2x : .directMVHEVC
            )
        case .generatedMVHEVC:
            supported = VideoQualityCatalog.supportedGeneratedSteps(
                includingFileUpscale: route.includesUpscale
            )
        case .existingArtifact:
            supported = route.includesUpscale
                ? VideoQualityCatalog.supportedSteps(for: .fileUpscale)
                : QualityStep.allCases
        case .av1Stereo:
            return nil
        }
        return supported.min {
            abs($0.ordinal - intent.lastLadderStep.ordinal)
                < abs($1.ordinal - intent.lastLadderStep.ordinal)
        }
    }

    private static func qualityTarget(for route: VideoRoutePlan) -> VideoQualityTarget? {
        switch route.kind {
        case .directMVHEVC:
            route.includesUpscale ? .directMVHEVCMetalFX2x : .directMVHEVC
        case .generatedMVHEVC:
            .generatedMVHEVC
        case .existingArtifact:
            route.includesUpscale ? .fileUpscale : nil
        case .av1Stereo:
            .av1Stereo
        }
    }

    private static func concreteQualityValues(
        from encoding: EncodingOptions
    ) -> VideoQualityCustomValues {
        VideoQualityCustomValues(
            directFinalBitrate: encoding.mvHEVC.directFinalBitrate,
            generatedEyeBitrate: encoding.mvHEVC.generatedEyeBitrate,
            generatedMergeQuality: encoding.mvHEVC.generatedMergeQuality,
            av1CRF: encoding.av1CRF,
            upscaleQuality: encoding.upscaleQuality
        )
    }

    private static func trigger(
        for edit: RouteQualityEdit,
        priorRoute: VideoRoutePlan,
        proposedRoute: VideoRoutePlan
    ) -> RouteQualityTrigger {
        _ = priorRoute
        _ = proposedRoute
        return edit.trigger
    }
}

final class RouteQualityResolutionState: ObservableObject {
    @Published private(set) var conflict: RouteQualityConflict?
    @Published private(set) var invalidMessage: String?

    var blockReason: String? {
        conflict?.blockReason ?? invalidMessage
    }

    func apply(_ edit: RouteQualityEdit, to options: inout ConversionOptions) {
        guard conflict == nil else {
            return
        }
        switch RouteQualityEngine.propose(options: options, edit: edit) {
        case let .resolved(draft):
            options = draft.options
            invalidMessage = nil
        case let .conflict(conflict):
            self.conflict = conflict
            invalidMessage = nil
        case let .invalid(message):
            invalidMessage = message
        }
    }

    func resolve(_ option: RouteQualityResolutionOption, in options: inout ConversionOptions) {
        guard let conflict else {
            return
        }
        guard options == conflict.priorOptions else {
            reset()
            invalidMessage = "Conversion settings changed while this choice was waiting. Review the current settings and try the change again."
            return
        }
        var selectedConflict = conflict
        selectedConflict.selectedResolutionID = option.id
        switch RouteQualityEngine.resolve(option, conflict: selectedConflict) {
        case let .success(draft):
            options = draft.options
            self.conflict = nil
            invalidMessage = nil
        case let .failure(message):
            invalidMessage = message.localizedDescription
        }
    }

    func validate(_ options: ConversionOptions) {
        guard conflict == nil else {
            return
        }
        if case let .failure(message) = RouteQualityEngine.validate(options) {
            invalidMessage = message.localizedDescription
        } else {
            invalidMessage = nil
        }
    }

    func reset() {
        conflict = nil
        invalidMessage = nil
    }
}

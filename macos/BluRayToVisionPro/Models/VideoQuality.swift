import Foundation

enum QualityStep: String, CaseIterable, Codable, Identifiable {
    case spaceSaver = "space_saver"
    case compact
    case efficient
    case balanced
    case detailed
    case highDetail = "high_detail"
    case maximumDetail = "maximum_detail"

    var id: String { rawValue }

    var ordinal: Int {
        switch self {
        case .spaceSaver:
            1
        case .compact:
            2
        case .efficient:
            3
        case .balanced:
            4
        case .detailed:
            5
        case .highDetail:
            6
        case .maximumDetail:
            7
        }
    }

    var title: String {
        switch self {
        case .spaceSaver:
            "Space Saver"
        case .compact:
            "Compact"
        case .efficient:
            "Efficient"
        case .balanced:
            "Balanced"
        case .detailed:
            "Detailed"
        case .highDetail:
            "High Detail"
        case .maximumDetail:
            "Maximum Detail"
        }
    }

    var detail: String {
        switch self {
        case .spaceSaver:
            "Prioritizes smaller files over fine detail."
        case .compact:
            "Reduces storage while preserving everyday clarity."
        case .efficient:
            "Leans toward smaller files with moderate detail."
        case .balanced:
            "Recommended balance of detail, storage, and compatibility."
        case .detailed:
            "Preserves more texture at a higher storage cost."
        case .highDetail:
            "Favors fine detail and larger output files."
        case .maximumDetail:
            "Prioritizes maximum retained detail and the largest files."
        }
    }
}

enum QualityIntentMode: String, Codable {
    case ladder
    case custom
}

enum VideoQualitySelection: Equatable, Identifiable {
    case step(QualityStep)
    case custom

    var id: String {
        switch self {
        case let .step(step):
            step.rawValue
        case .custom:
            "custom"
        }
    }

    var title: String {
        switch self {
        case let .step(step):
            step.title
        case .custom:
            "Custom"
        }
    }
}

enum VideoQualityTarget: String, CaseIterable, Codable {
    case directMVHEVC = "direct_mv_hevc"
    case directMVHEVCMetalFX2x = "direct_mv_hevc_metalfx_2x"
    case generatedMVHEVC = "generated_mv_hevc"
    case fileUpscale = "upscale_quality"
    case av1Stereo = "av1_sbs"
}

enum VideoQualityRouteValues: Equatable {
    case direct(quality: Double)
    case generated(eyeBitrateMbps: Int, mergeQuality: Int)
    case upscale(quality: Int)
}

enum VideoQualityCatalog {
    static let mappingVersion = 2
    static let defaultDirectCustomBitrateMbps = 40
    static let balancedGeneratedEyeBitrateMbps = 20
    static let balancedGeneratedMergeQuality = 75
    static let balancedUpscaleQuality = 75
    static let defaultAV1CRF = 32

    static func mapping(
        for step: QualityStep,
        target: VideoQualityTarget
    ) -> VideoQualityRouteValues? {
        return switch target {
        case .directMVHEVC:
            .direct(quality: directQuality(for: step))
        case .directMVHEVCMetalFX2x:
            .direct(quality: directMetalFXQuality(for: step))
        case .generatedMVHEVC:
            step == .balanced
                ? .generated(
                    eyeBitrateMbps: balancedGeneratedEyeBitrateMbps,
                    mergeQuality: balancedGeneratedMergeQuality
                )
                : nil
        case .fileUpscale:
            switch step {
            case .balanced:
                .upscale(quality: balancedUpscaleQuality)
            case .detailed:
                .upscale(quality: 100)
            default:
                nil
            }
        case .av1Stereo:
            nil
        }
    }

    static func supports(_ step: QualityStep, for target: VideoQualityTarget) -> Bool {
        mapping(for: step, target: target) != nil
    }

    static func supportsProfileMVHEVCIntent(_ step: QualityStep) -> Bool {
        supports(step, for: .directMVHEVC)
            && supports(step, for: .directMVHEVCMetalFX2x)
    }

    static func supportedSteps(for target: VideoQualityTarget) -> [QualityStep] {
        QualityStep.allCases.filter { supports($0, for: target) }
    }

    static func supportedGeneratedSteps(includingFileUpscale: Bool) -> [QualityStep] {
        QualityStep.allCases.filter { step in
            supports(step, for: .generatedMVHEVC)
                && (!includingFileUpscale || supports(step, for: .fileUpscale))
        }
    }

    static var selectableMVHEVCSteps: [QualityStep] {
        QualityStep.allCases.filter(supportsProfileMVHEVCIntent)
    }

    private static func directQuality(for step: QualityStep) -> Double {
        switch step {
        case .spaceSaver:
            0.4
        case .compact:
            0.5
        case .efficient:
            0.6
        case .balanced:
            0.7
        case .detailed:
            0.75
        case .highDetail:
            0.8
        case .maximumDetail:
            0.85
        }
    }

    private static func directMetalFXQuality(for step: QualityStep) -> Double {
        switch step {
        case .spaceSaver:
            0.3
        case .compact:
            0.4
        case .efficient:
            0.5
        case .balanced:
            0.6
        case .detailed:
            0.65
        case .highDetail:
            0.7
        case .maximumDetail:
            0.75
        }
    }
}

struct VideoQualityCustomValues: Codable, Equatable {
    var directFinalBitrate: BitratePreference
    var generatedEyeBitrate: BitratePreference
    var generatedMergeQuality: Int
    var av1CRF: Int
    var upscaleQuality: Int

    static var defaults: VideoQualityCustomValues {
        VideoQualityCustomValues(
            directFinalBitrate: BitratePreference(
                mode: .automatic,
                customMbps: VideoQualityCatalog.defaultDirectCustomBitrateMbps
            ),
            generatedEyeBitrate: BitratePreference(
                mode: .automatic,
                customMbps: VideoQualityCatalog.balancedGeneratedEyeBitrateMbps
            ),
            generatedMergeQuality: VideoQualityCatalog.balancedGeneratedMergeQuality,
            av1CRF: VideoQualityCatalog.defaultAV1CRF,
            upscaleQuality: VideoQualityCatalog.balancedUpscaleQuality
        )
    }

    var isValid: Bool {
        Self.isValid(directFinalBitrate)
            && Self.isValid(generatedEyeBitrate)
            && (0 ... 100).contains(generatedMergeQuality)
            && (0 ... 63).contains(av1CRF)
            && (0 ... 100).contains(upscaleQuality)
    }

    private static func isValid(_ preference: BitratePreference) -> Bool {
        switch preference.mode {
        case .automatic:
            preference.customMbps.map { (1 ... 500).contains($0) } ?? true
        case .custom:
            preference.customMbps.map { (1 ... 500).contains($0) } ?? false
        }
    }
}

struct VideoQualityIntent: Codable, Equatable {
    var mode: QualityIntentMode
    var lastLadderStep: QualityStep
    var custom: VideoQualityCustomValues

    static var balanced: VideoQualityIntent {
        VideoQualityIntent(
            mode: .ladder,
            lastLadderStep: .balanced,
            custom: .defaults
        )
    }

    static func custom(
        lastLadderStep: QualityStep = .balanced,
        values: VideoQualityCustomValues
    ) -> VideoQualityIntent {
        VideoQualityIntent(
            mode: .custom,
            lastLadderStep: lastLadderStep,
            custom: values
        )
    }

    static func custom(
        lastLadderStep: QualityStep = .balanced,
        mvHEVC: MVHEVCOptions,
        av1CRF: Int,
        upscaleQuality: Int
    ) -> VideoQualityIntent {
        custom(
            lastLadderStep: lastLadderStep,
            values: VideoQualityCustomValues(
                directFinalBitrate: mvHEVC.directFinalBitrate,
                generatedEyeBitrate: mvHEVC.generatedEyeBitrate,
                generatedMergeQuality: mvHEVC.generatedMergeQuality,
                av1CRF: av1CRF,
                upscaleQuality: upscaleQuality
            )
        )
    }

    var selectedStep: QualityStep? {
        mode == .ladder ? lastLadderStep : nil
    }

    var selection: VideoQualitySelection {
        switch mode {
        case .ladder:
            .step(lastLadderStep)
        case .custom:
            .custom
        }
    }

    var displayTitle: String {
        selection.title
    }
}

enum VideoQualityStateError: LocalizedError, Equatable {
    case incompatibleOutputMode
    case invalidCustomValues
    case inconsistentMirrors
    case unsupportedStep(QualityStep)

    var errorDescription: String? {
        switch self {
        case .incompatibleOutputMode:
            "Guided video quality is unavailable for this output mode."
        case .invalidCustomValues:
            "Custom video quality values are invalid."
        case .inconsistentMirrors:
            "Video quality intent does not match its concrete compatibility values."
        case let .unsupportedStep(step):
            "The \(step.title) quality step is not available for the active video route."
        }
    }
}

extension CodingUserInfoKey {
    static let requiresVideoQualityIntent = CodingUserInfoKey(
        rawValue: "com.shinycomputers.bd-to-avp.requires-video-quality-intent"
    )!
}

extension EncodingOptions {
    mutating func selectVideoOutputMode(_ mode: VideoOutputMode) {
        if mode == .av1Stereo {
            selectCustomQuality()
        }
        videoOutputMode = mode
    }

    mutating func selectQualityStep(_ step: QualityStep) throws {
        guard VideoQualityCatalog.supportsProfileMVHEVCIntent(step) else {
            throw VideoQualityStateError.unsupportedStep(step)
        }
        guard videoOutputMode == .mvHEVC else {
            throw VideoQualityStateError.incompatibleOutputMode
        }
        if videoQuality.mode == .custom {
            videoQuality.custom = currentCustomVideoQuality
        }
        videoQuality.mode = .ladder
        videoQuality.lastLadderStep = step
        try applyLadderQualityMirrors()
    }

    mutating func selectCustomQuality() {
        videoQuality.mode = .custom
        applyCustomQualityMirrors()
    }

    mutating func editCustomQuality(
        _ edit: (inout VideoQualityCustomValues) -> Void
    ) {
        if videoQuality.mode != .custom {
            selectCustomQuality()
        }
        edit(&videoQuality.custom)
        applyCustomQualityMirrors()
    }

    mutating func applyVideoQualityIntent() throws {
        guard videoQuality.custom.isValid else {
            throw VideoQualityStateError.invalidCustomValues
        }
        switch videoQuality.mode {
        case .ladder:
            try applyLadderQualityMirrors()
        case .custom:
            applyCustomQualityMirrors()
        }
    }

    func normalizedQualityState() throws -> EncodingOptions {
        guard videoQuality.custom.isValid else {
            throw VideoQualityStateError.invalidCustomValues
        }
        if videoQuality.mode == .ladder,
           !VideoQualityCatalog.supportsProfileMVHEVCIntent(videoQuality.lastLadderStep)
        {
            throw VideoQualityStateError.unsupportedStep(videoQuality.lastLadderStep)
        }
        guard !qualityMirrorsMatchIntent else {
            return self
        }
        let current = currentCustomVideoQuality
        guard current.isValid else {
            throw VideoQualityStateError.invalidCustomValues
        }
        var normalized = self
        normalized.videoQuality = .custom(
            lastLadderStep: videoQuality.lastLadderStep,
            values: current
        )
        return normalized
    }

    func validateQualityMirrors() throws {
        guard videoQuality.custom.isValid else {
            throw VideoQualityStateError.invalidCustomValues
        }
        if videoQuality.mode == .ladder,
           !VideoQualityCatalog.supportsProfileMVHEVCIntent(videoQuality.lastLadderStep)
        {
            throw VideoQualityStateError.unsupportedStep(videoQuality.lastLadderStep)
        }
        guard qualityMirrorsMatchIntent else {
            throw VideoQualityStateError.inconsistentMirrors
        }
    }

    static func migratedQualityIntent(
        videoOutputMode: VideoOutputMode,
        av1CRF: Int,
        mvHEVC: MVHEVCOptions,
        upscaleQuality: Int
    ) -> VideoQualityIntent {
        let current = VideoQualityCustomValues(
            directFinalBitrate: mvHEVC.directFinalBitrate,
            generatedEyeBitrate: mvHEVC.generatedEyeBitrate,
            generatedMergeQuality: mvHEVC.generatedMergeQuality,
            av1CRF: av1CRF,
            upscaleQuality: upscaleQuality
        )
        guard videoOutputMode == .mvHEVC,
              mvHEVC.directFinalBitrate.mode == .automatic,
              mvHEVC.generatedEyeBitrate.mode == .automatic,
              mvHEVC.generatedMergeQuality == VideoQualityCatalog.balancedGeneratedMergeQuality,
              upscaleQuality == VideoQualityCatalog.balancedUpscaleQuality
        else {
            return .custom(values: current)
        }

        let retained = VideoQualityCustomValues(
            directFinalBitrate: BitratePreference(
                mode: .automatic,
                customMbps: mvHEVC.directFinalBitrate.customMbps
                    ?? VideoQualityCatalog.defaultDirectCustomBitrateMbps
            ),
            generatedEyeBitrate: BitratePreference(
                mode: .automatic,
                customMbps: mvHEVC.generatedEyeBitrate.customMbps
                    ?? VideoQualityCatalog.balancedGeneratedEyeBitrateMbps
            ),
            generatedMergeQuality: mvHEVC.generatedMergeQuality,
            av1CRF: av1CRF,
            upscaleQuality: upscaleQuality
        )
        return VideoQualityIntent(
            mode: .ladder,
            lastLadderStep: .balanced,
            custom: retained
        )
    }

    mutating func applyDecodedLegacyQualityIntent() throws {
        videoQuality = Self.migratedQualityIntent(
            videoOutputMode: videoOutputMode,
            av1CRF: av1CRF,
            mvHEVC: mvHEVC,
            upscaleQuality: upscaleQuality
        )
        if videoQuality.mode == .ladder {
            try applyLadderQualityMirrors()
        }
    }

    private var currentCustomVideoQuality: VideoQualityCustomValues {
        VideoQualityCustomValues(
            directFinalBitrate: mvHEVC.directFinalBitrate,
            generatedEyeBitrate: mvHEVC.generatedEyeBitrate,
            generatedMergeQuality: mvHEVC.generatedMergeQuality,
            av1CRF: av1CRF,
            upscaleQuality: upscaleQuality
        )
    }

    private var qualityMirrorsMatchIntent: Bool {
        switch videoQuality.mode {
        case .custom:
            return currentCustomVideoQuality == videoQuality.custom
        case .ladder:
            guard videoOutputMode == .mvHEVC else {
                return false
            }
            let generatedPreference: BitratePreference
            let generatedMergeQuality: Int
            if case let .generated(_, mergeQuality) = VideoQualityCatalog.mapping(
                for: videoQuality.lastLadderStep,
                target: .generatedMVHEVC
            ) {
                generatedPreference = BitratePreference(
                    mode: .automatic,
                    customMbps: videoQuality.custom.generatedEyeBitrate.customMbps
                )
                generatedMergeQuality = mergeQuality
            } else {
                generatedPreference = videoQuality.custom.generatedEyeBitrate
                generatedMergeQuality = videoQuality.custom.generatedMergeQuality
            }
            let resolvedUpscaleQuality: Int
            if case let .upscale(quality) = VideoQualityCatalog.mapping(
                for: videoQuality.lastLadderStep,
                target: .fileUpscale
            ) {
                resolvedUpscaleQuality = quality
            } else {
                resolvedUpscaleQuality = videoQuality.custom.upscaleQuality
            }
            return mvHEVC.directFinalBitrate.mode == .automatic
                && mvHEVC.directFinalBitrate.customMbps == videoQuality.custom.directFinalBitrate.customMbps
                && mvHEVC.generatedEyeBitrate == generatedPreference
                && mvHEVC.generatedMergeQuality == generatedMergeQuality
                && av1CRF == videoQuality.custom.av1CRF
                && upscaleQuality == resolvedUpscaleQuality
        }
    }

    private mutating func applyCustomQualityMirrors() {
        mvHEVC.directFinalBitrate = videoQuality.custom.directFinalBitrate
        mvHEVC.generatedEyeBitrate = videoQuality.custom.generatedEyeBitrate
        mvHEVC.generatedMergeQuality = videoQuality.custom.generatedMergeQuality
        av1CRF = videoQuality.custom.av1CRF
        upscaleQuality = videoQuality.custom.upscaleQuality
    }

    private mutating func applyLadderQualityMirrors() throws {
        guard videoOutputMode == .mvHEVC else {
            throw VideoQualityStateError.incompatibleOutputMode
        }
        guard VideoQualityCatalog.supportsProfileMVHEVCIntent(videoQuality.lastLadderStep) else {
            throw VideoQualityStateError.unsupportedStep(videoQuality.lastLadderStep)
        }
        mvHEVC.directFinalBitrate = BitratePreference(
            mode: .automatic,
            customMbps: videoQuality.custom.directFinalBitrate.customMbps
        )
        if case let .generated(_, mergeQuality) = VideoQualityCatalog.mapping(
            for: videoQuality.lastLadderStep,
            target: .generatedMVHEVC
        ) {
            mvHEVC.generatedEyeBitrate = BitratePreference(
                mode: .automatic,
                customMbps: videoQuality.custom.generatedEyeBitrate.customMbps
            )
            mvHEVC.generatedMergeQuality = mergeQuality
        } else {
            mvHEVC.generatedEyeBitrate = videoQuality.custom.generatedEyeBitrate
            mvHEVC.generatedMergeQuality = videoQuality.custom.generatedMergeQuality
        }
        av1CRF = videoQuality.custom.av1CRF
        if case let .upscale(quality) = VideoQualityCatalog.mapping(
            for: videoQuality.lastLadderStep,
            target: .fileUpscale
        ) {
            upscaleQuality = quality
        } else {
            upscaleQuality = videoQuality.custom.upscaleQuality
        }
    }
}

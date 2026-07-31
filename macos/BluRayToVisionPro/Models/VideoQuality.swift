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
}

enum QualityIntentMode: String, Codable {
    case ladder
    case custom
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
    static let mappingVersion = 1
    static let defaultDirectCustomBitrateMbps = 40
    static let balancedGeneratedEyeBitrateMbps = 20
    static let balancedGeneratedMergeQuality = 75
    static let balancedUpscaleQuality = 75
    static let defaultAV1CRF = 32

    static func mapping(
        for step: QualityStep,
        target: VideoQualityTarget
    ) -> VideoQualityRouteValues? {
        guard step == .balanced else {
            return nil
        }
        return switch target {
        case .directMVHEVC:
            .direct(quality: 0.7)
        case .directMVHEVCMetalFX2x:
            .direct(quality: 0.6)
        case .generatedMVHEVC:
            .generated(
                eyeBitrateMbps: balancedGeneratedEyeBitrateMbps,
                mergeQuality: balancedGeneratedMergeQuality
            )
        case .fileUpscale:
            .upscale(quality: balancedUpscaleQuality)
        case .av1Stereo:
            nil
        }
    }

    static func supports(_ step: QualityStep, for target: VideoQualityTarget) -> Bool {
        mapping(for: step, target: target) != nil
    }

    static func supportsCompleteMVHEVCIntent(_ step: QualityStep) -> Bool {
        supports(step, for: .directMVHEVC)
            && supports(step, for: .directMVHEVCMetalFX2x)
            && supports(step, for: .generatedMVHEVC)
            && supports(step, for: .fileUpscale)
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
            "The \(step.title) quality step is not available for every required MV-HEVC route."
        }
    }
}

extension CodingUserInfoKey {
    static let requiresVideoQualityIntent = CodingUserInfoKey(
        rawValue: "com.shinycomputers.bd-to-avp.requires-video-quality-intent"
    )!
}

extension EncodingOptions {
    mutating func selectQualityStep(_ step: QualityStep) throws {
        guard VideoQualityCatalog.supportsCompleteMVHEVCIntent(step) else {
            throw VideoQualityStateError.unsupportedStep(step)
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
           !VideoQualityCatalog.supportsCompleteMVHEVCIntent(videoQuality.lastLadderStep)
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
           !VideoQualityCatalog.supportsCompleteMVHEVCIntent(videoQuality.lastLadderStep)
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
            guard videoOutputMode == .mvHEVC,
                  case let .generated(_, mergeQuality) = VideoQualityCatalog.mapping(
                    for: videoQuality.lastLadderStep,
                    target: .generatedMVHEVC
                  ),
                  case let .upscale(quality) = VideoQualityCatalog.mapping(
                    for: videoQuality.lastLadderStep,
                    target: .fileUpscale
                  )
            else {
                return false
            }
            return mvHEVC.directFinalBitrate.mode == .automatic
                && mvHEVC.directFinalBitrate.customMbps == videoQuality.custom.directFinalBitrate.customMbps
                && mvHEVC.generatedEyeBitrate.mode == .automatic
                && mvHEVC.generatedEyeBitrate.customMbps == videoQuality.custom.generatedEyeBitrate.customMbps
                && mvHEVC.generatedMergeQuality == mergeQuality
                && av1CRF == videoQuality.custom.av1CRF
                && upscaleQuality == quality
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
        guard case let .generated(_, mergeQuality) = VideoQualityCatalog.mapping(
            for: videoQuality.lastLadderStep,
            target: .generatedMVHEVC
        ),
        case let .upscale(quality) = VideoQualityCatalog.mapping(
            for: videoQuality.lastLadderStep,
            target: .fileUpscale
        ) else {
            throw VideoQualityStateError.unsupportedStep(videoQuality.lastLadderStep)
        }
        mvHEVC.directFinalBitrate = BitratePreference(
            mode: .automatic,
            customMbps: videoQuality.custom.directFinalBitrate.customMbps
        )
        mvHEVC.generatedEyeBitrate = BitratePreference(
            mode: .automatic,
            customMbps: videoQuality.custom.generatedEyeBitrate.customMbps
        )
        mvHEVC.generatedMergeQuality = mergeQuality
        av1CRF = videoQuality.custom.av1CRF
        upscaleQuality = quality
    }
}

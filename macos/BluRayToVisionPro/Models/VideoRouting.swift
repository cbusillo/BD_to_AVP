import Foundation

enum VideoRouteKind: String, Equatable {
    case directMVHEVC = "direct_mv_hevc"
    case generatedMVHEVC = "generated_mv_hevc"
    case av1Stereo = "av1"
    case existingArtifact = "existing_artifact"
}

enum GeneratedVideoRouteRequirement: Equatable {
    case restartStage
    case reusableIntermediates
    case softwareEncoder
    case upscale
    case fieldOfView

    var identifier: String {
        switch self {
        case .restartStage:
            "restart_stage_requires_generated_artifacts"
        case .reusableIntermediates:
            "reusable_intermediates_requested"
        case .softwareEncoder:
            "software_encoder_requested"
        case .upscale:
            "upscale_requires_generated_artifacts"
        case .fieldOfView:
            "field_of_view_requires_generated_route"
        }
    }

    var detail: String {
        switch self {
        case .restartStage:
            "The selected restart stage requires file-backed stereo artifacts."
        case .reusableIntermediates:
            "Creating reusable intermediate files for inspection or external processing requires generated left- and right-eye movies."
        case .softwareEncoder:
            "Software HEVC encoding requires generated left- and right-eye movies."
        case .upscale:
            "FX Upscale currently uses completed video files, so this job requires the generated route."
        case .fieldOfView:
            "This field of view is outside the direct encoder's 1°–180° range, so the generated route is required."
        }
    }
}

struct VideoRoutePlan: Equatable {
    static let automaticDirectBitrateMbps = 40
    static let automaticGeneratedEyeBitrateMbps = MVHEVCOptions.defaultGeneratedEyeBitrate
    static let automaticGeneratedMergeQuality = 75

    let kind: VideoRouteKind
    let generatedRequirement: GeneratedVideoRouteRequirement?
    let startStage: Int
    let includesUpscale: Bool
    let directBitrateMode: BitrateMode?
    let directBitrateMbps: Int?
    let generatedEyeBitrateMode: BitrateMode?
    let generatedEyeBitrateMbps: Int?
    let generatedMergeQuality: Int?
    let av1CRF: Int?

    init(
        options: ConversionOptions,
        allowsExistingArtifact: Bool = true
    ) {
        self.init(
            encoding: options.encoding,
            startStage: options.job.startStage.rawValue,
            keepsReusableArtifacts: options.job.intermediatePolicy.createsReusableArtifacts,
            softwareEncoder: options.job.softwareEncoder,
            allowsExistingArtifact: allowsExistingArtifact
        )
    }

    init(
        encoding: EncodingOptions,
        job: JobOptions = JobOptions(),
        allowsExistingArtifact: Bool = true
    ) {
        self.init(
            encoding: encoding,
            startStage: job.startStage.rawValue,
            keepsReusableArtifacts: job.intermediatePolicy.createsReusableArtifacts,
            softwareEncoder: job.softwareEncoder,
            allowsExistingArtifact: allowsExistingArtifact
        )
    }

    init(
        encoding: EncodingOptions,
        startStage: Int,
        keepsReusableArtifacts: Bool,
        softwareEncoder: Bool,
        allowsExistingArtifact: Bool
    ) {
        self.startStage = startStage
        includesUpscale = encoding.videoOutputMode == .mvHEVC && encoding.upscaleEnabled

        if allowsExistingArtifact, startStage > ConversionStage.combineToMVHEVC.rawValue {
            kind = .existingArtifact
            generatedRequirement = nil
            directBitrateMode = nil
            directBitrateMbps = nil
            generatedEyeBitrateMode = nil
            generatedEyeBitrateMbps = nil
            generatedMergeQuality = nil
            av1CRF = nil
            return
        }

        if encoding.videoOutputMode == .av1Stereo {
            kind = .av1Stereo
            generatedRequirement = nil
            directBitrateMode = nil
            directBitrateMbps = nil
            generatedEyeBitrateMode = nil
            generatedEyeBitrateMbps = nil
            generatedMergeQuality = nil
            av1CRF = encoding.av1CRF
            return
        }

        let requirement: GeneratedVideoRouteRequirement?
        if startStage >= ConversionStage.createLeftRightFiles.rawValue {
            requirement = .restartStage
        } else if keepsReusableArtifacts {
            requirement = .reusableIntermediates
        } else if softwareEncoder {
            requirement = .softwareEncoder
        } else if encoding.upscaleEnabled {
            requirement = .upscale
        } else if !(1 ... 180).contains(encoding.fieldOfView) {
            requirement = .fieldOfView
        } else {
            requirement = nil
        }

        if let requirement {
            kind = .generatedMVHEVC
            generatedRequirement = requirement
            directBitrateMode = nil
            directBitrateMbps = nil
            generatedEyeBitrateMode = encoding.mvHEVC.generatedEyeBitrate.mode
            generatedEyeBitrateMbps = Self.resolvedBitrate(
                encoding.mvHEVC.generatedEyeBitrate,
                automatic: Self.automaticGeneratedEyeBitrateMbps
            )
            generatedMergeQuality = encoding.mvHEVC.generatedMergeQuality
            av1CRF = nil
            return
        }

        kind = .directMVHEVC
        generatedRequirement = nil
        directBitrateMode = encoding.mvHEVC.directFinalBitrate.mode
        directBitrateMbps = Self.resolvedBitrate(
            encoding.mvHEVC.directFinalBitrate,
            automatic: Self.automaticDirectBitrateMbps
        )
        generatedEyeBitrateMode = nil
        generatedEyeBitrateMbps = nil
        generatedMergeQuality = nil
        av1CRF = nil
    }

    var usesGeneratedSettings: Bool {
        kind == .generatedMVHEVC
    }

    var allowsFinalizedPreview: Bool {
        kind != .existingArtifact
    }

    var title: String {
        switch kind {
        case .directMVHEVC:
            "Direct MV-HEVC when available"
        case .generatedMVHEVC:
            "Generated MV-HEVC"
        case .av1Stereo:
            "AV1 stereo"
        case .existingArtifact:
            "Existing encoded video artifact"
        }
    }

    var systemImage: String {
        switch kind {
        case .directMVHEVC:
            "bolt.horizontal.circle"
        case .generatedMVHEVC:
            "rectangle.split.2x1"
        case .av1Stereo:
            "rectangle.split.2x1.fill"
        case .existingArtifact:
            "arrow.clockwise.circle"
        }
    }

    var settingsSummary: String {
        switch kind {
        case .directMVHEVC:
            "\(bitratePolicyTitle(directBitrateMode)) · \(directBitrateMbps ?? Self.automaticDirectBitrateMbps) Mbps final"
        case .generatedMVHEVC:
            "\(bitratePolicyTitle(generatedEyeBitrateMode)) · \(generatedEyeBitrateMbps ?? Self.automaticGeneratedEyeBitrateMbps) Mbps per eye · merge \(generatedMergeQuality ?? Self.automaticGeneratedMergeQuality)"
        case .av1Stereo:
            "CRF \(av1CRF ?? 32)"
        case .existingArtifact:
            "No video re-encode"
        }
    }

    var compactSummary: String {
        "\(title) · \(settingsSummary)"
    }

    var detail: String {
        switch kind {
        case .directMVHEVC:
            "The engine confirms stereo MV-HEVC support before reading conversion input. If unavailable, it visibly uses Automatic generated settings instead."
        case .generatedMVHEVC:
            generatedRequirement?.detail ?? "This job creates left- and right-eye movies before assembling MV-HEVC."
        case .av1Stereo:
            "The bundled software encoder creates a full side-by-side AV1 movie."
        case .existingArtifact:
            "The selected restart stage resumes from an existing encoded video artifact, so encoder controls are not applied."
        }
    }

    var pipelineSteps: [String] {
        switch kind {
        case .directMVHEVC:
            ["1 Prepare", "2–3 Extract 3D", "4 Direct MV-HEVC", "7–9 Finish"]
        case .generatedMVHEVC:
            if includesUpscale {
                ["1 Prepare", "2–4 Eye files", "5 Merge MV-HEVC", "6 FX Upscale", "7–9 Finish"]
            } else {
                ["1 Prepare", "2–4 Eye files", "5 Merge MV-HEVC", "7–9 Finish"]
            }
        case .av1Stereo:
            ["1 Prepare", "2–3 Extract 3D", "4–5 AV1 stereo", "7–9 Finish"]
        case .existingArtifact:
            ["Resume stage \(startStage)", "Use encoded artifact", "Finish remaining stages"]
        }
    }

    var pipelineDetail: String {
        switch kind {
        case .directMVHEVC:
            "Direct MV-HEVC writes the stage-4 spatial movie and skips stage 5."
        case .generatedMVHEVC:
            if includesUpscale {
                "Generated MV-HEVC creates eye movies at stage 4, assembles them at stage 5, and upscales at stage 6."
            } else {
                "Generated MV-HEVC creates eye movies at stage 4 and assembles them at stage 5."
            }
        case .av1Stereo:
            "AV1 uses stages 1–5 and 7–9; FX Upscale is unavailable."
        case .existingArtifact:
            "Earlier video stages are preserved and not repeated."
        }
    }

    private static func resolvedBitrate(_ preference: BitratePreference, automatic: Int) -> Int {
        if preference.mode == .custom, let customMbps = preference.customMbps {
            return customMbps
        }
        return automatic
    }

    private func bitratePolicyTitle(_ mode: BitrateMode?) -> String {
        mode == .custom ? "Custom" : "Automatic"
    }
}

extension VideoRouteReport {
    var kind: VideoRouteKind? {
        VideoRouteKind(rawValue: selected)
    }

    var isFallback: Bool {
        fallbackReason != nil
    }

    var displayTitle: String {
        switch kind {
        case .directMVHEVC:
            "Direct MV-HEVC"
        case .generatedMVHEVC:
            isFallback ? "Generated MV-HEVC fallback" : "Generated MV-HEVC"
        case .av1Stereo:
            "AV1 stereo"
        case .existingArtifact:
            "Existing encoded video artifact"
        case .none:
            "Video route"
        }
    }

    var systemImage: String {
        switch kind {
        case .directMVHEVC:
            "bolt.horizontal.circle.fill"
        case .generatedMVHEVC:
            isFallback ? "arrow.triangle.branch" : "rectangle.split.2x1.fill"
        case .av1Stereo:
            "rectangle.split.2x1.fill"
        case .existingArtifact:
            "arrow.clockwise.circle.fill"
        case .none:
            "questionmark.circle"
        }
    }

    var settingsSummary: String {
        switch kind {
        case .directMVHEVC:
            return bitrateMbps.map { "\($0) Mbps final" } ?? "Automatic final bitrate"
        case .generatedMVHEVC:
            if let eyeBitrateMbps, let mergeQuality {
                return "\(eyeBitrateMbps) Mbps per eye · merge \(mergeQuality)"
            }
            return "Generated stereo video"
        case .av1Stereo:
            return crf.map { "CRF \($0)" } ?? "Software AV1"
        case .existingArtifact:
            return "No video re-encode"
        case .none:
            return selected.replacingOccurrences(of: "_", with: " ")
        }
    }

    var compactSummary: String {
        "\(displayTitle) · \(settingsSummary)"
    }

    var displayDetail: String {
        if let fallbackReason {
            let fallback = Self.fallbackDescription(fallbackReason)
            if fallbackTiming == "pre_input" {
                return "\(fallback) The route changed before conversion input was read."
            }
            return fallback
        }
        return Self.reasonDescription(reason)
    }

    private static func reasonDescription(_ reason: String) -> String {
        switch reason {
        case "direct_eligible":
            "This Mac confirmed direct stereo MV-HEVC support before conversion input."
        case "generated_route_requested":
            "The requested settings require generated eye movies."
        case "restart_stage_requires_generated_artifacts":
            "The selected restart stage requires generated stereo artifacts."
        case "reusable_intermediates_requested":
            "Reusable intermediate files were requested."
        case "software_encoder_requested":
            "Software HEVC encoding requires generated eye movies."
        case "upscale_requires_generated_artifacts":
            "FX Upscale requires the file-backed generated route."
        case "field_of_view_requires_generated_route":
            "The selected field of view requires the generated route."
        case "av1_output_requested":
            "AV1 stereo output was requested."
        case "resume_uses_existing_video_artifact":
            "The selected restart stage uses an existing encoded video artifact."
        case "direct_capability_unavailable":
            "Direct stereo MV-HEVC was unavailable, so preflight selected generated MV-HEVC."
        default:
            "The conversion engine selected this route during preflight."
        }
    }

    private static func fallbackDescription(_ reason: String) -> String {
        switch reason {
        case "helper_missing":
            "The packaged direct MV-HEVC encoder was unavailable, so the job uses generated eye movies."
        case "helper_not_executable":
            "The packaged direct MV-HEVC encoder could not be launched, so the job uses generated eye movies."
        case "stereo_mv_hevc_encode_unavailable":
            "This Mac could not initialize stereo MV-HEVC encoding, so the job uses generated eye movies."
        default:
            "Direct MV-HEVC was unavailable, so the job uses generated eye movies."
        }
    }
}

struct VideoStorageEstimate: Equatable {
    let finalOutputBytes: Int64?
    let peakWorkingBytes: Int64?
    let retainedIntermediateBytes: Int64?
    let conservativeFallbackReserve: Bool
    let unavailableReason: String?

    init(drafts: [ConversionDraft]) {
        guard !drafts.isEmpty else {
            finalOutputBytes = nil
            peakWorkingBytes = nil
            retainedIntermediateBytes = nil
            conservativeFallbackReserve = false
            unavailableReason = "Available after source analysis."
            return
        }

        var committedBytes: Int64 = 0
        var totalFinalBytes: Int64 = 0
        var totalRetainedBytes: Int64 = 0
        var maximumPeakBytes: Int64 = 0
        var usesFallbackReserve = false

        for draft in drafts {
            guard let estimate = Self.estimate(draft: draft) else {
                finalOutputBytes = nil
                peakWorkingBytes = nil
                retainedIntermediateBytes = nil
                conservativeFallbackReserve = false
                unavailableReason = Self.unavailableReason(for: draft)
                return
            }
            maximumPeakBytes = max(maximumPeakBytes, committedBytes + estimate.peakBytes)
            committedBytes += estimate.finalBytes + estimate.retainedBytes
            totalFinalBytes += estimate.finalBytes
            totalRetainedBytes += estimate.retainedBytes
            usesFallbackReserve = usesFallbackReserve || estimate.conservativeFallbackReserve
        }

        finalOutputBytes = totalFinalBytes
        peakWorkingBytes = maximumPeakBytes
        retainedIntermediateBytes = totalRetainedBytes
        conservativeFallbackReserve = usesFallbackReserve
        unavailableReason = nil
    }

    var finalOutputDescription: String {
        guard let description = formatted(finalOutputBytes) else {
            return unavailableReason ?? "Not available"
        }
        return conservativeFallbackReserve ? "Up to \(description)" : "About \(description)"
    }

    var peakWorkingDescription: String {
        guard let description = formatted(peakWorkingBytes) else {
            return unavailableReason ?? "Not available"
        }
        return conservativeFallbackReserve ? "Up to \(description)" : "About \(description)"
    }

    var retainedIntermediateDescription: String {
        guard let retainedIntermediateBytes else {
            return unavailableReason ?? "Not available"
        }
        if retainedIntermediateBytes == 0 {
            return "None after success"
        }
        return "About \(formatted(retainedIntermediateBytes) ?? "0 bytes")"
    }

    var detail: String {
        if let unavailableReason {
            return unavailableReason
        }
        if conservativeFallbackReserve {
            return "Video-only estimate with overhead. Peak reserve includes the larger generated-fallback route; audio, subtitles, and source preparation can add space."
        }
        return "Video-only estimate with overhead. Audio, subtitles, and source preparation can add space."
    }

    private struct DraftEstimate {
        let finalBytes: Int64
        let peakBytes: Int64
        let retainedBytes: Int64
        let conservativeFallbackReserve: Bool
    }

    private static func estimate(draft: ConversionDraft) -> DraftEstimate? {
        guard let durationSeconds = draft.estimatedDurationSeconds,
              durationSeconds.isFinite,
              durationSeconds > 0
        else {
            return nil
        }

        let route = VideoRoutePlan(options: draft.options)
        switch route.kind {
        case .directMVHEVC:
            let directFinal = bytes(
                bitrateMbps: route.directBitrateMbps ?? VideoRoutePlan.automaticDirectBitrateMbps,
                durationSeconds: durationSeconds
            )
            let fallbackFinal = bytes(
                bitrateMbps: VideoRoutePlan.automaticGeneratedEyeBitrateMbps * 2,
                durationSeconds: durationSeconds
            )
            guard let directFinal, let fallbackFinal else {
                return nil
            }
            return DraftEstimate(
                finalBytes: max(directFinal, fallbackFinal),
                peakBytes: max(directFinal, fallbackFinal * 2),
                retainedBytes: 0,
                conservativeFallbackReserve: true
            )
        case .generatedMVHEVC:
            let aggregateBitrate = (route.generatedEyeBitrateMbps
                ?? VideoRoutePlan.automaticGeneratedEyeBitrateMbps) * 2
            guard let finalBytes = bytes(
                bitrateMbps: aggregateBitrate,
                durationSeconds: durationSeconds
            ) else {
                return nil
            }
            let eyeBytes = finalBytes
            let retainedBytes = draft.options.job.intermediatePolicy.createsReusableArtifacts ? eyeBytes : 0
            return DraftEstimate(
                finalBytes: finalBytes,
                peakBytes: finalBytes + eyeBytes,
                retainedBytes: retainedBytes,
                conservativeFallbackReserve: false
            )
        case .av1Stereo, .existingArtifact:
            return nil
        }
    }

    private static func unavailableReason(for draft: ConversionDraft) -> String {
        switch VideoRoutePlan(options: draft.options).kind {
        case .av1Stereo:
            "AV1 size varies with source content and CRF."
        case .existingArtifact:
            "This restart uses an existing encoded video artifact."
        case .directMVHEVC, .generatedMVHEVC:
            "Available after source duration is known."
        }
    }

    private static func bytes(bitrateMbps: Int, durationSeconds: Double) -> Int64? {
        let estimatedBytes = Double(bitrateMbps) * 1_000_000 / 8 * durationSeconds * 1.15
        guard estimatedBytes.isFinite, estimatedBytes >= 0, estimatedBytes <= Double(Int64.max) else {
            return nil
        }
        return Int64(estimatedBytes.rounded(.up))
    }

    private func formatted(_ bytes: Int64?) -> String? {
        guard let bytes else {
            return nil
        }
        return ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }
}

extension ConversionDraft {
    var estimatedDurationSeconds: Double? {
        if let selectedTitle, selectedTitle.durationSeconds > 0 {
            return selectedTitle.durationSeconds
        }
        guard let durationSeconds = sourceDetails?.durationSeconds, durationSeconds > 0 else {
            return nil
        }
        return durationSeconds
    }
}

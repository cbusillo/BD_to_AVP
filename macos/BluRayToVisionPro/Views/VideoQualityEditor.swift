import SwiftUI

struct VideoQualityEditor: View {
    enum Context: Equatable {
        case profile
        case conversion(JobOptions)

        var jobOptions: JobOptions {
            switch self {
            case .profile:
                JobOptions()
            case let .conversion(jobOptions):
                jobOptions
            }
        }

        var isProfile: Bool {
            self == .profile
        }
    }

    @Binding var options: EncodingOptions
    let context: Context
    @ObservedObject var routeQualityState: RouteQualityResolutionState

    @State private var showsExpertControls: Bool
    @State private var showsDirectRateControl: Bool
    @State private var showsGeneratedRateControl: Bool
    @State private var selectionError: String?

    init(
        options: Binding<EncodingOptions>,
        context: Context,
        routeQualityState: RouteQualityResolutionState = RouteQualityResolutionState()
    ) {
        _options = options
        self.context = context
        self.routeQualityState = routeQualityState
        _showsExpertControls = State(initialValue: options.wrappedValue.videoQuality.mode == .custom)
        _showsDirectRateControl = State(
            initialValue: options.wrappedValue.videoQuality.custom.directFinalBitrate.mode == .custom
        )
        _showsGeneratedRateControl = State(
            initialValue: options.wrappedValue.videoQuality.custom.generatedEyeBitrate.mode == .custom
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            switch routePlan.kind {
            case .existingArtifact:
                existingArtifactControls
            case .av1Stereo:
                qualitySelectionControl
                expertControls
            case .directMVHEVC, .generatedMVHEVC:
                qualitySelectionControl
                Toggle("AI FX upscale to 2× resolution", isOn: upscaleBinding)
                expertControls
            }

            if let selectionError {
                Label(selectionError, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .onChange(of: options.videoQuality.mode) { _, mode in
            if mode == .custom {
                showsExpertControls = true
            }
        }
        .onChange(of: options.videoQuality.custom.directFinalBitrate.mode) { _, mode in
            showsDirectRateControl = mode == .custom
        }
        .onChange(of: options.videoQuality.custom.generatedEyeBitrate.mode) { _, mode in
            showsGeneratedRateControl = mode == .custom
        }
    }

    @ViewBuilder
    private var existingArtifactControls: some View {
        if options.videoOutputMode == .mvHEVC,
           routePlan.startStage == ConversionStage.upscaleVideo.rawValue
        {
            Toggle("AI FX upscale to 2× resolution", isOn: upscaleBinding)
            if options.upscaleEnabled {
                qualitySelectionControl
                expertControls
            } else {
                retainedQualityStatus(
                    detail: "The retained quality choice is not applied unless stage 6 file upscale is enabled."
                )
            }
        } else {
            retainedQualityStatus(
                detail: "This restart reuses an encoded video artifact, so retained quality settings are not applied."
            )
        }
    }

    private var qualitySelectionControl: some View {
        QualitySelectionControl(
            selection: options.videoQuality.selection,
            availableSteps: availableSteps,
            outputMode: options.videoOutputMode,
            selectStep: selectQualityStep,
            selectCustom: selectCustomQuality
        )
    }

    private func retainedQualityStatus(detail: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            LabeledContent("Video quality") {
                Text("Retained for an earlier restart stage")
                    .foregroundStyle(.secondary)
            }
            Text(detail)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Video quality")
        .accessibilityValue("Retained for an earlier restart stage")
        .accessibilityHint(detail)
    }

    private var expertControls: some View {
        DisclosureGroup(isExpanded: $showsExpertControls) {
            VStack(alignment: .leading, spacing: 12) {
                Text(expertStateDetail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                switch routePlan.kind {
                case .directMVHEVC:
                    directRateControls
                    Divider()
                    generatedControls(title: "Generated fallback", labelPrefix: "Generated fallback")
                    if options.upscaleEnabled || context.isProfile {
                        Divider()
                        upscaleControls(retainedWhenDisabled: !options.upscaleEnabled)
                    }
                case .generatedMVHEVC:
                    generatedControls(title: "Generated MV-HEVC", labelPrefix: "Generated")
                    if options.upscaleEnabled {
                        Divider()
                        upscaleControls(retainedWhenDisabled: false)
                    }
                case .av1Stereo:
                    av1Controls
                case .existingArtifact:
                    upscaleControls(retainedWhenDisabled: false)
                }
            }
            .padding(.top, 6)
        } label: {
            HStack(spacing: 8) {
                Text("Expert quality controls")
                Spacer()
                Text(options.videoQuality.mode == .custom ? "Active" : "Retained")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(options.videoQuality.mode == .custom ? Color.accentColor : .secondary)
            }
        }
        .accessibilityLabel("Expert quality controls")
        .accessibilityValue(options.videoQuality.mode == .custom ? "Active" : "Retained, not active")
        .accessibilityHint("Editing any value selects Custom video quality.")
    }

    private var directRateControls: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Direct MV-HEVC")
                .font(.subheadline.weight(.semibold))

            LabeledContent("Direct final rate control") {
                Text(directRateSummary)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.trailing)
            }

            DisclosureGroup("Direct final bitrate", isExpanded: $showsDirectRateControl) {
                Picker("Direct rate policy", selection: directRateModeBinding) {
                    Text("Adaptive").tag(BitrateMode.automatic)
                    Text("Fixed Mbps").tag(BitrateMode.custom)
                }
                .pickerStyle(.segmented)

                if customValues.directFinalBitrate.mode == .custom {
                    LabeledContent("Direct fixed target") {
                        Stepper(value: directRateBinding, in: 1 ... 500) {
                            Text("\(directCustomMbps) Mbps final")
                                .monospacedDigit()
                                .multilineTextAlignment(.trailing)
                        }
                    }
                } else {
                    Text("Adaptive mode lets the direct encoder vary bitrate with source complexity.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func generatedControls(title: String, labelPrefix: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.subheadline.weight(.semibold))

            ExpertQualitySliderRow(
                title: "\(labelPrefix) merge quality",
                value: generatedMergeQualityBinding
            )

            LabeledContent("\(labelPrefix) eye bitrate") {
                Text(generatedRateSummary)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.trailing)
            }

            DisclosureGroup("\(labelPrefix) eye bitrate", isExpanded: $showsGeneratedRateControl) {
                Picker("\(labelPrefix) bitrate policy", selection: generatedRateModeBinding) {
                    Text("Recommended default").tag(BitrateMode.automatic)
                    Text("Fixed Mbps").tag(BitrateMode.custom)
                }
                .pickerStyle(.segmented)

                if customValues.generatedEyeBitrate.mode == .custom {
                    LabeledContent("\(labelPrefix) fixed target") {
                        Stepper(value: generatedRateBinding, in: 1 ... 500) {
                            Text("\(generatedCustomMbps) Mbps per eye")
                                .monospacedDigit()
                                .multilineTextAlignment(.trailing)
                        }
                    }
                } else {
                    Text(
                        "The recommended default currently resolves to \(VideoRoutePlan.automaticGeneratedEyeBitrateMbps) Mbps per eye."
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func upscaleControls(retainedWhenDisabled: Bool) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            ExpertQualitySliderRow(
                title: retainedWhenDisabled ? "Retained file-upscale quality" : "File-upscale quality",
                value: upscaleQualityBinding
            )

            if routePlan.kind != .existingArtifact {
                Toggle(
                    "Link generated merge and file-upscale quality",
                    isOn: linkGeneratedAndUpscaleQualityBinding
                )
            }
        }
    }

    private var av1Controls: some View {
        VStack(alignment: .leading, spacing: 8) {
            LabeledContent("AV1 quality") {
                Stepper(value: av1CRFBinding, in: 0 ... 63) {
                    Text("CRF \(customValues.av1CRF)")
                        .monospacedDigit()
                        .frame(width: 74, alignment: .trailing)
                }
            }

            Text("Lower CRF values preserve more detail and create larger files. AV1 always uses Custom quality.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var routePlan: VideoRoutePlan {
        VideoRoutePlan(encoding: options, job: context.jobOptions)
    }

    private var availableSteps: Set<QualityStep> {
        guard options.videoOutputMode == .mvHEVC else {
            return []
        }
        let steps: [QualityStep]
        switch routePlan.kind {
        case .directMVHEVC:
            steps = VideoQualityCatalog.supportedSteps(
                for: routePlan.includesUpscale ? .directMVHEVCMetalFX2x : .directMVHEVC
            )
        case .generatedMVHEVC:
            steps = VideoQualityCatalog.supportedGeneratedSteps(
                includingFileUpscale: routePlan.includesUpscale
            )
        case .existingArtifact:
            steps = routePlan.includesUpscale
                ? VideoQualityCatalog.supportedSteps(for: .fileUpscale)
                : []
        case .av1Stereo:
            steps = []
        }
        return Set(steps)
    }

    private var expertStateDetail: String {
        if options.videoQuality.mode == .custom {
            return "These exact settings are active for the applicable route."
        }
        return "These retained Custom values are not active. Editing any value restores the complete Custom snapshot."
    }

    private var customValues: VideoQualityCustomValues {
        options.videoQuality.custom
    }

    private var directCustomMbps: Int {
        customValues.directFinalBitrate.customMbps ?? VideoQualityCatalog.defaultDirectCustomBitrateMbps
    }

    private var generatedCustomMbps: Int {
        customValues.generatedEyeBitrate.customMbps ?? VideoQualityCatalog.balancedGeneratedEyeBitrateMbps
    }

    private var directRateSummary: String {
        switch customValues.directFinalBitrate.mode {
        case .automatic:
            "Adaptive · content-dependent"
        case .custom:
            "Fixed · \(directCustomMbps) Mbps final"
        }
    }

    private var generatedRateSummary: String {
        switch customValues.generatedEyeBitrate.mode {
        case .automatic:
            "Recommended · \(VideoRoutePlan.automaticGeneratedEyeBitrateMbps) Mbps per eye"
        case .custom:
            "Fixed · \(generatedCustomMbps) Mbps per eye"
        }
    }

    private func selectQualityStep(_ step: QualityStep) {
        guard availableSteps.contains(step) else {
            return
        }
        applyRouteEdit(.qualityStep(step))
        selectionError = routeQualityState.invalidMessage
    }

    private func selectCustomQuality() {
        applyRouteEdit(.customQuality(options.videoQuality.custom))
        selectionError = routeQualityState.invalidMessage
        if routeQualityState.conflict == nil, routeQualityState.invalidMessage == nil {
            showsExpertControls = true
        }
    }

    private var upscaleBinding: Binding<Bool> {
        Binding(
            get: { options.upscaleEnabled },
            set: { enabled in
                applyRouteEdit(.upscaleEnabled(enabled))
            }
        )
    }

    private func applyRouteEdit(_ edit: RouteQualityEdit) {
        var conversionOptions = ConversionOptions(
            encoding: options,
            job: context.jobOptions
        )
        routeQualityState.apply(edit, to: &conversionOptions)
        options = conversionOptions.encoding
    }

    private func editCustomQuality(
        _ edit: (inout VideoQualityCustomValues) -> Void
    ) {
        var values = options.videoQuality.custom
        edit(&values)
        applyRouteEdit(.customQuality(values))
        selectionError = routeQualityState.invalidMessage
    }

    private var directRateModeBinding: Binding<BitrateMode> {
        Binding(
            get: { customValues.directFinalBitrate.mode },
            set: { mode in
                editCustomQuality { custom in
                    custom.directFinalBitrate.mode = mode
                    if mode == .custom, custom.directFinalBitrate.customMbps == nil {
                        custom.directFinalBitrate.customMbps = VideoQualityCatalog.defaultDirectCustomBitrateMbps
                    }
                }
            }
        )
    }

    private var directRateBinding: Binding<Int> {
        Binding(
            get: { directCustomMbps },
            set: { newValue in
                editCustomQuality { custom in
                    custom.directFinalBitrate.mode = .custom
                    custom.directFinalBitrate.customMbps = newValue
                }
            }
        )
    }

    private var generatedRateModeBinding: Binding<BitrateMode> {
        Binding(
            get: { customValues.generatedEyeBitrate.mode },
            set: { mode in
                editCustomQuality { custom in
                    custom.generatedEyeBitrate.mode = mode
                    if mode == .custom, custom.generatedEyeBitrate.customMbps == nil {
                        custom.generatedEyeBitrate.customMbps = VideoQualityCatalog.balancedGeneratedEyeBitrateMbps
                    }
                }
            }
        )
    }

    private var generatedRateBinding: Binding<Int> {
        Binding(
            get: { generatedCustomMbps },
            set: { newValue in
                editCustomQuality { custom in
                    custom.generatedEyeBitrate.mode = .custom
                    custom.generatedEyeBitrate.customMbps = newValue
                }
            }
        )
    }

    private var generatedMergeQualityBinding: Binding<Int> {
        Binding(
            get: { customValues.generatedMergeQuality },
            set: { newValue in
                let linked = options.mvHEVC.linkGeneratedAndUpscaleQuality
                editCustomQuality { custom in
                    custom.generatedMergeQuality = newValue
                    if linked {
                        custom.upscaleQuality = newValue
                    }
                }
            }
        )
    }

    private var upscaleQualityBinding: Binding<Int> {
        Binding(
            get: { customValues.upscaleQuality },
            set: { newValue in
                let linked = options.mvHEVC.linkGeneratedAndUpscaleQuality
                editCustomQuality { custom in
                    custom.upscaleQuality = newValue
                    if linked {
                        custom.generatedMergeQuality = newValue
                    }
                }
            }
        )
    }

    private var linkGeneratedAndUpscaleQualityBinding: Binding<Bool> {
        Binding(
            get: { options.mvHEVC.linkGeneratedAndUpscaleQuality },
            set: { linked in
                applyRouteEdit(.linkGeneratedAndUpscaleQuality(linked))
            }
        )
    }

    private var av1CRFBinding: Binding<Int> {
        Binding(
            get: { customValues.av1CRF },
            set: { newValue in
                editCustomQuality { custom in
                    custom.av1CRF = newValue
                }
            }
        )
    }
}

private struct QualitySelectionControl: View {
    let selection: VideoQualitySelection
    let availableSteps: Set<QualityStep>
    let outputMode: VideoOutputMode
    let selectStep: (QualityStep) -> Void
    let selectCustom: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text("Video quality")
                    .font(.headline)
                Spacer()
                Text(selectionValue)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.secondary)
            }

            ScrollView(.horizontal, showsIndicators: false) {
                wideLadder
            }

            Button(action: selectCustom) {
                HStack(spacing: 10) {
                    Image(systemName: isCustomSelected ? "checkmark.circle.fill" : "slider.horizontal.3")
                        .foregroundStyle(isCustomSelected ? Color.accentColor : .secondary)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Custom")
                            .fontWeight(.medium)
                        Text("Restore retained expert values for the active route.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                }
                .padding(10)
                .background(customBackground, in: RoundedRectangle(cornerRadius: 9))
                .overlay {
                    RoundedRectangle(cornerRadius: 9)
                        .stroke(isCustomSelected ? Color.accentColor : Color.secondary.opacity(0.25))
                }
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Custom video quality")
            .accessibilityValue(isCustomSelected ? "Selected" : "Not selected")
            .accessibilityHint("Restores retained expert values and reveals route-specific controls.")
            .accessibilityAddTraits(isCustomSelected ? .isSelected : [])

            Text(selectionDetail)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Video quality")
        .accessibilityValue(selectionValue)
        .accessibilityHint("Choose a calibrated guided step or Custom expert settings.")
    }

    private var wideLadder: some View {
        HStack(alignment: .top, spacing: 5) {
            ForEach(QualityStep.allCases) { step in
                if availableSteps.contains(step) {
                    Button {
                        selectStep(step)
                    } label: {
                        QualityStepCell(
                            step: step,
                            selected: selection == .step(step),
                            available: true
                        )
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(step.title)
                    .accessibilityValue(stepAccessibilityValue(step, available: true))
                    .accessibilityHint("Uses the checked values for this output.")
                    .accessibilityAddTraits(selection == .step(step) ? .isSelected : [])
                } else {
                    QualityStepCell(
                        step: step,
                        selected: selection == .step(step),
                        available: false
                    )
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel(step.title)
                        .accessibilityValue(stepAccessibilityValue(step, available: false))
                        .accessibilityHint(unavailableHint)
                }
            }
        }
        .fixedSize(horizontal: true, vertical: false)
    }

    private var isCustomSelected: Bool {
        selection == .custom
    }

    private var selectionValue: String {
        switch selection {
        case let .step(step):
            availableSteps.contains(step)
                ? "\(step.title), step \(step.ordinal) of \(QualityStep.allCases.count)"
                : "Custom, retained values; previous guided step is unavailable"
        case .custom:
            "Custom"
        }
    }

    private var selectionTitle: String {
        switch selection {
        case let .step(step):
            availableSteps.contains(step) ? step.title : "Custom"
        case .custom:
            "Custom"
        }
    }

    private var selectionDetail: String {
        switch selection {
        case let .step(step):
            availableSteps.contains(step)
                ? step.detail
                : "The retained guided choice is unavailable for the active route; choose another step or Custom."
        case .custom:
            "Exact direct, generated, AV1, and upscale values are retained independently from the guided ladder."
        }
    }

    private var customBackground: Color {
        isCustomSelected ? Color.accentColor.opacity(0.12) : Color.secondary.opacity(0.06)
    }

    private var unavailableHint: String {
        outputMode == .av1Stereo
            ? "AV1 uses Custom CRF and does not have guided steps."
            : "This guided step is not supported by the active video route."
    }

    private func stepAccessibilityValue(_ step: QualityStep, available: Bool) -> String {
        var parts = ["Step \(step.ordinal) of \(QualityStep.allCases.count)"]
        if selection == .step(step) {
            parts.append("selected")
        }
        parts.append(available ? "available" : "unavailable")
        return parts.joined(separator: ", ")
    }

}

private struct QualityStepCell: View {
    let step: QualityStep
    let selected: Bool
    let available: Bool

    var body: some View {
        VStack(spacing: 5) {
            ZStack {
                Circle()
                    .fill(circleFill)
                    .frame(width: 26, height: 26)
                Text("\(step.ordinal)")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(circleForeground)
            }

            Text(step.title)
                .font(.caption.weight(selected ? .semibold : .regular))
                .multilineTextAlignment(.center)
                .lineLimit(2)
                .frame(height: 32, alignment: .top)

            Text(available ? "Available" : "Unavailable")
                .font(.caption2)
                .foregroundStyle(available ? Color.secondary : Color.secondary.opacity(0.75))
        }
        .frame(width: 72, height: 84)
        .padding(.vertical, 6)
        .background(background, in: RoundedRectangle(cornerRadius: 9))
        .overlay {
            RoundedRectangle(cornerRadius: 9)
                .stroke(border)
        }
        .opacity(available ? 1 : 0.72)
        .contentShape(RoundedRectangle(cornerRadius: 9))
    }

    private var circleFill: Color {
        if selected {
            return .accentColor
        }
        return available ? Color.accentColor.opacity(0.14) : Color.secondary.opacity(0.12)
    }

    private var circleForeground: Color {
        selected ? .white : available ? .accentColor : .secondary
    }

    private var background: Color {
        selected ? Color.accentColor.opacity(0.12) : Color.secondary.opacity(0.05)
    }

    private var border: Color {
        selected ? .accentColor : Color.secondary.opacity(0.22)
    }
}

private struct ExpertQualitySliderRow: View {
    let title: String
    @Binding var value: Int

    var body: some View {
        LabeledContent(title) {
            HStack(spacing: 10) {
                Slider(
                    value: Binding(
                        get: { Double(value) },
                        set: { value = Int($0.rounded()) }
                    ),
                    in: 0 ... 100,
                    step: 1
                )
                .frame(minWidth: 120, idealWidth: 180)

                Text("\(value)")
                    .monospacedDigit()
                    .frame(width: 30, alignment: .trailing)
            }
        }
    }
}

import RealityKit
import SwiftUI
import UniformTypeIdentifiers

struct PlaybackProbeView: View {
    private static let playerScaleInset: Float = 0.86
    private static let playerDepthOffset: Float = -0.14

    @ObservedObject var model: PlaybackProbeModel
    @State private var isImporting = false
    @State private var showsTechnicalDetails = false

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            HStack(spacing: 0) {
                playerPane
                if model.validationPhase != .running {
                    Divider()
                    guidePane
                        .frame(width: 390)
                }
            }
        }
        .frame(minWidth: 960, minHeight: 620)
        .background(.ultraThinMaterial)
        .fileImporter(
            isPresented: $isImporting,
            allowedContentTypes: [.movie],
            allowsMultipleSelection: false
        ) { result in
            switch result {
            case let .success(urls):
                if let url = urls.first {
                    model.importAsset(from: url)
                }
            case let .failure(error):
                model.reportImportFailure(error)
            }
        }
    }

    private var header: some View {
        HStack(spacing: 16) {
            Image(systemName: "visionpro")
                .font(.title2)
                .foregroundStyle(.tint)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 2) {
                Text("BD to AVP Playback Check")
                    .font(.headline)
                    .accessibilityIdentifier("playback-check-title")
                Text(headerSubtitle)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }

            Spacer()

            Button("Choose Movie…") {
                isImporting = true
            }
            .disabled(model.validationPhase == .running || model.isLoading)
            .accessibilityIdentifier("choose-movie-button")
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 16)
        .background(.regularMaterial)
    }

    private var playerPane: some View {
        VStack(spacing: 0) {
            if model.isLoading {
                loadingPlayerState
            } else if !model.hasLoadedAsset {
                emptyPlayerState
            } else {
                spatialPlayerSurface
            }

            if model.validationPhase != .running {
                Divider()
                playbackControls
            }
        }
    }

    private var emptyPlayerState: some View {
        ZStack {
            Color.black
            ContentUnavailableView {
                Label("Choose a Finished Movie", systemImage: "film.stack")
            } description: {
                Text("Select one BD to AVP movie to check playback on this Vision Pro.")
            } actions: {
                Button("Choose Movie…") {
                    isImporting = true
                }
            }
            .foregroundStyle(.white)
        }
        .aspectRatio(CGSize(width: 16, height: 9), contentMode: .fit)
    }

    private var loadingPlayerState: some View {
        ZStack {
            Color.black
            ProgressView("Preparing the movie…")
                .padding(24)
                .glassBackgroundEffect()
        }
        .aspectRatio(CGSize(width: 16, height: 9), contentMode: .fit)
    }

    private var spatialPlayerSurface: some View {
        GeometryReader3D { geometry in
            ZStack {
                Color.black

                RealityView { content in
                    model.installPlayerComponent()
                    if model.playerEntity.parent == nil {
                        content.add(model.playerEntity)
                    }
                    scalePlayerEntity(proxy: geometry, content: content)
                } update: { content in
                    scalePlayerEntity(proxy: geometry, content: content)
                }
            }
        }
        .aspectRatio(CGSize(width: 16, height: 9), contentMode: .fit)
        .background(.black)
    }

    private var playbackControls: some View {
        HStack(spacing: 12) {
            Button {
                model.togglePlayback()
            } label: {
                Label(model.isPlaying ? "Pause" : "Play", systemImage: model.isPlaying ? "pause.fill" : "play.fill")
            }
            .disabled(!model.canControlPlayback)

            Button("Beginning") {
                model.seek(to: .beginning)
            }
            .disabled(!model.canSeek)

            Button("Middle") {
                model.seek(to: .middle)
            }
            .disabled(!model.canSeek)

            Button("End") {
                model.seek(to: .end)
            }
            .disabled(!model.canSeek)

            Spacer()

            Text(model.timeSummary)
                .font(.system(.body, design: .monospaced))
                .foregroundStyle(.secondary)
        }
        .buttonStyle(.bordered)
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
        .background(.regularMaterial)
    }

    private var guidePane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                guideContent

                if let failure = model.failure, model.validationPhase != .selectMovie {
                    failureSection(failure)
                }

                if model.hasLoadedAsset {
                    technicalDetails
                }
            }
            .padding(24)
        }
        .background(.thinMaterial)
    }

    @ViewBuilder
    private var guideContent: some View {
        if model.isLoading {
            preparingContent
        } else {
            switch model.validationPhase {
            case .selectMovie:
                selectMovieContent
            case .preparing:
                preparingContent
            case .ready:
                readyContent
            case .running:
                runningContent
            case .observations:
                observationsContent
            case .complete:
                completedContent
            }
        }
    }

    private var selectMovieContent: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Check a movie before release")
                    .font(.title2.bold())
                Text("This guided check plays three short sections and records the technical results for you.")
                    .foregroundStyle(.secondary)
            }

            assuranceCard

            if let failure = model.failure {
                failureSection(failure)
            }

            Button {
                isImporting = true
            } label: {
                Label("Choose a Movie", systemImage: "folder")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
        }
    }

    private var preparingContent: some View {
        VStack(alignment: .leading, spacing: 16) {
            ProgressView()
                .controlSize(.large)
            Text("Preparing the movie")
                .font(.title2.bold())
            Text("The app is preparing a temporary local copy, reading its playback options, and creating a SHA-256 fingerprint. A full-length movie can take several minutes.")
                .foregroundStyle(.secondary)
            assuranceCard
        }
    }

    private var readyContent: some View {
        VStack(alignment: .leading, spacing: 18) {
            Label("Movie ready", systemImage: "checkmark.circle.fill")
                .font(.title2.bold())
                .foregroundStyle(.green)

            Text("The check takes about 15 seconds. Keep this window visible while it plays the beginning, middle, and end.")
                .foregroundStyle(.secondary)

            Text(model.expectedPresentationGuidance)
                .font(.callout)
                .foregroundStyle(.secondary)

            VStack(alignment: .leading, spacing: 12) {
                instructionRow(number: 1, text: "Start the guided check")
                instructionRow(number: 2, text: "Watch the picture during three short seeks")
                instructionRow(number: 3, text: "Answer two plain-language questions")
            }

            Button {
                model.startGuidedValidation()
            } label: {
                Label("Run Playback Check", systemImage: "play.circle.fill")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(!model.canStartValidation)
            .accessibilityIdentifier("run-playback-check-button")

            assuranceCard
        }
    }

    private var runningContent: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Checking playback")
                .font(.title2.bold())
            Text(model.currentValidationStepText)
                .foregroundStyle(.secondary)

            ProgressView(
                value: Double(model.completedCheckCount),
                total: Double(model.validationChecks.count)
            )

            checkList
        }
    }

    private var observationsContent: some View {
        VStack(alignment: .leading, spacing: 18) {
            Label(
                model.automaticStatusText,
                systemImage: model.automaticChecksPassed ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
            )
            .font(.headline)
            .foregroundStyle(model.automaticChecksPassed ? .green : .orange)
            .accessibilityIdentifier("automatic-check-status")

            VStack(alignment: .leading, spacing: 6) {
                Text("Two things only you can confirm")
                    .font(.title2.bold())
                Text("These answers record what you saw. They do not approve a release or publish anything.")
                    .foregroundStyle(.secondary)
            }

            observationCard(
                title: "Did the picture stay visible during the entire check?",
                detail: "Answer No if the video disappeared, turned black, or moved into a window you could not find.",
                keyPath: \.videoRemainedVisible
            )

            observationCard(
                title: "Did the scene look three-dimensional rather than flat?",
                detail: "Answer Not sure if the clip does not contain an obvious foreground and background.",
                keyPath: \.appearedThreeDimensional
            )

            Button {
                model.finishGuidedValidation()
            } label: {
                Label("Finish Check", systemImage: "checkmark")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(!model.canFinishObservations)
        }
        .accessibilityIdentifier("playback-observations")
    }

    private var completedContent: some View {
        VStack(alignment: .leading, spacing: 18) {
            if let result = model.validationResult {
                Image(systemName: result.symbolName)
                    .font(.system(size: 46))
                    .foregroundStyle(resultColor(result))
                    .accessibilityHidden(true)

                Text(result.title)
                    .font(.title2.bold())
                    .accessibilityIdentifier("playback-check-result")

                Text(result.summary)
                    .foregroundStyle(.secondary)
            }

            checkList

            if let validationReportURL = model.validationReportURL {
                VStack(alignment: .leading, spacing: 6) {
                    Label("JSON report saved automatically", systemImage: "doc.badge.checkmark")
                        .font(.headline)
                        .foregroundStyle(.green)
                    Text(validationReportURL.lastPathComponent)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }

                ShareLink(
                    item: validationReportURL,
                    subject: Text("BD to AVP playback check report")
                ) {
                    Label("Share JSON Report", systemImage: "square.and.arrow.up")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .accessibilityIdentifier("share-playback-report")
            } else if let validationReportSaveError = model.validationReportSaveError {
                VStack(alignment: .leading, spacing: 10) {
                    Label(
                        "The JSON file could not be saved: \(validationReportSaveError)",
                        systemImage: "exclamationmark.triangle.fill"
                    )
                    .font(.callout)
                    .foregroundStyle(.red)

                    if !model.validationReportText.isEmpty {
                        ShareLink(
                            item: model.validationReportText,
                            subject: Text("BD to AVP playback check report")
                        ) {
                            Label("Share Report Text", systemImage: "square.and.arrow.up")
                        }
                        .buttonStyle(.bordered)
                    }
                }
            }

            Button("Run Again") {
                model.prepareForAnotherRun()
            }
            .buttonStyle(.bordered)
            .frame(maxWidth: .infinity)
        }
    }

    private var assuranceCard: some View {
        Label {
            Text("Nothing is uploaded, converted, published, or approved. A temporary local copy is removed when replaced or when the app next launches.")
        } icon: {
            Image(systemName: "hand.raised.fill")
                .foregroundStyle(.blue)
        }
        .font(.callout)
        .padding(14)
        .background(.blue.opacity(0.10), in: RoundedRectangle(cornerRadius: 14))
    }

    private var checkList: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(model.validationChecks) { check in
                checkRow(check)
            }
        }
    }

    private var technicalDetails: some View {
        DisclosureGroup("Technical details", isExpanded: $showsTechnicalDetails) {
            VStack(alignment: .leading, spacing: 14) {
                statusRow(title: "Stereo decode support", value: model.decodeSupportText)
                statusRow(title: "Player", value: model.playerItemStatusText)
                statusRow(title: "Rendering", value: model.renderingStatusText)
                statusRow(title: "Expected presentation", value: model.expectedPresentationText)
                statusRow(
                    title: "Mode requested during check",
                    value: model.isSpatialPresentationRequested ? "Stereo · Spatial · Portal" : "Contained screen preview"
                )
                statusRow(title: "Actual presentation", value: model.actualPresentationText)
                    .accessibilityIdentifier("actual-presentation-status")
                statusRow(title: "Duration", value: model.timeSummary)
                statusRow(title: "File size", value: formattedFileSize)
                statusRow(
                    title: "Movie fingerprint",
                    value: model.sourceSHA256.isEmpty ? "Not available" : String(model.sourceSHA256.prefix(16))
                )

                if !model.audioOptions.isEmpty {
                    Picker("Audio", selection: $model.selectedAudioID) {
                        ForEach(model.audioOptions) { option in
                            Text(option.name).tag(option.id)
                        }
                    }
                }

                if !model.subtitleOptions.isEmpty {
                    Picker("Subtitles", selection: $model.selectedSubtitleID) {
                        ForEach(model.subtitleOptions) { option in
                            Text(option.name).tag(option.id)
                        }
                    }
                }
            }
            .padding(.top, 12)
        }
        .accessibilityIdentifier("technical-details")
        .onChange(of: model.selectedAudioID) { _, identifier in
            model.selectAudio(identifier)
        }
        .onChange(of: model.selectedSubtitleID) { _, identifier in
            model.selectSubtitle(identifier)
        }
    }

    private var formattedFileSize: String {
        guard let sourceFileSizeBytes = model.sourceFileSizeBytes else {
            return "Unknown"
        }
        return ByteCountFormatter.string(fromByteCount: sourceFileSizeBytes, countStyle: .file)
    }

    private var headerSubtitle: String {
        if model.validationPhase == .running {
            return model.currentValidationStepText
        }
        return model.hasLoadedAsset ? model.assetName : "Guided Vision Pro validation"
    }

    private func instructionRow(number: Int, text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(String(number))
                .font(.caption.bold())
                .frame(width: 24, height: 24)
                .background(.tint, in: Circle())
                .foregroundStyle(.white)
            Text(text)
        }
    }

    private func observationCard(
        title: String,
        detail: String,
        keyPath: WritableKeyPath<PlaybackObservations, PlaybackObservationAnswer>
    ) -> some View {
        let selectedAnswer = model.observations[keyPath: keyPath]

        return VStack(alignment: .leading, spacing: 12) {
            Text(title)
                .font(.headline)
            Text(detail)
                .font(.caption)
                .foregroundStyle(.secondary)

            HStack(spacing: 8) {
                ForEach(
                    [PlaybackObservationAnswer.yes, .no, .unsure],
                    id: \.rawValue
                ) { answer in
                    Button(answer.label) {
                        model.setObservation(answer, for: keyPath)
                    }
                    .buttonStyle(.bordered)
                    .tint(selectedAnswer == answer ? observationColor(answer) : .secondary)
                    .frame(maxWidth: .infinity)
                }
            }
        }
        .padding(14)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
    }

    private func checkRow(_ check: PlaybackCheck) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: checkSymbol(check.status))
                .foregroundStyle(checkColor(check.status))
                .frame(width: 18)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 2) {
                Text(check.id.title)
                    .font(.callout.weight(.semibold))
                Text(check.detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func failureSection(_ failure: ProbeFailure) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(failure.title, systemImage: "exclamationmark.triangle.fill")
                .font(.headline)
                .foregroundStyle(.red)
            Text(failure.message)
                .font(.callout)
        }
        .padding(14)
        .background(.red.opacity(0.12), in: RoundedRectangle(cornerRadius: 14))
    }

    private func statusRow(title: String, value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value)
                .font(.caption.monospaced())
                .multilineTextAlignment(.trailing)
        }
        .accessibilityElement(children: .combine)
    }

    private func scalePlayerEntity(proxy: GeometryProxy3D, content: RealityViewContent) {
        guard let component = model.playerEntity.components[VideoPlayerComponent.self] else {
            return
        }

        let frame = proxy.frame(in: .local)
        let frameSize = abs(content.convert(frame.size, from: .local, to: .scene))
        let screenSize = component.playerScreenSize
        guard screenSize.x > 0, screenSize.y > 0 else {
            return
        }

        let scale = min(frameSize.x / screenSize.x, frameSize.y / screenSize.y) * Self.playerScaleInset
        guard scale.isFinite, scale > 0 else {
            return
        }
        model.playerEntity.scale = SIMD3<Float>(repeating: scale)
        model.playerEntity.position = SIMD3<Float>(0, 0, Self.playerDepthOffset)
    }

    private func checkSymbol(_ status: PlaybackCheckStatus) -> String {
        switch status {
        case .pending:
            return "circle"
        case .running:
            return "clock.fill"
        case .passed:
            return "checkmark.circle.fill"
        case .failed:
            return "xmark.octagon.fill"
        }
    }

    private func checkColor(_ status: PlaybackCheckStatus) -> Color {
        switch status {
        case .pending:
            return .secondary
        case .running:
            return .blue
        case .passed:
            return .green
        case .failed:
            return .red
        }
    }

    private func observationColor(_ answer: PlaybackObservationAnswer) -> Color {
        switch answer {
        case .yes:
            return .green
        case .no:
            return .red
        case .unsure, .unanswered:
            return .orange
        }
    }

    private func resultColor(_ result: PlaybackValidationResult) -> Color {
        switch result {
        case .passed:
            return .green
        case .needsReview:
            return .orange
        case .failed:
            return .red
        }
    }
}

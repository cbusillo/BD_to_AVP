import AVKit
import RealityKit
import SwiftUI

struct PlayerView: View {
    private static let playerScaleInset: Float = 0.86
    private static let playerDepthOffset: Float = -0.14

    @ObservedObject private var session: MVHEVCPlayerSession
    private let onRetry: () -> Void
    private let onLocate: () -> Void
    private let onDone: () -> Void

    @Environment(\.accessibilityVoiceOverEnabled) private var isVoiceOverEnabled
    @Environment(\.accessibilitySwitchControlEnabled) private var isSwitchControlEnabled
    @State private var hudVisibility = PlaybackHUDVisibilityState()

    init(
        session: MVHEVCPlayerSession,
        onRetry: @escaping () -> Void,
        onLocate: @escaping () -> Void,
        onDone: @escaping () -> Void
    ) {
        self.session = session
        self.onRetry = onRetry
        self.onLocate = onLocate
        self.onDone = onDone
    }

    var body: some View {
        Group {
            if isPackedStereoMedia {
                PackedStereoPlayerSurface(
                    session: session,
                    onRetry: onRetry,
                    onLocate: onLocate,
                    onDone: done
                )
            } else {
                GeometryReader3D { geometry in
                    playerSurface(geometry: geometry)
                }
            }
        }
        .background(.black)
        .overlay {
            if !isPackedStereoMedia {
                Button {
                    hudVisibility.reveal(isPlaying: isAutomaticHidingAllowed)
                } label: {
                    Color.clear
                        .contentShape(Rectangle())
                }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("player-surface")
                    .accessibilityLabel("Show playback controls")
                    .accessibilityHint("Reveals playback controls for three seconds.")
            }
        }
        .onAppear {
            hudVisibility.reconcile(isPlaying: isAutomaticHidingAllowed)
        }
        .onHover { isHovered in
            if isHovered {
                hudVisibility.reveal(isPlaying: isAutomaticHidingAllowed)
            }
        }
        .onChange(of: session.isPlaying) { _, isPlaying in
            hudVisibility.reconcile(
                isPlaying: PlaybackHUDVisibilityPolicy.allowsAutomaticHiding(
                    isPlaying: isPlaying,
                    isVoiceOverEnabled: isVoiceOverEnabled,
                    isSwitchControlEnabled: isSwitchControlEnabled
                )
            )
        }
        .onChange(of: session.state) { _, _ in
            hudVisibility.reconcile(isPlaying: isAutomaticHidingAllowed)
        }
        .onChange(of: isVoiceOverEnabled) { _, _ in
            hudVisibility.reconcile(isPlaying: isAutomaticHidingAllowed)
        }
        .onChange(of: isSwitchControlEnabled) { _, _ in
            hudVisibility.reconcile(isPlaying: isAutomaticHidingAllowed)
        }
        .task(id: hudVisibility.autoHideGeneration) {
            let generation = hudVisibility.autoHideGeneration
            guard hudVisibility.isAutoHideScheduled else {
                return
            }

            do {
                try await Task.sleep(nanoseconds: UInt64(PlaybackHUDVisibilityState.autoHideDelay * 1_000_000_000))
            } catch {
                return
            }

            guard !Task.isCancelled else {
                return
            }
            hudVisibility.autoHideTimerFired(generation: generation)
        }
        .ornament(
            visibility: .visible,
            attachmentAnchor: .scene(.bottom),
            contentAlignment: .topBack
        ) {
            if !isPackedStereoMedia {
                Group {
                    if hudVisibility.isVisible {
                        VStack(spacing: 0) {
                            Color.clear
                                .frame(height: 16)
                                .accessibilityHidden(true)
                            PlayerOrnamentView(
                                session: session,
                                onRetry: onRetry,
                                onLocate: onLocate,
                                onDone: done,
                                onHoverChanged: { isHovered in
                                    if isHovered {
                                        hudVisibility.hoverBegan(isPlaying: isAutomaticHidingAllowed)
                                    }
                                },
                                onScrubbingChanged: { isScrubbing in
                                    hudVisibility.setInteracting(isScrubbing, isPlaying: isAutomaticHidingAllowed)
                                },
                                onInteraction: {
                                    hudVisibility.reveal(isPlaying: isAutomaticHidingAllowed)
                                }
                            )
                        }
                    } else {
                        Button {
                            hudVisibility.reveal(isPlaying: isAutomaticHidingAllowed)
                        } label: {
                            Label("Show controls", systemImage: "chevron.up")
                                .frame(minWidth: 60, minHeight: 60)
                        }
                        .buttonStyle(.bordered)
                        .glassBackgroundEffect()
                        .accessibilityIdentifier("player-show-controls")
                        .accessibilityHint("Expands the playback controls.")
                    }
                }
                .animation(.easeInOut(duration: 0.2), value: hudVisibility.isVisible)
            }
        }
        .onDisappear {
            session.finish()
        }
    }

    @ViewBuilder
    private func playerSurface(geometry: GeometryProxy3D) -> some View {
        switch session.state {
        case .idle:
            Color.clear
        case .loading:
            Color.clear
        case .failed:
            Color.clear
        case .ready:
            RealityView { content in
                session.installPlayerComponent()
                content.add(session.playerEntity)
                fitPlayerEntity(proxy: geometry, content: content)
            } update: { content in
                fitPlayerEntity(proxy: geometry, content: content)
            }
        }
    }

    private func done() {
        session.finish()
        onDone()
    }

    private var isAutomaticHidingAllowed: Bool {
        PlaybackHUDVisibilityPolicy.allowsAutomaticHiding(
            isPlaying: session.isPlaying,
            isVoiceOverEnabled: isVoiceOverEnabled,
            isSwitchControlEnabled: isSwitchControlEnabled
        )
    }

    private var isPackedStereoMedia: Bool {
        session.mediaItem?.format == .sideBySide || session.mediaItem?.format == .overUnder
    }

    private func fitPlayerEntity(proxy: GeometryProxy3D, content: RealityViewContent) {
        guard let component = session.playerEntity.components[VideoPlayerComponent.self] else {
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
        session.playerEntity.scale = SIMD3<Float>(repeating: scale)
        session.playerEntity.position = SIMD3<Float>(0, 0, Self.playerDepthOffset)
    }
}

private struct PackedStereoPlayerSurface: UIViewControllerRepresentable {
    @ObservedObject var session: MVHEVCPlayerSession
    let onRetry: () -> Void
    let onLocate: () -> Void
    let onDone: () -> Void

    @MainActor
    final class Coordinator: NSObject {
        private struct ActionState: Equatable {
            let eyeOrder: PackedStereoEyeOrder
            let isChangingEyeOrder: Bool
            let canControlPlayback: Bool
            let showsEyeOrder: Bool
            let canRetry: Bool
            let canLocate: Bool
        }

        var session: MVHEVCPlayerSession
        var onRetry: () -> Void
        var onLocate: () -> Void
        var onDone: () -> Void
        weak var statusLabel: UILabel?
        private var actionState: ActionState?

        init(
            session: MVHEVCPlayerSession,
            onRetry: @escaping () -> Void,
            onLocate: @escaping () -> Void,
            onDone: @escaping () -> Void
        ) {
            self.session = session
            self.onRetry = onRetry
            self.onLocate = onLocate
            self.onDone = onDone
        }

        func installInfoView(in viewController: AVPlayerViewController) {
            let titleLabel = UILabel()
            titleLabel.text = session.mediaItem?.title ?? "Packed Stereo Check"
            titleLabel.font = .preferredFont(forTextStyle: .headline)
            titleLabel.adjustsFontForContentSizeCategory = true
            titleLabel.textColor = .label

            let statusLabel = UILabel()
            statusLabel.font = .preferredFont(forTextStyle: .subheadline)
            statusLabel.adjustsFontForContentSizeCategory = true
            statusLabel.textColor = .secondaryLabel
            statusLabel.numberOfLines = 2
            statusLabel.accessibilityIdentifier = "player-packed-stereo-status"
            self.statusLabel = statusLabel

            let labels = UIStackView(arrangedSubviews: [titleLabel, statusLabel])
            labels.axis = .vertical
            labels.spacing = 4
            labels.translatesAutoresizingMaskIntoConstraints = false
            let infoView = viewController.contextualActionsInfoView
            infoView.addSubview(labels)
            NSLayoutConstraint.activate([
                labels.leadingAnchor.constraint(equalTo: infoView.leadingAnchor),
                labels.trailingAnchor.constraint(equalTo: infoView.trailingAnchor),
                labels.topAnchor.constraint(equalTo: infoView.topAnchor),
                labels.bottomAnchor.constraint(equalTo: infoView.bottomAnchor),
            ])
        }

        func update(viewController: AVPlayerViewController) {
            statusLabel?.text = PackedStereoStatusPresentation.message(
                mediaItem: session.mediaItem,
                isReady: session.isReady,
                failureMessage: session.failureMessage,
                preparationPhase: session.preparationPhase
            )

            let newState = ActionState(
                eyeOrder: session.isEyeSwapped ? .reversed : .normal,
                isChangingEyeOrder: session.isChangingEyeOrder,
                canControlPlayback: session.canControlPlayback,
                showsEyeOrder: session.state != .failed || BuiltInStereoChecks.contains(session.mediaItem),
                canRetry: session.failurePresentation?.canRetry == true,
                canLocate: session.failurePresentation?.canLocate == true
            )
            guard newState != actionState else {
                return
            }
            actionState = newState

            let eyeOrderTitle: String
            if newState.isChangingEyeOrder {
                eyeOrderTitle = "Changing Eye Order…"
            } else {
                eyeOrderTitle = "Eye Order: \(newState.eyeOrder == .reversed ? "Reversed" : "Normal")"
            }
            let eyeOrderAction = UIAction(
                title: eyeOrderTitle,
                image: UIImage(systemName: "arrow.left.arrow.right"),
                attributes: newState.canControlPlayback ? [] : .disabled
            ) { [weak self] _ in
                self?.session.toggleEyeSwap()
            }
            let doneAction = UIAction(
                title: "Done",
                image: UIImage(systemName: "xmark")
            ) { [weak self] _ in
                self?.onDone()
            }
            var actions: [UIAction] = []
            if newState.showsEyeOrder {
                actions.append(eyeOrderAction)
            }
            if newState.canRetry {
                actions.append(
                    UIAction(title: "Try Again", image: UIImage(systemName: "arrow.clockwise")) { [weak self] _ in
                        self?.onRetry()
                    }
                )
            }
            if newState.canLocate {
                actions.append(
                    UIAction(title: "Locate", image: UIImage(systemName: "folder")) { [weak self] _ in
                        self?.onLocate()
                    }
                )
            }
            actions.append(doneAction)
            viewController.contextualActions = actions
        }
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(
            session: session,
            onRetry: onRetry,
            onLocate: onLocate,
            onDone: onDone
        )
    }

    func makeUIViewController(context: Context) -> AVPlayerViewController {
        let viewController = AVPlayerViewController()
        viewController.showsPlaybackControls = true
        viewController.allowsPictureInPicturePlayback = false
        viewController.player = session.player
        context.coordinator.installInfoView(in: viewController)
        context.coordinator.update(viewController: viewController)
        return viewController
    }

    func updateUIViewController(_ viewController: AVPlayerViewController, context: Context) {
        if viewController.player !== session.player {
            viewController.player = session.player
        }
        context.coordinator.session = session
        context.coordinator.onRetry = onRetry
        context.coordinator.onLocate = onLocate
        context.coordinator.onDone = onDone
        context.coordinator.update(viewController: viewController)
    }

    static func dismantleUIViewController(_ viewController: AVPlayerViewController, coordinator: Coordinator) {
        viewController.contextualActions = []
        viewController.contextualActionsInfoView.subviews.forEach { $0.removeFromSuperview() }
        viewController.player = nil
        coordinator.statusLabel = nil
    }
}

private struct PlayerOrnamentView: View {
    @ObservedObject var session: MVHEVCPlayerSession
    let onRetry: () -> Void
    let onLocate: () -> Void
    let onDone: () -> Void
    let onHoverChanged: (Bool) -> Void
    let onScrubbingChanged: (Bool) -> Void
    let onInteraction: () -> Void

    @State private var scrubState = PlaybackScrubState()

    var body: some View {
        Group {
            switch session.state {
            case .ready:
                readyControls
            case .loading:
                statusControls(
                    title: session.preparationPhase.title,
                    systemImage: "film.stack",
                    message: session.preparationPhase.message(for: session.mediaItem?.title),
                    showsProgress: true,
                    failure: nil
                )
            case .failed:
                let failure = session.failurePresentation
                statusControls(
                    title: failure?.title ?? "Movie Unavailable",
                    systemImage: "exclamationmark.triangle",
                    message: session.failureMessage ?? "The movie could not be prepared.",
                    showsProgress: false,
                    failure: failure
                )
            case .idle:
                statusControls(
                    title: "Preparing Playback",
                    systemImage: "film.stack",
                    message: "Opening \(session.mediaItem?.title ?? "your movie")…",
                    showsProgress: true,
                    failure: nil
                )
            }
        }
        .padding(.horizontal, 20)
        .padding(.top, 30)
        .padding(.bottom, 20)
        .frame(maxWidth: 980)
        .glassBackgroundEffect()
        .onHover(perform: onHoverChanged)
        .accessibilityElement(children: .contain)
    }

    private var readyControls: some View {
        VStack(spacing: 14) {
            controlBar

            if let warning = session.failureMessage {
                Label(warning, systemImage: "exclamationmark.triangle")
                    .font(.subheadline)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            VStack(spacing: 8) {
                Slider(
                    value: Binding(
                        get: { scrubState.value ?? session.currentTime },
                        set: {
                            scrubState.update(requestedTime: $0, duration: session.duration)
                            onInteraction()
                        }
                    ),
                    in: 0 ... max(1, session.duration),
                    onEditingChanged: handleScrubbing
                )
                .disabled(!session.canSeek)
                .accessibilityLabel("Playback position")
                .accessibilityValue(displayedTimeSummary)

                HStack {
                    Text(PlaybackTimeFormatter.string(for: scrubState.value ?? session.currentTime))
                    Spacer()
                    Text(PlaybackTimeFormatter.string(for: session.duration))
                }
                .font(.system(.subheadline, design: .monospaced))
                .foregroundStyle(.secondary)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Playback time \(displayedTimeSummary)")
            }

        }
    }

    private var controlBar: some View {
        HStack(spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text(session.mediaItem?.title ?? "BD to AVP Player")
                    .font(.headline)
                    .lineLimit(1)
                Label(session.mediaItem?.format.displayName ?? "MV-HEVC", systemImage: "view.3d")
                    .font(.subheadline)
                    .accessibilityLabel("Stereo format \(session.mediaItem?.format.displayName ?? "MV-HEVC")")
                if BuiltInStereoChecks.contains(session.mediaItem) {
                    Text("Cover one eye at a time")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(width: BuiltInStereoChecks.contains(session.mediaItem) ? 220 : 180, alignment: .leading)

            Spacer(minLength: 8)

            transportButton(
                title: "Back 10 seconds",
                systemImage: "gobackward.10",
                action: session.seekBackward
            )
            .disabled(!session.canSeek)

            Button(action: performPlaybackToggle) {
                Label(
                    session.isPlaying ? "Pause" : "Play",
                    systemImage: session.isPlaying ? "pause.fill" : "play.fill"
                )
                .labelStyle(.iconOnly)
                .frame(minWidth: 72, minHeight: 72)
            }
            .buttonStyle(.borderedProminent)
            .disabled(!session.canControlPlayback)
            .accessibilityIdentifier("player-play-pause")
            .accessibilityLabel(session.isPlaying ? "Pause playback" : "Play movie")

            transportButton(
                title: "Forward 30 seconds",
                systemImage: "goforward.30",
                action: session.seekForward
            )
            .disabled(!session.canSeek)

            Spacer(minLength: 8)

            if session.supportsEyeSwap {
                eyeSwapButton
            }
            audioMenu
            subtitleMenu
            doneButton
        }
    }

    private func statusControls(
        title: String,
        systemImage: String,
        message: String,
        showsProgress: Bool,
        failure: PlaybackFailurePresentation?
    ) -> some View {
        HStack(spacing: 16) {
            if showsProgress {
                ProgressView()
                    .controlSize(.large)
            } else {
                Image(systemName: systemImage)
                    .font(.title2)
            }

            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .font(.headline)
                Text(message)
                    .font(.subheadline)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Spacer(minLength: 20)
            if failure?.canRetry == true {
                recoveryButton(
                    title: "Try Again",
                    systemImage: "arrow.clockwise",
                    identifier: "player-retry",
                    action: onRetry
                )
            }
            if failure?.canLocate == true {
                recoveryButton(
                    title: "Locate",
                    systemImage: "folder",
                    identifier: "player-locate",
                    action: onLocate
                )
            }
            doneButton
        }
        .frame(maxWidth: 580, alignment: .leading)
    }

    private func recoveryButton(
        title: String,
        systemImage: String,
        identifier: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .labelStyle(.iconOnly)
                .frame(minWidth: 60, minHeight: 60)
        }
        .buttonStyle(.bordered)
        .accessibilityIdentifier(identifier)
        .accessibilityLabel(title)
    }

    private var doneButton: some View {
        Button(action: onDone) {
            Label("Done", systemImage: "xmark")
                .labelStyle(.iconOnly)
                .frame(minWidth: 60, minHeight: 60)
        }
            .accessibilityIdentifier("player-done")
            .accessibilityLabel("Done playing movie")
    }

    private func transportButton(title: String, systemImage: String, action: @escaping () -> Void) -> some View {
        Button {
            onInteraction()
            action()
        } label: {
            Label(title, systemImage: systemImage)
                .labelStyle(.iconOnly)
                .frame(minWidth: 60, minHeight: 60)
        }
        .buttonStyle(.bordered)
        .accessibilityLabel(title)
    }

    private func performPlaybackToggle() {
        onInteraction()
        session.togglePlayback()
    }

    private var eyeSwapButton: some View {
        let presentation = PlaybackEyeOrderPresentation.value(isEyeSwapped: session.isEyeSwapped)
        return Button {
            onInteraction()
            session.toggleEyeSwap()
        } label: {
            VStack(spacing: 2) {
                if session.isChangingEyeOrder {
                    ProgressView()
                } else {
                    Image(systemName: presentation.systemImage)
                }
                Text("Eye Order")
                    .font(.caption2.weight(.semibold))
                Text(presentation.title)
                    .font(.caption2)
            }
            .frame(minWidth: 92, minHeight: 60)
        }
        .buttonStyle(.bordered)
        .disabled(session.isChangingEyeOrder || !session.canControlPlayback)
        .accessibilityIdentifier("player-eye-swap")
        .accessibilityLabel("Eye order")
        .accessibilityValue(presentation.title)
        .accessibilityAddTraits(presentation.isSelected ? .isSelected : [])
    }

    private func handleScrubbing(_ isEditing: Bool) {
        onScrubbingChanged(isEditing)
        if isEditing {
            scrubState.begin(currentTime: session.currentTime)
        } else if let scrubbedTime = scrubState.finish() {
            session.seek(to: scrubbedTime)
        }
    }

    private var displayedTimeSummary: String {
        let displayedTime = scrubState.value ?? session.currentTime
        return "\(PlaybackTimeFormatter.string(for: displayedTime)) / \(PlaybackTimeFormatter.string(for: session.duration))"
    }

    private var audioMenu: some View {
        Menu {
            ForEach(session.audioOptions) { option in
                Button {
                    onInteraction()
                    session.selectAudio(id: option.id)
                } label: {
                    if session.selectedAudioID == option.id {
                        Label(option.displayName, systemImage: "checkmark")
                            .accessibilityAddTraits(.isSelected)
                    } else {
                        Text(option.displayName)
                    }
                }
            }
        } label: {
            Label("Audio", systemImage: "speaker.wave.2")
                .labelStyle(.iconOnly)
                .frame(minWidth: 60, minHeight: 60)
        }
        .disabled(session.audioOptions.isEmpty)
        .accessibilityLabel("Audio track")
        .accessibilityValue(
            session.audioOptions.first { $0.id == session.selectedAudioID }?.displayName ?? "None selected"
        )
    }

    private var subtitleMenu: some View {
        Menu {
            ForEach(session.subtitleOptions) { option in
                Button(option.displayName) {
                    onInteraction()
                    session.selectSubtitle(id: option.id)
                }
            }
        } label: {
            Label("CC", systemImage: "captions.bubble")
                .labelStyle(.iconOnly)
                .frame(minWidth: 60, minHeight: 60)
        }
        .disabled(session.subtitleOptions.isEmpty)
        .accessibilityLabel("Closed captions")
    }
}

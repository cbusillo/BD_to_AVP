import AVKit
import RealityKit
import SwiftUI

struct PlayerView: View {
    private static let playerScaleInset: Float = 0.86
    private static let unsizedVideoScale: Float = 0.001
    private static let playerDepthOffset: Float = -0.14
    private static let trackListAttachmentID = "player-track-list"
    /// Meters between the bottom edge of the window and the open track list, so the list sits
    /// just above the controls below the window.
    private static let trackListBottomInset: Float = 0.015
    /// Meters the open track list sits in front of the video.
    private static let trackListDepthAboveVideo: Float = 0.02

    @ObservedObject private var session: MVHEVCPlayerSession
    private let onRetry: () -> Void
    private let onLocate: () -> Void
    private let onDone: () -> Void

    @Environment(\.accessibilityVoiceOverEnabled) private var isVoiceOverEnabled
    @Environment(\.accessibilitySwitchControlEnabled) private var isSwitchControlEnabled
    @State private var hudVisibility = PlaybackHUDVisibilityState()
    @State private var trackPicker = PlaybackTrackPickerState()
    /// The video's screen size from RealityKit's size event, before `playerScreenSize` has it.
    @State private var reportedVideoSize: SIMD2<Float>?

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
                // No backing behind the video: where the window and video shapes differ, the
                // room shows through instead of a black frame around the picture (#1132).
                GeometryReader3D { geometry in
                    playerSurface(geometry: geometry)
                }
            }
        }
        .overlay {
            // While a track list is open it sits over the video, so the surface steps aside
            // rather than catching the pinches meant for the list.
            if !isPackedStereoMedia, !trackPicker.isOpen {
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
        .onChange(of: session.isPlaying) { _, _ in
            hudVisibility.reconcile(isPlaying: isAutomaticHidingAllowed)
        }
        .onChange(of: session.isBuffering) { _, _ in
            hudVisibility.reconcile(isPlaying: isAutomaticHidingAllowed)
        }
        .onChange(of: session.isConnectionLost) { _, _ in
            hudVisibility.reconcile(isPlaying: isAutomaticHidingAllowed)
        }
        .onChange(of: session.state) { _, state in
            if state != .ready {
                trackPicker.close()
                reportedVideoSize = nil
            }
            hudVisibility.reconcile(isPlaying: isAutomaticHidingAllowed)
        }
        .onChange(of: trackPicker.isOpen) { _, isOpen in
            hudVisibility.setHold(.trackPicker, isActive: isOpen, isPlaying: isAutomaticHidingAllowed)
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
                VStack(spacing: 0) {
                    Color.clear
                        .frame(height: 16)
                        .accessibilityHidden(true)
                    PlayerOrnamentView(
                        session: session,
                        isFaded: hudVisibility.isFaded,
                        trackPicker: $trackPicker,
                        onRetry: onRetry,
                        onLocate: onLocate,
                        onDone: done,
                        onHoldChanged: { hold, isActive in
                            if hold == .hover, isActive {
                                hudVisibility.hoverBegan(isPlaying: isAutomaticHidingAllowed)
                            } else {
                                hudVisibility.setHold(hold, isActive: isActive, isPlaying: isAutomaticHidingAllowed)
                            }
                        },
                        onInteraction: {
                            hudVisibility.reveal(isPlaying: isAutomaticHidingAllowed)
                        }
                    )
                }
            }
        }
        .onDisappear {
            trackPicker.close()
            hudVisibility.setHold(.trackPicker, isActive: false, isPlaying: isAutomaticHidingAllowed)
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
            let reportedSize = reportedVideoSize
            RealityView { content, attachments in
                session.installPlayerComponent()
                content.add(session.playerEntity)
                _ = content.subscribe(to: VideoPlayerEvents.VideoSizeDidChange.self, on: session.playerEntity) { event in
                    reportedVideoSize = event.screenMeshSize
                }
                fitPlayerEntity(proxy: geometry, content: content, reportedSize: reportedSize)
                placeTrackList(proxy: geometry, content: content, attachments: attachments)
            } update: { content, attachments in
                fitPlayerEntity(proxy: geometry, content: content, reportedSize: reportedSize)
                placeTrackList(proxy: geometry, content: content, attachments: attachments)
            } attachments: {
                if let picker = trackPicker.openPicker {
                    Attachment(id: Self.trackListAttachmentID) {
                        PlayerTrackListView(
                            session: session,
                            picker: picker,
                            onSelect: {
                                hudVisibility.reveal(isPlaying: isAutomaticHidingAllowed)
                                trackPicker.close()
                            },
                            onClose: { trackPicker.close() }
                        )
                    }
                }
            }
        }
    }

    private func done() {
        session.finish()
        onDone()
    }

    /// While the movie loads more or its share is unreachable, the controls stay up so the
    /// wearer sees why.
    private var isAutomaticHidingAllowed: Bool {
        PlaybackHUDVisibilityPolicy.allowsAutomaticHiding(
            isPlaying: session.isPlaying && !session.isBuffering && !session.isConnectionLost,
            isVoiceOverEnabled: isVoiceOverEnabled,
            isSwitchControlEnabled: isSwitchControlEnabled
        )
    }

    private var isPackedStereoMedia: Bool {
        session.mediaItem?.format == .sideBySide || session.mediaItem?.format == .overUnder
    }

    /// The video's screen size in its entity's space, or nil before RealityKit knows it.
    private func videoScreenSize(reportedSize: SIMD2<Float>?) -> SIMD2<Float>? {
        guard let component = session.playerEntity.components[VideoPlayerComponent.self] else {
            return nil
        }
        let screenSize = component.playerScreenSize
        if screenSize.x > 0, screenSize.y > 0 {
            return screenSize
        }
        guard let reportedSize, reportedSize.x > 0, reportedSize.y > 0 else {
            return nil
        }
        return reportedSize
    }

    private func fitPlayerEntity(proxy: GeometryProxy3D, content: RealityViewContent, reportedSize: SIMD2<Float>?) {
        guard session.playerEntity.components[VideoPlayerComponent.self] != nil else {
            return
        }

        let frameSize = windowSize(proxy: proxy, content: content)
        // Until the video's size is known, keep it too small to cover anything: at its natural
        // size a stereo video can draw over the controls ornament (#1103).
        guard let screenSize = videoScreenSize(reportedSize: reportedSize) else {
            session.playerEntity.scale = SIMD3<Float>(repeating: Self.unsizedVideoScale)
            session.playerEntity.position = SIMD3<Float>(0, 0, Self.playerDepthOffset)
            return
        }

        let scale = min(frameSize.x / screenSize.x, frameSize.y / screenSize.y) * Self.playerScaleInset
        guard scale.isFinite, scale > 0 else {
            return
        }
        session.playerEntity.scale = SIMD3<Float>(repeating: scale)
        session.playerEntity.position = SIMD3<Float>(0, 0, Self.playerDepthOffset)
#if BD_TO_AVP_QUALIFICATION
        PlayerLayoutProbe.windowSize = SIMD2(frameSize.x, frameSize.y)
        PlayerLayoutProbe.videoSize = screenSize * scale
#endif
    }

    /// The window's size in the RealityView's scene space, whose origin is the window's center.
    private func windowSize(proxy: GeometryProxy3D, content: RealityViewContent) -> SIMD3<Float> {
        abs(content.convert(proxy.frame(in: .local).size, from: .local, to: .scene))
    }

    /// Places the open track list at the bottom of the window, just in front of the video, so it
    /// reads as part of the controls panel directly below. Growing the controls ornament pushed the lists
    /// below the window, a second ornament or a SwiftUI view in the window is hidden behind the
    /// window's surface or floats out in front of the controls, and system menus presented from
    /// an ornament open behind the video. A RealityView attachment shares the video's space.
    private func placeTrackList(proxy: GeometryProxy3D, content: RealityViewContent, attachments: RealityViewAttachments) {
        let placed = content.entities.first { $0.name == Self.trackListAttachmentID }
        guard let list = attachments.entity(for: Self.trackListAttachmentID) else {
            placed?.removeFromParent()
            return
        }
        if placed !== list {
            placed?.removeFromParent()
            list.name = Self.trackListAttachmentID
            content.add(list)
        }

        let windowBottom = -windowSize(proxy: proxy, content: content).y / 2
        let listHeight = list.visualBounds(relativeTo: list.parent).extents.y
        list.position = SIMD3<Float>(
            0,
            windowBottom + Self.trackListBottomInset + listHeight / 2,
            session.playerEntity.position.z + Self.trackListDepthAboveVideo
        )
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
    private static let cornerRadius: CGFloat = 40

    @ObservedObject var session: MVHEVCPlayerSession
    /// Faded controls stay mounted and interactive; looking at them brings them back.
    let isFaded: Bool
    @Binding var trackPicker: PlaybackTrackPickerState
    let onRetry: () -> Void
    let onLocate: () -> Void
    let onDone: () -> Void
    let onHoldChanged: (PlaybackHUDVisibilityState.Hold, Bool) -> Void
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
        .glassBackgroundEffect(in: RoundedRectangle(cornerRadius: Self.cornerRadius))
        .contentShape(.hoverEffect, RoundedRectangle(cornerRadius: Self.cornerRadius))
        .hoverEffect { [isFaded] effect, isActive, _ in
            let transition = PlaybackHUDPresentation.lookTransition(isLookedAt: isActive)
            return effect.animation(.easeInOut(duration: transition.duration).delay(transition.delay)) {
                $0.opacity(PlaybackHUDPresentation.opacity(isFaded: isFaded, isLookedAt: isActive))
            }
        }
        .hoverEffectGroup()
        .onHover { isHovered in
            onHoldChanged(.hover, isHovered)
        }
        .onDisappear {
            onHoldChanged(.hover, false)
        }
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

            if let warning = session.audioWarning {
                Label(warning, systemImage: "speaker.slash")
                    .font(.subheadline)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("player-audio-warning")
            }

            if let loading = PlaybackLoadingPresentation.state(
                isConnectionLost: session.isConnectionLost,
                isBuffering: session.isBuffering
            ) {
                HStack(spacing: 10) {
                    if loading == .loading {
                        ProgressView()
                    } else {
                        Image(systemName: "wifi.exclamationmark")
                            .accessibilityHidden(true)
                    }
                    Text(loading.message)
                        .font(.subheadline)
                        .foregroundStyle(loading == .loading ? .secondary : .primary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityElement(children: .combine)
                .accessibilityIdentifier(loading == .loading ? "player-buffering" : "player-connection-lost")
            }

            VStack(spacing: 8) {
                PlaybackScrubBar(
                    timeline: session.timeline,
                    position: displayedTime,
                    isEnabled: session.canSeek,
                    accessibilityValue: displayedTimeSummary,
                    onScrubbingChanged: handleScrubbing,
                    onScrub: { requestedTime in
                        scrubState.update(requestedTime: requestedTime, duration: session.timeline.duration)
                        onInteraction()
                    },
                    onAdjust: { step in
                        onInteraction()
                        session.seek(to: session.currentTime + step)
                    }
                )

                HStack {
                    Text(PlaybackTimeFormatter.string(for: displayedTime))
                    Spacer()
                    Text(PlaybackTimeFormatter.string(for: session.timeline.duration))
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
                Text(session.mediaItem?.title ?? "Shiny 3D Cinema")
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
        onHoldChanged(.scrubbing, isEditing)
        if isEditing {
            scrubState.begin(currentTime: session.currentTime)
        } else if let scrubbedTime = scrubState.finish() {
            session.seek(to: scrubbedTime)
        }
    }

    private var displayedTime: TimeInterval {
        scrubState.value ?? session.currentTime
    }

    private var displayedTimeSummary: String {
        let timeline = session.timeline
        let summary = "\(PlaybackTimeFormatter.string(for: displayedTime)) / \(PlaybackTimeFormatter.string(for: timeline.duration))"
        guard timeline.loadedEnd < timeline.duration else {
            return summary
        }
        return "\(summary), loaded through \(PlaybackTimeFormatter.string(for: timeline.loadedEnd))"
    }

    // These buttons open app-owned track lists rather than system menus, because a menu
    // presented from the ornament opens behind the video.
    private var audioMenu: some View {
        trackPickerButton(
            .audio,
            title: "Audio language",
            systemImage: "character.bubble",
            identifier: "player-audio-menu",
            isEnabled: session.isReady,
            selectedName: session.audioOptions.first { $0.id == session.selectedAudioID }?.displayName
        )
    }

    private var subtitleMenu: some View {
        trackPickerButton(
            .subtitles,
            title: "Subtitles",
            systemImage: "captions.bubble",
            identifier: "player-subtitle-menu",
            isEnabled: session.isReady,
            selectedName: session.subtitleOptions.first { $0.id == session.selectedSubtitleID }?.displayName
        )
    }

    private func trackPickerButton(
        _ picker: PlaybackTrackPicker,
        title: String,
        systemImage: String,
        identifier: String,
        isEnabled: Bool,
        selectedName: String?
    ) -> some View {
        let isOpen = trackPicker.openPicker == picker
        return Button {
            onInteraction()
            trackPicker.toggle(picker)
        } label: {
            Label(title, systemImage: systemImage)
                .labelStyle(.iconOnly)
                .frame(minWidth: 60, minHeight: 60)
        }
        .buttonStyle(.bordered)
        .tint(isOpen ? .accentColor : nil)
        .disabled(!isEnabled)
        .accessibilityIdentifier(identifier)
        .accessibilityLabel(title)
        .accessibilityValue(selectedName ?? "None selected")
        .accessibilityAddTraits(isOpen ? .isSelected : [])
    }
}

/// The audio or subtitle list, shown over the bottom of the video directly above the controls.
/// A long list stops at a fixed height and scrolls, starting at the current selection.
private struct PlayerTrackListView: View {
    private static let cornerRadius: CGFloat = 28
    private static let width: CGFloat = 420

    @ObservedObject var session: MVHEVCPlayerSession
    let picker: PlaybackTrackPicker
    let onSelect: () -> Void
    let onClose: () -> Void

    var body: some View {
        switch picker {
        case .audio:
            trackList(
                title: "Audio Language",
                identifier: "player-audio-tracks",
                note: PlaybackTrackListNote.note(for: .audio, optionCount: session.audioOptions.count),
                options: session.audioOptions,
                selectedID: session.selectedAudioID,
                select: { session.selectAudio(id: $0) }
            )
        case .subtitles:
            trackList(
                title: "Subtitles",
                identifier: "player-subtitle-tracks",
                note: session.subtitleListNote
                    ?? PlaybackTrackListNote.note(for: .subtitles, optionCount: session.subtitleOptions.count),
                options: session.subtitleOptions,
                selectedID: session.selectedSubtitleID,
                select: { session.selectSubtitle(id: $0) }
            )
        }
    }

    private func trackList(
        title: String,
        identifier: String,
        note: String?,
        options: [PlaybackMediaOption],
        selectedID: String,
        select: @escaping (String) -> Void
    ) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(title)
                    .font(.headline)
                Spacer()
                Button(action: onClose) {
                    Label("Close \(title)", systemImage: "xmark")
                        .labelStyle(.iconOnly)
                }
                .buttonBorderShape(.circle)
                .accessibilityIdentifier("\(identifier)-close")
            }

            ScrollViewReader { proxy in
                ScrollView {
                    VStack(spacing: 0) {
                        ForEach(options) { option in
                            trackRow(option, isSelected: option.id == selectedID) {
                                select(option.id)
                                onSelect()
                            }
                            .id(option.id)
                        }
                    }
                }
                .scrollIndicators(PlaybackTrackListLayout.scrolls(rowCount: options.count) ? .visible : .hidden)
                .frame(height: PlaybackTrackListLayout.listHeight(rowCount: options.count))
                .onAppear {
                    proxy.scrollTo(selectedID, anchor: .center)
                }
            }

            if let note {
                Text(note)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier("\(identifier)-note")
            }
        }
        .padding(16)
        .frame(width: Self.width)
        .glassBackgroundEffect(in: RoundedRectangle(cornerRadius: Self.cornerRadius))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier(identifier)
    }

    private func trackRow(
        _ option: PlaybackMediaOption,
        isSelected: Bool,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack {
                Text(option.displayName)
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if isSelected {
                    Image(systemName: "checkmark")
                        .accessibilityHidden(true)
                }
            }
            .padding(.horizontal, 14)
            .frame(height: PlaybackTrackListLayout.rowHeight - 4)
            .contentShape(.hoverEffect, RoundedRectangle(cornerRadius: 12))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .hoverEffect(.highlight)
        .padding(.vertical, 2)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}

/// A streaming-style scrub bar: the track spans the title's full length, a lighter band shows
/// the range that can be played right now, and the filled part shows the playhead.
private struct PlaybackScrubBar: View {
    private static let trackHeight = PlaybackScrubBarLayout.trackHeight
    private static let thumbSize = PlaybackScrubBarLayout.thumbSize
    private static let adjustmentStep: TimeInterval = 10

    let timeline: PlaybackTimeline
    let position: TimeInterval
    let isEnabled: Bool
    let accessibilityValue: String
    let onScrubbingChanged: (Bool) -> Void
    let onScrub: (TimeInterval) -> Void
    let onAdjust: (TimeInterval) -> Void

    @State private var isScrubbing = false

    var body: some View {
        GeometryReader { geometry in
            let width = max(1, geometry.size.width)
            let playheadX = CGFloat(timeline.fraction(of: position)) * width
            let loadedX = CGFloat(timeline.loadedStartFraction) * width
            let loadedWidth = CGFloat(timeline.loadedEndFraction - timeline.loadedStartFraction) * width

            ZStack(alignment: .leading) {
                Capsule()
                    .fill(.quaternary)
                    .frame(height: Self.trackHeight)
                Capsule()
                    .fill(.secondary)
                    .frame(width: loadedWidth, height: Self.trackHeight)
                    .offset(x: loadedX)
                Capsule()
                    .fill(.tint)
                    .frame(width: playheadX, height: Self.trackHeight)
                Circle()
                    .fill(.white)
                    .frame(width: Self.thumbSize, height: Self.thumbSize)
                    .offset(x: playheadX - Self.thumbSize / 2)
                    .opacity(isEnabled ? 1 : 0)
            }
            .frame(width: width, height: geometry.size.height)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        if !isScrubbing {
                            isScrubbing = true
                            onScrubbingChanged(true)
                        }
                        onScrub(timeline.time(atFraction: Double(value.location.x / width)))
                    }
                    .onEnded { _ in
                        isScrubbing = false
                        onScrubbingChanged(false)
                    }
            )
        }
        .frame(minWidth: 600, maxWidth: .infinity)
        .frame(height: PlaybackScrubBarLayout.hitHeight)
        // The whole frame takes pinches, but the look highlight hugs the visible track.
        .contentShape(.hoverEffect, ScrubBarHighlightShape())
        .hoverEffect(.highlight)
        .allowsHitTesting(isEnabled)
        .opacity(isEnabled ? 1 : 0.5)
        .accessibilityElement()
        .accessibilityIdentifier("player-scrub-bar")
        .accessibilityLabel("Playback position")
        .accessibilityValue(accessibilityValue)
        .accessibilityAdjustableAction { direction in
            guard isEnabled else { return }
            switch direction {
            case .increment:
                onAdjust(Self.adjustmentStep)
            case .decrement:
                onAdjust(-Self.adjustmentStep)
            @unknown default:
                break
            }
        }
    }
}

/// A capsule around the visible scrub track, centered in the bar's taller hit area.
private struct ScrubBarHighlightShape: Shape {
    func path(in rect: CGRect) -> Path {
        Capsule().path(in: PlaybackScrubBarLayout.highlightRect(in: rect))
    }
}

#if BD_TO_AVP_QUALIFICATION
/// The last fitted layout in scene meters, for the on-device qualification report.
@MainActor
enum PlayerLayoutProbe {
    static var windowSize: SIMD2<Float>?
    static var videoSize: SIMD2<Float>?
}
#endif

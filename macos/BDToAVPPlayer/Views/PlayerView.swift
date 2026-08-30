import RealityKit
import SwiftUI

struct PlayerView: View {
    private static let playerScaleInset: Float = 0.86
    private static let playerDepthOffset: Float = -0.14

    @ObservedObject private var session: MVHEVCPlayerSession
    private let onDone: () -> Void

    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityVoiceOverEnabled) private var isVoiceOverEnabled
    @Environment(\.accessibilitySwitchControlEnabled) private var isSwitchControlEnabled
    @State private var hudVisibility = PlaybackHUDVisibilityState()

    init(session: MVHEVCPlayerSession, onDone: @escaping () -> Void) {
        self.session = session
        self.onDone = onDone
    }

    var body: some View {
        GeometryReader3D { geometry in
            playerSurface(geometry: geometry)
        }
        .background(.black)
        .overlay {
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
                isPlaying: isPlaying && !isVoiceOverEnabled && !isSwitchControlEnabled
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
            visibility: hudVisibility.isVisible ? .visible : .hidden,
            attachmentAnchor: .scene(.bottom),
            contentAlignment: .topBack
        ) {
            VStack(spacing: 0) {
                Color.clear
                    .frame(height: 16)
                    .accessibilityHidden(true)
                PlayerOrnamentView(
                    session: session,
                    onDone: done,
                    onHoverChanged: { isHovered in
                        hudVisibility.setHovered(isHovered, isPlaying: isAutomaticHidingAllowed)
                    },
                    onScrubbingChanged: { isScrubbing in
                        hudVisibility.setInteracting(isScrubbing, isPlaying: isAutomaticHidingAllowed)
                    },
                    onInteraction: {
                        hudVisibility.reveal(isPlaying: isAutomaticHidingAllowed)
                    }
                )
            }
        }
        .onChange(of: scenePhase) { _, newPhase in
            if newPhase != .active {
                session.applicationBecameInactive()
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
        session.isPlaying && !isVoiceOverEnabled && !isSwitchControlEnabled
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

private struct PlayerOrnamentView: View {
    @ObservedObject var session: MVHEVCPlayerSession
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
                    title: "Preparing Playback",
                    systemImage: "film.stack",
                    message: "Preparing \(session.mediaItem?.title ?? "your movie")…",
                    showsProgress: true
                )
            case .failed:
                statusControls(
                    title: "Movie Unavailable",
                    systemImage: "exclamationmark.triangle",
                    message: session.failureMessage ?? "The movie could not be prepared.",
                    showsProgress: false
                )
            case .idle:
                statusControls(
                    title: "Preparing Playback",
                    systemImage: "film.stack",
                    message: "Opening \(session.mediaItem?.title ?? "your movie")…",
                    showsProgress: true
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
            }
            .frame(width: 180, alignment: .leading)

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

            audioMenu
            subtitleMenu
            doneButton
        }
    }

    private func statusControls(title: String, systemImage: String, message: String, showsProgress: Bool) -> some View {
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
            doneButton
        }
        .frame(maxWidth: 580, alignment: .leading)
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
                Button(option.displayName) {
                    onInteraction()
                    session.selectAudio(id: option.id)
                }
            }
        } label: {
            Label("Audio", systemImage: "speaker.wave.2")
                .labelStyle(.iconOnly)
                .frame(minWidth: 60, minHeight: 60)
        }
        .disabled(session.audioOptions.isEmpty)
        .accessibilityLabel("Audio track")
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

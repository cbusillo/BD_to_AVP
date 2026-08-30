import RealityKit
import SwiftUI

struct PlayerView: View {
    @ObservedObject private var session: MVHEVCPlayerSession
    private let onDone: () -> Void

    @Environment(\.scenePhase) private var scenePhase
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
        .onAppear {
            hudVisibility.reconcile(isPlaying: session.isPlaying)
        }
        .onHover { isHovered in
            if isHovered {
                hudVisibility.reveal(isPlaying: session.isPlaying)
            }
        }
        .onChange(of: session.isPlaying) { _, isPlaying in
            hudVisibility.reconcile(isPlaying: isPlaying)
        }
        .onChange(of: session.state) { _, _ in
            hudVisibility.reconcile(isPlaying: session.isPlaying)
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
            attachmentAnchor: .scene(.bottom)
        ) {
            PlayerOrnamentView(
                session: session,
                onDone: done,
                onHoverChanged: { isHovered in
                    hudVisibility.setHovered(isHovered, isPlaying: session.isPlaying)
                },
                onScrubbingChanged: { isScrubbing in
                    hudVisibility.setInteracting(isScrubbing, isPlaying: session.isPlaying)
                },
                onInteraction: {
                    hudVisibility.reveal(isPlaying: session.isPlaying)
                }
            )
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

        let scale = min(frameSize.x / screenSize.x, frameSize.y / screenSize.y) * 0.94
        guard scale.isFinite, scale > 0 else {
            return
        }
        session.playerEntity.scale = SIMD3<Float>(repeating: scale)
        session.playerEntity.position = SIMD3<Float>(0, 0, -0.03)
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
                    title: "No Movie Selected",
                    systemImage: "film.stack",
                    message: "Choose an MV-HEVC movie to start spatial playback.",
                    showsProgress: false
                )
            }
        }
        .padding(20)
        .frame(maxWidth: 880)
        .glassBackgroundEffect()
        .onHover(perform: onHoverChanged)
        .accessibilityElement(children: .contain)
    }

    private var readyControls: some View {
        VStack(spacing: 16) {
            ornamentHeader

            if let warning = session.failureMessage {
                Label(warning, systemImage: "exclamationmark.triangle")
                    .font(.subheadline)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            HStack(spacing: 28) {
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
            }
            .frame(maxWidth: .infinity)

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
                .accessibilityLabel("Playback time \(displayedTimeSummary)")
            }

            HStack(spacing: 16) {
                audioMenu
                subtitleMenu
                Spacer(minLength: 0)
            }
        }
    }

    private var ornamentHeader: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(session.mediaItem?.title ?? "BD to AVP Player")
                    .font(.headline)
                    .lineLimit(1)
                Label(session.mediaItem?.format.displayName ?? "MV-HEVC", systemImage: "view.3d")
                    .font(.subheadline)
                    .accessibilityLabel("Stereo format \(session.mediaItem?.format.displayName ?? "MV-HEVC")")
            }
            Spacer(minLength: 20)
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
        Button("Done", action: onDone)
            .frame(minWidth: 60, minHeight: 60)
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
                .frame(minWidth: 60, minHeight: 60)
        }
        .disabled(session.subtitleOptions.isEmpty)
        .accessibilityLabel("Closed captions")
    }
}

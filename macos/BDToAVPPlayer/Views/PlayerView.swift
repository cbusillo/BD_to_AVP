import RealityKit
import SwiftUI

struct PlayerView: View {
    @ObservedObject private var session: MVHEVCPlayerSession
    private let onDone: () -> Void

    @Environment(\.scenePhase) private var scenePhase

    init(session: MVHEVCPlayerSession, onDone: @escaping () -> Void) {
        self.session = session
        self.onDone = onDone
    }

    var body: some View {
        GeometryReader3D { geometry in
            ZStack(alignment: .bottom) {
                playerSurface(geometry: geometry)

                PlayerHUDView(session: session, onDone: done)
                    .padding(24)
            }
        }
        .background(.black)
        .onChange(of: scenePhase) { _, newPhase in
            if newPhase == .background {
                session.applicationDidEnterBackground()
            }
        }
    }

    @ViewBuilder
    private func playerSurface(geometry: GeometryProxy3D) -> some View {
        switch session.state {
        case .idle:
            ContentUnavailableView(
                "No Movie Selected",
                systemImage: "film.stack",
                description: Text("Choose an MV-HEVC movie to start spatial playback.")
            )
            .foregroundStyle(.white)
        case .loading:
            ProgressView("Preparing MV-HEVC playback…")
                .padding(24)
                .glassBackgroundEffect()
                .foregroundStyle(.white)
        case .failed:
            ContentUnavailableView(
                "Movie Unavailable",
                systemImage: "exclamationmark.triangle",
                description: Text(session.failureMessage ?? "The movie could not be prepared.")
            )
            .foregroundStyle(.white)
        case .ready:
            RealityView { content in
                session.installPlayerComponent()
                if session.playerEntity.parent == nil {
                    content.add(session.playerEntity)
                }
                fitPlayerEntity(proxy: geometry, content: content)
                session.refreshRenderingReadiness()
            } update: { content in
                fitPlayerEntity(proxy: geometry, content: content)
                session.refreshRenderingReadiness()
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

private struct PlayerHUDView: View {
    @ObservedObject var session: MVHEVCPlayerSession
    let onDone: () -> Void

    var body: some View {
        VStack(spacing: 14) {
            HStack(spacing: 12) {
                Text(session.mediaItem?.title ?? "BD to AVP Player")
                    .font(.headline)
                    .lineLimit(1)
                Spacer()
                Label(session.mediaItem?.format.displayName ?? "MV-HEVC", systemImage: "view.3d")
                    .font(.caption.weight(.semibold))
                    .accessibilityLabel("Stereo format \(session.mediaItem?.format.displayName ?? "MV-HEVC")")
                Button("Done", action: onDone)
                    .accessibilityLabel("Done playing movie")
            }

            Slider(
                value: Binding(
                    get: { session.currentTime },
                    set: { session.seek(to: $0) }
                ),
                in: 0 ... max(1, session.duration)
            )
            .disabled(!session.canSeek)
            .accessibilityLabel("Playback position")
            .accessibilityValue(session.timeSummary)

            HStack(spacing: 12) {
                Button(action: session.seekBackward) {
                    Label("Back 10 seconds", systemImage: "gobackward.10")
                }
                .disabled(!session.canSeek)
                .accessibilityLabel("Back 10 seconds")

                Button(action: session.togglePlayback) {
                    Label(session.isPlaying ? "Pause" : "Play", systemImage: session.isPlaying ? "pause.fill" : "play.fill")
                }
                .disabled(!session.canControlPlayback)
                .accessibilityLabel(session.isPlaying ? "Pause playback" : "Play movie")

                Button(action: session.seekForward) {
                    Label("Forward 30 seconds", systemImage: "goforward.30")
                }
                .disabled(!session.canSeek)
                .accessibilityLabel("Forward 30 seconds")

                Spacer()

                Text(session.timeSummary)
                    .font(.system(.subheadline, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Playback time \(session.timeSummary)")

                audioMenu
                subtitleMenu
            }
        }
        .padding(18)
        .frame(maxWidth: 760)
        .glassBackgroundEffect(in: RoundedRectangle(cornerRadius: 24, style: .continuous))
        .accessibilityElement(children: .contain)
    }

    private var audioMenu: some View {
        Menu {
            ForEach(session.audioOptions) { option in
                Button(option.displayName) {
                    session.selectAudio(id: option.id)
                }
            }
        } label: {
            Label("Audio", systemImage: "speaker.wave.2")
        }
        .disabled(session.audioOptions.isEmpty)
        .accessibilityLabel("Audio track")
    }

    private var subtitleMenu: some View {
        Menu {
            ForEach(session.subtitleOptions) { option in
                Button(option.displayName) {
                    session.selectSubtitle(id: option.id)
                }
            }
        } label: {
            Label("CC", systemImage: "captions.bubble")
        }
        .disabled(session.subtitleOptions.isEmpty)
        .accessibilityLabel("Closed captions")
    }
}

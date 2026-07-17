import RealityKit
import SwiftUI

struct SpatialPlaybackPlayerView: View {
    @ObservedObject var model: PlaybackProbeModel

    var body: some View {
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

                if !model.hasLoadedAsset {
                    ContentUnavailableView {
                        Label("No Preview Loaded", systemImage: "visionpro")
                    } description: {
                        Text("Choose a finalized movie in the Spatial Playback Controls window.")
                    }
                } else if model.isLoading {
                    ProgressView("Preparing spatial playback…")
                        .padding(24)
                        .glassBackgroundEffect()
                }
            }
        }
        .aspectRatio(CGSize(width: 16, height: 9), contentMode: .fit)
        .background(.black)
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

        let scale = min(frameSize.x / screenSize.x, frameSize.y / screenSize.y)
        guard scale.isFinite, scale > 0 else {
            return
        }
        model.playerEntity.scale = SIMD3<Float>(repeating: scale)
    }
}

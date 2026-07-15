import SwiftUI

@main
struct SpatialPlaybackProbeApp: App {
    static let immersiveSpaceID = "spatial-playback"

    @StateObject private var model = PlaybackProbeModel()
    @State private var immersionStyle: ImmersionStyle = .mixed

    var body: some Scene {
        WindowGroup("Spatial Playback") {
            PlaybackProbeView(model: model)
                .task {
                    await model.bootstrap()
                }
                .onOpenURL { url in
                    model.importAsset(from: url)
                }
        }
        .defaultSize(width: 1.1, height: 0.76, depth: 0.24, in: .meters)
        .windowStyle(.volumetric)

        ImmersiveSpace(id: Self.immersiveSpaceID) {
            SpatialPlaybackRealityView(model: model)
        }
        .immersionStyle(selection: $immersionStyle, in: .mixed)
    }
}

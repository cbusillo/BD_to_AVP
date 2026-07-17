import SwiftUI

@main
struct SpatialPlaybackProbeApp: App {
    static let controlsWindowID = "spatial-controls"
    static let playerWindowID = "spatial-player"

    @StateObject private var model = PlaybackProbeModel()

    var body: some Scene {
        Window("Spatial Playback Controls", id: Self.controlsWindowID) {
            PlaybackProbeView(model: model)
                .task {
                    await model.bootstrap()
                }
                .onOpenURL { url in
                    model.importAsset(from: url)
                }
        }
        .defaultSize(width: 0.62, height: 0.72, depth: 0.12, in: .meters)
        .windowStyle(.plain)

        Window("Spatial Player", id: Self.playerWindowID) {
            SpatialPlaybackPlayerView(model: model)
                .frame(depth: 1)
        }
        .defaultSize(width: 1.2, height: 0.675, depth: 1, in: .meters)
        .windowStyle(.plain)
    }
}

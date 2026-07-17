import SwiftUI

@main
struct SpatialPlaybackProbeApp: App {
    static let playbackCheckWindowID = "playback-check"

    @StateObject private var model = PlaybackProbeModel()

    var body: some Scene {
        Window("BD to AVP Playback Check", id: Self.playbackCheckWindowID) {
            PlaybackProbeView(model: model)
                .task {
                    await model.bootstrap()
                }
                .onOpenURL { url in
                    model.importAsset(from: url)
                }
        }
        .defaultSize(width: 1.35, height: 0.82, depth: 0.30, in: .meters)
        .windowStyle(.volumetric)
    }
}

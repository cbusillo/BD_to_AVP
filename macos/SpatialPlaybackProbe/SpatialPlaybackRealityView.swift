import RealityKit
import SwiftUI

struct SpatialPlaybackRealityView: View {
    @ObservedObject var model: PlaybackProbeModel

    var body: some View {
        RealityView { content in
            model.installPlayerComponent()
            model.playerEntity.removeFromParent()
            content.add(model.playerEntity)
        }
    }
}

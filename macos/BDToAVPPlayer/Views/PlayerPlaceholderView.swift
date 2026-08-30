import SwiftUI

struct PlayerPlaceholderView: View {
    var body: some View {
        ContentUnavailableView(
            "BD to AVP Player",
            systemImage: "film",
            description: Text("Playback is coming soon.")
        )
    }
}

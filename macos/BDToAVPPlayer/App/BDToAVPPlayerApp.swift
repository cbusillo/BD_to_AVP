import SwiftUI

@main
struct BDToAVPPlayerApp: App {
    @StateObject private var model = PlayerAppModel()

    var body: some Scene {
        WindowGroup {
            AppShellView(model: model)
        }
    }
}

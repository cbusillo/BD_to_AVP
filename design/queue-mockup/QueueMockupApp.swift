import SwiftUI

@main
struct QueueMockupApp: App {
    var body: some Scene {
        WindowGroup("BD to AVP Queue Mockup") {
            QueueMockupRootView()
        }
        .defaultSize(width: 1180, height: 760)
        .windowResizability(.contentMinSize)
    }
}

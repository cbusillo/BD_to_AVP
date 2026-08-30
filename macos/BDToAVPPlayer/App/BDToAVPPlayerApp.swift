import SwiftUI

@main
struct BDToAVPPlayerApp: App {
    @StateObject private var model: PlayerAppModel
    @StateObject private var playerSession: MVHEVCPlayerSession
    private let resumeStore: ResumeStore

    init() {
        let bookmarkStore = BookmarkStore()
        _model = StateObject(wrappedValue: PlayerAppModel(bookmarkStore: bookmarkStore))
        _playerSession = StateObject(wrappedValue: MVHEVCPlayerSession())
        resumeStore = ResumeStore()
    }

    var body: some Scene {
        WindowGroup {
            AppShellView(model: model, playerSession: playerSession, resumeStore: resumeStore)
        }
        .windowStyle(.plain)
        .defaultSize(width: 1.25, height: 0.78, depth: 0.08, in: .meters)
    }
}

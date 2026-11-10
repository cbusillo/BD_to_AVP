import SwiftUI

@main
struct BDToAVPPlayerApp: App {
    @StateObject private var model: PlayerAppModel
    @StateObject private var playerSession: MVHEVCPlayerSession
    @StateObject private var cinema = CinemaState()
    /// Cinema opens fully dark like a theater; the Digital Crown brings the room back in.
    @State private var cinemaImmersion: ImmersionStyle = Self.cinemaImmersionStyle
    private static let cinemaImmersionStyle = ProgressiveImmersionStyle(immersion: 0.2 ... 1.0, initialAmount: 1.0)
    private let resumeStore: ResumeStore

    init() {
        let bookmarkStore = BookmarkStore()
        _model = StateObject(wrappedValue: PlayerAppModel(bookmarkStore: bookmarkStore))
        _playerSession = StateObject(wrappedValue: MVHEVCPlayerSession())
        resumeStore = ResumeStore()
    }

    var body: some Scene {
        WindowGroup {
            AppShellView(
                model: model,
                playerSession: playerSession,
                resumeStore: resumeStore
            )
        }
        .environmentObject(cinema)
        // The library is always the window the app opens with (#1187).
        .defaultLaunchBehavior(.presented)
        .windowStyle(.plain)
        .defaultSize(width: 1.25, height: 0.78, depth: 0.08, in: .meters)

        ImmersiveSpace(id: CinemaState.spaceID) {
            CinemaSpaceView(session: playerSession, cinema: cinema)
        }
        .immersionStyle(selection: $cinemaImmersion, in: Self.cinemaImmersionStyle)

        WindowGroup(id: PlaybackStatsView.windowID, for: String.self) { _ in
            PlaybackStatsView(session: playerSession)
        }
        .defaultSize(width: 460, height: 640)
        // Stats belongs to a playing movie. visionOS once restored it alone as the app's only
        // window, with no way back to the library (#1187).
        .defaultLaunchBehavior(.suppressed)
        .restorationBehavior(.disabled)
    }
}

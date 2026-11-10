import RealityKit
import SwiftUI

/// Cinema mode (#1187). visionOS caps a window's size, which caps how much of the movie's detail
/// the headset can show. In Cinema the movie leaves the window for a large screen in an
/// immersive space; the window stays as the controls.
@MainActor
final class CinemaState: ObservableObject {
    static let spaceID = "cinema"
    /// Meters from the viewer to the screen. Far enough that the screen reads as a theater
    /// screen rather than a nearby panel.
    static let screenDistance: Float = 8
    /// Height of the screen's center above the floor, near a seated viewer's eyes.
    static let screenHeight: Float = 1.25
    static let screenAngleRange: ClosedRange<Double> = 40 ... 110
    static let screenAngleStep: Double = 10

    @Published var isActive = false
    /// How much of the view the screen spans, in degrees across.
    @Published var screenAngle: Double = 70

    /// The screen's width in meters for the current angle at `screenDistance`.
    var screenWidth: Float {
        2 * Self.screenDistance * Float(tan(screenAngle * .pi / 360))
    }

    func larger() {
        screenAngle = min(screenAngle + Self.screenAngleStep, Self.screenAngleRange.upperBound)
    }

    func smaller() {
        screenAngle = max(screenAngle - Self.screenAngleStep, Self.screenAngleRange.lowerBound)
    }
}

/// The immersive space's content: the session's one video entity, sized to the chosen angle.
struct CinemaSpaceView: View {
    @ObservedObject var session: MVHEVCPlayerSession
    @ObservedObject var cinema: CinemaState
    /// The video's screen size from RealityKit's size event, before `playerScreenSize` has it.
    @State private var reportedVideoSize: SIMD2<Float>?

    var body: some View {
        RealityView { content in
            session.installPlayerComponent()
            content.add(session.playerEntity)
            _ = content.subscribe(to: VideoPlayerEvents.VideoSizeDidChange.self, on: session.playerEntity) { event in
                reportedVideoSize = event.screenMeshSize
            }
            fit()
        } update: { _ in
            fit()
        }
        .onAppear { cinema.isActive = true }
        // The wearer can leave with the Digital Crown as well as the Cinema button.
        .onDisappear { cinema.isActive = false }
    }

    private func fit() {
        let entity = session.playerEntity
        entity.position = SIMD3<Float>(0, CinemaState.screenHeight, -CinemaState.screenDistance)
        guard let component = entity.components[VideoPlayerComponent.self] else { return }
        var size = component.playerScreenSize
        if size.x <= 0, let reportedVideoSize {
            size = reportedVideoSize
        }
        guard size.x > 0 else { return }
        let scale = cinema.screenWidth / size.x
        guard scale.isFinite, scale > 0 else { return }
        entity.scale = SIMD3<Float>(repeating: scale)
    }
}

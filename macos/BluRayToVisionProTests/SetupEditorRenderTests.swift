import AppKit
import SwiftUI
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class SetupEditorRenderTests: XCTestCase {
    func testReadyProfileSurfaceRendersOutcomeAndPicker() throws {
        let profile = BuiltInProfile.balanced.profile
        let routeState = RouteQualityResolutionState()
        let view = ConversionSetupView(
            selectedProfileID: .constant(profile.id),
            selectedTab: .constant(.video),
            options: .constant(ConversionOptions(encoding: profile.options)),
            profiles: [profile],
            selectedProfile: profile,
            profileModified: true,
            isLocked: false,
            sourceKind: nil,
            routeQualityState: routeState,
            isReady: true,
            openEditor: {},
            saveSelectedProfile: {},
            saveAsNewProfile: {},
            resetProfile: {}
        )
        let hostingView = NSHostingView(
            rootView: view
                .frame(width: 620, height: 520)
                .preferredColorScheme(.light)
        )
        hostingView.frame = NSRect(x: 0, y: 0, width: 620, height: 520)
        hostingView.layoutSubtreeIfNeeded()
        let bitmap = try XCTUnwrap(hostingView.bitmapImageRepForCachingDisplay(in: hostingView.bounds))
        hostingView.cacheDisplay(in: hostingView.bounds, to: bitmap)

        XCTAssertGreaterThan(bitmap.pixelsWide, 0)
        XCTAssertGreaterThan(bitmap.pixelsHigh, 0)
        XCTAssertNotNil(bitmap.tiffRepresentation)
    }
}

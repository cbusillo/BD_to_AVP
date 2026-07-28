import AppKit
import SwiftUI
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class ActivityDrawerRenderTests: XCTestCase {
    func testActivityDrawerRendersBoundedHistory() throws {
        let jobID = UUID(uuidString: "B78EE8D6-9740-40F9-B6F1-103C67287EC4")!
        var state = WorkerLifecycleState()
        state.selectSource(URL(fileURLWithPath: "/tmp/activity-history.m2ts"))
        try state.begin(jobID: jobID, operationKind: .conversion)

        for sequence in 0..<12 {
            try state.receive(
                WorkerEvent(
                    protocolVersion: WorkerJobSpec.protocolVersion,
                    type: .log,
                    jobID: jobID,
                    sequence: sequence,
                    payload: WorkerEventPayload(message: "Processed activity step \(sequence + 1)")
                )
            )
        }
        try state.receive(
            WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .warning,
                jobID: jobID,
                sequence: 12,
                payload: WorkerEventPayload(message: "Using a safe fallback for this source")
            )
        )
        state.failTransport(
            message: "The engine stopped before publishing an output.",
            details: "Review the retained activity above, then capture diagnostics if needed."
        )

        let appearances: [(ColorScheme, NSAppearance.Name)] = [
            (.light, .aqua),
            (.dark, .darkAqua),
        ]
        for (colorScheme, appearanceName) in appearances {
            let content = ActivityDrawer(
                state: state,
                observabilityStatus: .empty,
                showTechnicalDetails: false
            )
            .frame(width: 900, height: 190)
            .preferredColorScheme(colorScheme)
            let hostingView = NSHostingView(rootView: content)
            hostingView.appearance = NSAppearance(named: appearanceName)
            hostingView.frame = NSRect(x: 0, y: 0, width: 900, height: 190)
            hostingView.layoutSubtreeIfNeeded()
            let bitmap = try XCTUnwrap(hostingView.bitmapImageRepForCachingDisplay(in: hostingView.bounds))
            hostingView.cacheDisplay(in: hostingView.bounds, to: bitmap)
            let image = NSImage(size: hostingView.bounds.size)
            image.addRepresentation(bitmap)

            XCTAssertGreaterThanOrEqual(image.size.width, 900)
            XCTAssertGreaterThanOrEqual(image.size.height, 190)
        }
    }
}

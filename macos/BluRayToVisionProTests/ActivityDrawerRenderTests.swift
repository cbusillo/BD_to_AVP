import AppKit
import SwiftUI
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class ActivityDrawerRenderTests: XCTestCase {
    func testStallNoticeRendersWithoutTechnicalDetails() throws {
        let stall = WorkerStallEvent(
            toolRunID: UUID(), stallEpisodeID: UUID(), tool: "mv_hevc_encoder", state: .stalled,
            canExtend: true, grantsUsed: 0, maxGrants: 2, grantSeconds: 120,
            artifacts: [], artifactsOmitted: 0, toolProgress: nil
        )
        let output = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("build/stall-controls")
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        for pending in [false, true] {
            var notice = StallRecoveryState.Notice(jobID: UUID(), stall: stall)
            if pending {
                notice.pendingCommand = WorkerWaitCommand(
                    commandID: UUID(), jobID: notice.jobID,
                    toolRunID: stall.toolRunID, stallEpisodeID: stall.stallEpisodeID
                )
            }
            let content = StallRecoveryNotice(
                notice: notice, supportsWaiting: true, keepWaiting: {}, stop: {}
            )
            .frame(width: 980, height: 120)
            .preferredColorScheme(.light)
            let hostingView = NSHostingView(rootView: content)
            hostingView.appearance = NSAppearance(named: .aqua)
            hostingView.frame = NSRect(x: 0, y: 0, width: 980, height: 120)
            hostingView.layoutSubtreeIfNeeded()
            let bitmap = try XCTUnwrap(hostingView.bitmapImageRepForCachingDisplay(in: hostingView.bounds))
            hostingView.cacheDisplay(in: hostingView.bounds, to: bitmap)
            let image = try XCTUnwrap(bitmap.representation(using: .png, properties: [:]))
            XCTAssertGreaterThan(image.count, 1_000)
            try image.write(to: output.appendingPathComponent(pending ? "stall-pending.png" : "stall-notice.png"))
        }
    }

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

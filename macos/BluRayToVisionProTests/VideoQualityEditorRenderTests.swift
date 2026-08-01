import AppKit
import SwiftUI
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class VideoQualityEditorRenderTests: XCTestCase {
    func testQualityEditorRendersPrimaryStatesAtWideAndNarrowWidths() throws {
        var custom = EncodingOptions()
        custom.editCustomQuality { values in
            values.directFinalBitrate = BitratePreference(mode: .custom, customMbps: 48)
            values.generatedEyeBitrate = BitratePreference(mode: .custom, customMbps: 35)
            values.generatedMergeQuality = 84
            values.upscaleQuality = 87
        }
        custom.upscaleEnabled = true

        var av1 = EncodingOptions()
        av1.selectVideoOutputMode(.av1Stereo)

        var generated = custom
        generated.cropBlackBars = true

        var existingUpscale = EncodingOptions()
        existingUpscale.upscaleEnabled = true

        let scenarios: [(String, EncodingOptions, VideoQualityEditor.Context)] = [
            ("balanced", EncodingOptions(), .profile),
            ("custom", custom, .profile),
            ("generated", generated, .conversion(JobOptions())),
            ("av1", av1, .profile),
            (
                "existing-upscale",
                existingUpscale,
                .conversion(JobOptions(startStage: .upscaleVideo))
            ),
            (
                "existing-inactive",
                existingUpscale,
                .conversion(JobOptions(startStage: .transcodeAudio))
            ),
        ]
        let widths: [CGFloat] = [420, 760]
        let appearances: [(ColorScheme, NSAppearance.Name)] = [
            (.light, .aqua),
            (.dark, .darkAqua),
        ]

        for (name, options, context) in scenarios {
            for width in widths {
                for (colorScheme, appearanceName) in appearances {
                    try attachRender(
                        VideoQualityEditor(
                            options: .constant(options),
                            context: context
                        ),
                        name: "quality-\(name)",
                        width: width,
                        colorScheme: colorScheme,
                        appearanceName: appearanceName
                    )
                }
            }
        }
    }

    func testRouteSummaryRendersRequestedAndSelectedFallbackSettings() throws {
        let fallback = VideoRouteReport(
            intent: "automatic",
            selected: "generated_mv_hevc",
            reason: "direct_capability_unavailable",
            bitrateMbps: nil,
            eyeBitrateMbps: 20,
            mergeQuality: 75,
            crf: nil,
            fallbackReason: "helper_missing",
            fallbackTiming: "pre_input",
            qualityIntent: VideoRouteReport.QualityIntent(
                mode: "ladder",
                step: "balanced",
                mappingVersion: VideoQualityCatalog.mappingVersion
            ),
            requested: VideoRouteReport.RouteSettings(
                route: "direct_mv_hevc",
                bitrateMbps: nil,
                eyeBitrateMbps: nil,
                mergeQuality: nil,
                crf: nil,
                rateControl: "quality",
                quality: 0.7,
                upscaleMode: nil,
                upscaleQuality: nil
            )
        )
        let widths: [CGFloat] = [420, 760]
        let appearances: [(ColorScheme, NSAppearance.Name)] = [
            (.light, .aqua),
            (.dark, .darkAqua),
        ]

        for width in widths {
            for (colorScheme, appearanceName) in appearances {
                try attachRender(
                    VideoRouteSummaryView(report: fallback),
                    name: "route-fallback",
                    width: width,
                    colorScheme: colorScheme,
                    appearanceName: appearanceName
                )
            }
        }
    }

    private func attachRender<Content: View>(
        _ view: Content,
        name: String,
        width: CGFloat,
        colorScheme: ColorScheme,
        appearanceName: NSAppearance.Name
    ) throws {
        let content = ScrollView {
            view.padding(16)
        }
        .frame(width: width, height: 720)
        .background(Color(nsColor: .windowBackgroundColor))
        .preferredColorScheme(colorScheme)
        let hostingView = NSHostingView(rootView: content)
        hostingView.appearance = NSAppearance(named: appearanceName)
        hostingView.frame = NSRect(x: 0, y: 0, width: width, height: 720)
        hostingView.layoutSubtreeIfNeeded()
        let bitmap = try XCTUnwrap(
            hostingView.bitmapImageRepForCachingDisplay(in: hostingView.bounds),
            "Missing render for \(name) at \(Int(width)) px"
        )
        hostingView.cacheDisplay(in: hostingView.bounds, to: bitmap)
        let image = NSImage(size: hostingView.bounds.size)
        image.addRepresentation(bitmap)

        XCTAssertGreaterThanOrEqual(image.size.width, width)
        XCTAssertGreaterThanOrEqual(image.size.height, 720)
        XCTAssertNotNil(image.tiffRepresentation)
        XCTAssertGreaterThan(distinctColorCount(in: bitmap), 3, "Render appears blank for \(name)")

        let attachment = XCTAttachment(image: image)
        attachment.name = "\(name)-\(Int(width))-\(colorScheme == .dark ? "dark" : "light")"
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func distinctColorCount(in bitmap: NSBitmapImageRep) -> Int {
        var colors = Set<String>()
        for y in stride(from: 0, to: bitmap.pixelsHigh, by: 12) {
            for x in stride(from: 0, to: bitmap.pixelsWide, by: 12) {
                guard let color = bitmap.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB) else {
                    continue
                }
                let components = [color.redComponent, color.greenComponent, color.blueComponent]
                    .map { Int(($0 * 31).rounded()) }
                colors.insert(components.map(String.init).joined(separator: ":"))
            }
        }
        return colors.count
    }
}

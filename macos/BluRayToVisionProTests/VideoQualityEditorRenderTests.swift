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

    func testRouteSummaryRendersUnsupportedQualityAsCustom() throws {
        let reports = [
            (
                "generated-unsupported",
                VideoRouteReport(
                    intent: "generated",
                    selected: "generated_mv_hevc",
                    reason: "generated_route_requested",
                    bitrateMbps: nil,
                    eyeBitrateMbps: 33,
                    mergeQuality: 88,
                    crf: nil,
                    fallbackReason: nil,
                    fallbackTiming: nil,
                    upscaleQuality: 66,
                    qualityIntent: VideoRouteReport.QualityIntent(
                        mode: "ladder",
                        step: QualityStep.maximumDetail.rawValue,
                        mappingVersion: VideoQualityCatalog.mappingVersion
                    )
                )
            ),
            (
                "existing-unsupported",
                VideoRouteReport(
                    intent: "existing_artifact",
                    selected: "existing_artifact",
                    reason: "resume_uses_existing_video_artifact",
                    bitrateMbps: nil,
                    eyeBitrateMbps: nil,
                    mergeQuality: nil,
                    crf: nil,
                    fallbackReason: nil,
                    fallbackTiming: nil,
                    upscaleQuality: 66,
                    qualityIntent: VideoRouteReport.QualityIntent(
                        mode: "ladder",
                        step: QualityStep.maximumDetail.rawValue,
                        mappingVersion: VideoQualityCatalog.mappingVersion
                    )
                )
            ),
        ]
        for (name, report) in reports {
            for (colorScheme, appearanceName) in [(ColorScheme.light, NSAppearance.Name.aqua), (.dark, .darkAqua)] {
                try attachRender(
                    VideoRouteSummaryView(report: report),
                    name: "route-\(name)",
                    width: 760,
                    colorScheme: colorScheme,
                    appearanceName: appearanceName
                )
            }
        }
    }

    func testRouteQualityConflictRendererCoversEveryTrigger() throws {
        let cases: [(String, RouteQualityTrigger, ConversionOptions, RouteQualityEdit)] = try [
            conflictRenderCase(name: "generated", trigger: .generatedRouteRequirement, edit: .qualityStep(.maximumDetail), generatedBase: true),
            conflictRenderCase(name: "reusable", trigger: .reusableIntermediates, edit: .reusableIntermediates(true)),
            conflictRenderCase(name: "software", trigger: .softwareEncoder, edit: .softwareEncoder(true)),
            conflictRenderCase(name: "restart", trigger: .restartStage, edit: .restartStage(.createLeftRightFiles)),
            conflictRenderCase(name: "upscale", trigger: .upscaleCrop, edit: .upscaleEnabled(true)),
            conflictRenderCase(name: "crop", trigger: .upscaleCrop, edit: .cropBlackBars(true), upscaleBase: true),
            conflictRenderCase(name: "field-of-view", trigger: .fieldOfView, edit: .fieldOfView(0)),
            conflictRenderCase(name: "resolution", trigger: .resolutionOverride, edit: .resolutionOverride("1280x720"), upscaleBase: true),
            conflictRenderCase(name: "output-mode", trigger: .outputMode, edit: .outputMode(.av1Stereo)),
        ]

        for (name, expectedTrigger, base, edit) in cases {
            let proposal = RouteQualityEngine.propose(options: base, edit: edit)
            guard case let .conflict(conflict) = proposal else {
                return XCTFail("Expected conflict for \(name)")
            }
            XCTAssertEqual(conflict.trigger, expectedTrigger)
            for (colorScheme, appearanceName) in [(ColorScheme.light, NSAppearance.Name.aqua), (.dark, .darkAqua)] {
                try attachRender(
                    RouteQualityConflictView(conflict: conflict, resolve: { _ in }),
                    name: "route-quality-\(name)",
                    width: 760,
                    colorScheme: colorScheme,
                    appearanceName: appearanceName
                )
            }
        }
    }

    private func conflictRenderCase(
        name: String,
        trigger: RouteQualityTrigger,
        edit: RouteQualityEdit,
        generatedBase: Bool = false,
        upscaleBase: Bool = false
    ) throws -> (String, RouteQualityTrigger, ConversionOptions, RouteQualityEdit) {
        var options = ConversionOptions()
        options.job.intermediatePolicy = generatedBase ? .reusable : .automatic
        options.encoding.upscaleEnabled = upscaleBase
        try options.encoding.selectQualityStep(generatedBase ? .balanced : .maximumDetail)
        return (name, trigger, options, edit)
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

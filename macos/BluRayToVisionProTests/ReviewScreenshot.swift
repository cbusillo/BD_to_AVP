import AppKit
import SwiftUI
import XCTest

/// Saves rendered views as PNGs for a person to review, only when a directory is supplied.
///
/// Set `BD_TO_AVP_REVIEW_SCREENSHOT_DIRECTORY` (pass it to `xcodebuild` as
/// `TEST_RUNNER_BD_TO_AVP_REVIEW_SCREENSHOT_DIRECTORY`). Without it, nothing is shown or written.
@MainActor
enum ReviewScreenshot {
    static let directoryEnvironmentKey = "BD_TO_AVP_REVIEW_SCREENSHOT_DIRECTORY"
    private static var retainedWindows: [NSWindow] = []

    static var directoryURL: URL? {
        guard let directory = ProcessInfo.processInfo.environment[directoryEnvironmentKey], !directory.isEmpty else {
            return nil
        }
        return URL(fileURLWithPath: directory, isDirectory: true)
    }

    /// Renders the view in an off-screen window, so lists and other AppKit-backed controls draw their rows.
    static func capture<Content: View>(
        _ view: Content,
        size: CGSize,
        appearance: NSAppearance.Name = .aqua,
        named name: String
    ) throws {
        guard let directoryURL else {
            return
        }
        let hostingView = NSHostingView(rootView: view.frame(width: size.width, height: size.height))
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: size),
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: appearance)
        window.contentView = hostingView
        window.setFrameOrigin(NSPoint(x: -20_000, y: -20_000))
        window.orderFrontRegardless()
        // The window stays open, off screen, for the rest of the run: the app quits when its last
        // window closes, which would end the test run.
        retainedWindows.append(window)
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        hostingView.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.2))
        let bitmap = try XCTUnwrap(hostingView.bitmapImageRepForCachingDisplay(in: hostingView.bounds))
        hostingView.cacheDisplay(in: hostingView.bounds, to: bitmap)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let data = try XCTUnwrap(bitmap.representation(using: .png, properties: [:]))
        try data.write(to: directoryURL.appendingPathComponent("\(name).png"))
    }
}

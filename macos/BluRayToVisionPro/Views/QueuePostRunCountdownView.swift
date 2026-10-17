import AppKit
import Combine
import SwiftUI

/// The countdown before the Mac sleeps or shuts down, with Cancel as the main control.
struct QueuePostRunCountdownView: View {
    let countdown: QueuePostRunCountdown
    let cancel: () -> Void
    /// Fixes the displayed time, for review screenshots; `nil` follows the clock.
    var displayDate: Date?

    var body: some View {
        TimelineView(.periodic(from: countdown.startedAt, by: 1)) { context in
            content(at: displayDate ?? context.date)
        }
    }

    private func content(at date: Date) -> some View {
        VStack(spacing: 14) {
            Image(systemName: countdown.action == .shutDown ? "power.circle.fill" : "moon.zzz.fill")
                .font(.system(size: 40, weight: .regular))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(Color.accentColor)
                .accessibilityHidden(true)
            Text(countdown.title(at: date))
                .font(.title3.weight(.semibold).monospacedDigit())
                .multilineTextAlignment(.center)
                .accessibilityIdentifier("post-run-countdown-title")
            Text(QueuePostRunCountdown.detail)
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            Button(action: cancel) {
                Text("Cancel")
                    .frame(minWidth: 180)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .keyboardShortcut(.defaultAction)
            .accessibilityIdentifier("post-run-countdown-cancel")
            Text("Press Return or Escape to cancel.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(24)
        .frame(width: 380)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("post-run-countdown")
    }
}

/// Shows the post-run countdown in a floating panel above other apps, and announces it to VoiceOver.
///
/// It only presents what `QueuePostRunController` decides; closing the panel cancels the countdown.
@MainActor
final class QueuePostRunCountdownPresenter: NSObject, NSWindowDelegate {
    private let postRun: QueuePostRunController
    private var panel: NSPanel?
    private var subscription: AnyCancellable?

    init(postRun: QueuePostRunController) {
        self.postRun = postRun
        super.init()
        subscription = postRun.$countdown
            .removeDuplicates()
            .receive(on: RunLoop.main)
            .sink { [weak self] countdown in
                self?.update(countdown)
            }
    }

    private func update(_ countdown: QueuePostRunCountdown?) {
        guard let countdown else {
            dismiss()
            return
        }
        dismiss()
        let view = QueuePostRunCountdownView(countdown: countdown) { [weak postRun] in
            postRun?.cancel()
        }
        let panel = CountdownPanel(
            contentRect: NSRect(x: 0, y: 0, width: 380, height: 260),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        panel.title = "Queue Finished"
        panel.isReleasedWhenClosed = false
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.hidesOnDeactivate = false
        panel.contentViewController = NSHostingController(rootView: view)
        panel.delegate = self
        panel.center()
        self.panel = panel
        NSApp.activate()
        panel.makeKeyAndOrderFront(nil)
        NSAccessibility.post(
            element: NSApp as Any,
            notification: .announcementRequested,
            userInfo: [
                .announcement: "\(countdown.title(at: countdown.startedAt)). \(QueuePostRunCountdown.detail)",
                .priority: NSAccessibilityPriorityLevel.high.rawValue,
            ]
        )
    }

    private func dismiss() {
        guard let panel else {
            return
        }
        self.panel = nil
        panel.delegate = nil
        panel.close()
    }

    func windowWillClose(_ notification: Notification) {
        // Closing the panel is a cancel, never a confirmation.
        panel = nil
        postRun.cancel()
    }
}

/// Escape cancels the countdown as well as Return.
private final class CountdownPanel: NSPanel {
    override func cancelOperation(_ sender: Any?) {
        close()
    }
}

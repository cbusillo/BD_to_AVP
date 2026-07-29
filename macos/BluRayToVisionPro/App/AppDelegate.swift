import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    nonisolated static let startupSmokeArgument = "--startup-smoke"
    nonisolated static let previewPresentationSmokeArgument = "--preview-presentation-smoke"

    weak var workCoordinator: AppWorkCoordinator?
    var observabilityEventStore: any ObservabilityEventPersisting = NullObservabilityEventStore.shared
    private weak var managedWindow: NSWindow?
    private var originalWindowDelegate: NSWindowDelegate?
    private var allowManagedWindowClose = false
    private var isStoppingForWindowClose = false
    private var isStoppingForTermination = false
    private var previewPresentationSmokeWindow: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let arguments = ProcessInfo.processInfo.arguments
        if let configuration = PreviewPresentationSmokeConfiguration.parse(arguments: arguments) {
            let window = NSWindow(
                contentViewController: NSHostingController(
                    rootView: PreviewPresentationSmokeView(configuration: configuration)
                        .frame(width: 820, height: 620)
                )
            )
            window.title = "Preview Presentation Smoke"
            window.isReleasedWhenClosed = false
            window.center()
            previewPresentationSmokeWindow = window
            window.orderFrontRegardless()
            return
        }
        guard Self.isStartupSmoke(arguments: arguments) else {
            return
        }
        DispatchQueue.main.async {
            NSApp.terminate(nil)
        }
    }

    nonisolated static func isStartupSmoke(arguments: [String]) -> Bool {
        arguments.contains(startupSmokeArgument)
    }

    nonisolated static func isPreviewPresentationSmoke(arguments: [String]) -> Bool {
        arguments.contains(previewPresentationSmokeArgument)
    }

    nonisolated static func isAutomationSmoke(arguments: [String]) -> Bool {
        isStartupSmoke(arguments: arguments) || isPreviewPresentationSmoke(arguments: arguments)
    }

    func attach(window: NSWindow, workCoordinator: AppWorkCoordinator) {
        self.workCoordinator = workCoordinator
        guard managedWindow !== window else {
            return
        }
        if let managedWindow, managedWindow.delegate === self {
            managedWindow.delegate = originalWindowDelegate
        }
        managedWindow = window
        originalWindowDelegate = window.delegate
        window.delegate = self
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if allowManagedWindowClose || !(workCoordinator?.hasActiveWorker ?? false) {
            return originalWindowDelegate?.windowShouldClose?(sender) ?? true
        }
        if isStoppingForWindowClose {
            return false
        }

        let alert = stopAlert(action: "close this window", buttonTitle: "Stop and Close")
        guard alert.runModal() == .alertFirstButtonReturn, let workCoordinator else {
            return false
        }

        isStoppingForWindowClose = true
        Task {
            await workCoordinator.stopForQuit()
            allowManagedWindowClose = true
            sender.performClose(nil)
        }
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if Self.isAutomationSmoke(arguments: ProcessInfo.processInfo.arguments) {
            return .terminateNow
        }
        if isStoppingForTermination {
            return .terminateLater
        }
        guard let workCoordinator, workCoordinator.hasActiveWorker else {
            isStoppingForTermination = true
            Task {
                await flushObservabilityStoreWithDeadline()
                sender.reply(toApplicationShouldTerminate: true)
            }
            return .terminateLater
        }

        let alert = stopAlert(action: "quit", buttonTitle: "Stop and Quit")

        guard alert.runModal() == .alertFirstButtonReturn else {
            return .terminateCancel
        }

        isStoppingForTermination = true
        Task {
            await workCoordinator.stopForQuit()
            await flushObservabilityStoreWithDeadline()
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    private func stopAlert(action: String, buttonTitle: String) -> NSAlert {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Stop the current activity and \(action)?"
        alert.informativeText = "The app will safely stop the current activity before continuing."
        alert.addButton(withTitle: buttonTitle)
        alert.addButton(withTitle: "Cancel")
        return alert
    }

    private func flushObservabilityStoreWithDeadline() async {
        let store = observabilityEventStore
        let completions = AsyncStream<Void> { continuation in
            Task {
                await store.flush()
                continuation.yield()
                continuation.finish()
            }
            Task {
                try? await Task.sleep(for: .milliseconds(250))
                continuation.yield()
                continuation.finish()
            }
        }
        for await _ in completions {
            return
        }
    }

    override func responds(to selector: Selector!) -> Bool {
        super.responds(to: selector) || originalWindowDelegate?.responds(to: selector) == true
    }

    override func forwardingTarget(for selector: Selector!) -> Any? {
        if let originalWindowDelegate, originalWindowDelegate.responds(to: selector) {
            return originalWindowDelegate
        }
        return super.forwardingTarget(for: selector)
    }
}

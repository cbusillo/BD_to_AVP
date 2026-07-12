import AppKit

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    weak var viewModel: ConversionViewModel?
    private weak var managedWindow: NSWindow?
    private var originalWindowDelegate: NSWindowDelegate?
    private var allowManagedWindowClose = false
    private var isStoppingForWindowClose = false
    private var isStoppingForTermination = false

    func attach(window: NSWindow, viewModel: ConversionViewModel) {
        self.viewModel = viewModel
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
        if allowManagedWindowClose || !(viewModel?.hasActiveWorker ?? false) {
            return originalWindowDelegate?.windowShouldClose?(sender) ?? true
        }
        if isStoppingForWindowClose {
            return false
        }

        let alert = stopAlert(action: "close this window", buttonTitle: "Stop and Close")
        guard alert.runModal() == .alertFirstButtonReturn, let viewModel else {
            return false
        }

        isStoppingForWindowClose = true
        Task {
            await viewModel.stopForQuit()
            allowManagedWindowClose = true
            sender.performClose(nil)
        }
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if isStoppingForTermination {
            return .terminateLater
        }
        guard let viewModel, viewModel.hasActiveWorker else {
            return .terminateNow
        }

        let alert = stopAlert(action: "quit", buttonTitle: "Stop and Quit")

        guard alert.runModal() == .alertFirstButtonReturn else {
            return .terminateCancel
        }

        isStoppingForTermination = true
        Task {
            await viewModel.stopForQuit()
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

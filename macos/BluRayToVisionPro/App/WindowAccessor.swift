import AppKit
import SwiftUI

struct WindowAccessor: NSViewRepresentable {
    let resolve: @MainActor (NSWindow) -> Void

    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        resolveWindow(for: view)
        return view
    }

    func updateNSView(_ view: NSView, context: Context) {
        resolveWindow(for: view)
    }

    private func resolveWindow(for view: NSView) {
        DispatchQueue.main.async {
            guard let window = view.window else {
                return
            }
            resolve(window)
        }
    }
}

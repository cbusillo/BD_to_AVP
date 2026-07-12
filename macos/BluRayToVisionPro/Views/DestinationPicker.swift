import AppKit

enum DestinationPicker {
    @MainActor
    static func chooseDestination(startingAt currentURL: URL) -> URL? {
        let panel = NSOpenPanel()
        panel.title = "Choose a Destination"
        panel.prompt = "Choose"
        panel.message = "Converted videos will be saved in this folder."
        panel.directoryURL = currentURL
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        return panel.runModal() == .OK ? panel.url : nil
    }
}

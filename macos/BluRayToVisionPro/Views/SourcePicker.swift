import AppKit
import UniformTypeIdentifiers

enum SourcePicker {
    @MainActor
    static func chooseExistingSource() -> ConversionSource? {
        let panel = NSOpenPanel()
        panel.title = "Open a 3D Blu-ray Source"
        panel.prompt = "Open Source"
        panel.message = "Choose a Blu-ray folder, ISO, MKV, MTS, or M2TS source."
        panel.canChooseDirectories = true
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.resolvesAliases = true
        guard panel.runModal() == .OK, let url = panel.url else {
            return nil
        }
        return ConversionSource.infer(from: url)
    }

    @MainActor
    static func chooseFile(kind: ConversionSourceKind) -> ConversionSource? {
        let panel = NSOpenPanel()
        panel.title = "Open \(kind.title)"
        panel.prompt = "Open"
        panel.message = pickerMessage(for: kind)
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.resolvesAliases = true
        panel.allowedContentTypes = kind.allowedExtensions.compactMap { fileExtension in
            UTType(filenameExtension: fileExtension)
        }
        guard panel.runModal() == .OK, let url = panel.url else {
            return nil
        }
        return ConversionSource(kind: kind, url: url)
    }

    @MainActor
    static func chooseFolder(kind: ConversionSourceKind) -> ConversionSource? {
        let panel = NSOpenPanel()
        panel.title = kind == .bluRayFolder ? "Open a Blu-ray Folder" : "Open a Source Folder"
        panel.prompt = "Open Folder"
        panel.message = kind == .bluRayFolder
            ? "Choose a folder containing a BDMV directory."
            : "Choose a folder containing disc images, MKV, MTS, or M2TS sources."
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.resolvesAliases = true
        guard panel.runModal() == .OK, let url = panel.url else {
            return nil
        }
        if kind == .bluRayFolder, !DiscSourceDetector.isBluRayFolder(url) {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "This folder is not a Blu-ray source"
            alert.informativeText = "Choose a disc folder that contains a BDMV directory."
            alert.addButton(withTitle: "OK")
            alert.runModal()
            return nil
        }
        return ConversionSource(kind: kind, url: url)
    }

    private static func pickerMessage(for kind: ConversionSourceKind) -> String {
        switch kind {
        case .discImage:
            "Choose an ISO, IMG, or BIN disc image."
        case .matroska:
            "Choose an existing 3D MKV file."
        case .transportStream:
            "Choose an MTS or M2TS transport stream."
        case .physicalDisc, .bluRayFolder, .sourceFolder:
            "Choose a supported source."
        }
    }
}

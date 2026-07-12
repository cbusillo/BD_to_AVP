import Foundation

enum ConversionSourceKind: String, CaseIterable, Identifiable {
    case physicalDisc
    case discImage
    case bluRayFolder
    case matroska
    case sourceFolder
    case transportStream

    var id: String { rawValue }

    var title: String {
        switch self {
        case .physicalDisc:
            "3D Blu-ray Disc"
        case .discImage:
            "Disc Image"
        case .bluRayFolder:
            "Blu-ray Folder"
        case .matroska:
            "MKV File"
        case .sourceFolder:
            "Source Folder"
        case .transportStream:
            "MTS / M2TS File"
        }
    }

    var systemImage: String {
        switch self {
        case .physicalDisc:
            "opticaldisc"
        case .discImage:
            "opticaldiscdrive"
        case .bluRayFolder:
            "folder.badge.gearshape"
        case .matroska:
            "film.stack"
        case .sourceFolder:
            "folder.stack"
        case .transportStream:
            "doc.richtext"
        }
    }

    var supportsMetadataInspection: Bool {
        self == .matroska || self == .transportStream
    }

    var isDiscWorkflow: Bool {
        self == .physicalDisc || self == .discImage || self == .bluRayFolder
    }

    var isSecondaryImport: Bool {
        self == .transportStream
    }

    var allowedExtensions: [String] {
        switch self {
        case .discImage:
            ["iso", "img", "bin"]
        case .matroska:
            ["mkv"]
        case .transportStream:
            ["mts", "m2ts"]
        case .physicalDisc, .bluRayFolder, .sourceFolder:
            []
        }
    }
}

struct ConversionSource: Equatable {
    let kind: ConversionSourceKind
    let url: URL
    let displayName: String

    init(kind: ConversionSourceKind, url: URL, displayName: String? = nil) {
        self.kind = kind
        self.url = url.standardizedFileURL
        self.displayName = displayName ?? Self.defaultDisplayName(for: url)
    }

    var proposedOutputStem: String {
        URL(fileURLWithPath: displayName).deletingPathExtension().lastPathComponent
    }

    var locationDescription: String {
        if kind == .physicalDisc {
            return url.path
        }
        return url.deletingLastPathComponent().path
    }

    static func infer(from url: URL, fileManager: FileManager = .default) -> ConversionSource? {
        let normalizedURL = url.standardizedFileURL
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: normalizedURL.path, isDirectory: &isDirectory) else {
            return nil
        }

        if isDirectory.boolValue {
            let kind: ConversionSourceKind = DiscSourceDetector.isBluRayFolder(normalizedURL, fileManager: fileManager)
                ? .bluRayFolder
                : .sourceFolder
            return ConversionSource(kind: kind, url: normalizedURL)
        }

        switch normalizedURL.pathExtension.lowercased() {
        case "iso", "img", "bin":
            return ConversionSource(kind: .discImage, url: normalizedURL)
        case "mkv":
            return ConversionSource(kind: .matroska, url: normalizedURL)
        case "mts", "m2ts":
            return ConversionSource(kind: .transportStream, url: normalizedURL)
        default:
            return nil
        }
    }

    private static func defaultDisplayName(for url: URL) -> String {
        let name = url.lastPathComponent.trimmingCharacters(in: .whitespacesAndNewlines)
        return name.isEmpty ? url.path : name
    }
}

enum DiscSourceDetector {
    private static let makeMKVPath = "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon"

    static var makeMKVAvailable: Bool {
        FileManager.default.isExecutableFile(atPath: makeMKVPath)
    }

    static func insertedDiscs(fileManager: FileManager = .default) -> [ConversionSource] {
        let volumeKeys: [URLResourceKey] = [.volumeNameKey, .isVolumeKey]
        let volumes = fileManager.mountedVolumeURLs(
            includingResourceValuesForKeys: volumeKeys,
            options: [.skipHiddenVolumes]
        ) ?? []
        return insertedDiscs(in: volumes, fileManager: fileManager)
    }

    static func insertedDiscs(in volumes: [URL], fileManager: FileManager = .default) -> [ConversionSource] {
        volumes.compactMap { volumeURL in
            guard isBluRayFolder(volumeURL, fileManager: fileManager) else {
                return nil
            }
            let values = try? volumeURL.resourceValues(forKeys: [.volumeNameKey])
            return ConversionSource(
                kind: .physicalDisc,
                url: volumeURL,
                displayName: values?.volumeName ?? volumeURL.lastPathComponent
            )
        }
        .sorted { $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedAscending }
    }

    static func isBluRayFolder(_ url: URL, fileManager: FileManager = .default) -> Bool {
        let candidates = [url, url.appendingPathComponent("BDMV", isDirectory: true)]
        return candidates.contains { candidate in
            candidate.lastPathComponent.caseInsensitiveCompare("BDMV") == .orderedSame
                && fileManager.fileExists(atPath: candidate.path)
        }
    }
}

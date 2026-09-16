import DiskArbitration
import Foundation

enum ConversionSourceKind: String, CaseIterable, Codable, Hashable, Identifiable {
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
            "folder"
        case .transportStream:
            "doc.richtext"
        }
    }

    var supportsMetadataInspection: Bool {
        self == .physicalDisc
            || self == .discImage
            || self == .bluRayFolder
            || self == .matroska
            || self == .transportStream
    }

    var supportsConversion: Bool {
        supportsMetadataInspection
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
            ["iso"]
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
    let workerSourcePath: String
    let mediaIdentifier: String?

    init(
        kind: ConversionSourceKind,
        url: URL,
        displayName: String? = nil,
        workerSourcePath: String? = nil,
        mediaIdentifier: String? = nil
    ) {
        let normalizedURL = kind == .bluRayFolder
            ? DiscSourceDetector.bluRayRoot(for: url) ?? url.standardizedFileURL
            : url.standardizedFileURL
        self.kind = kind
        self.url = normalizedURL
        self.displayName = displayName ?? Self.defaultDisplayName(for: normalizedURL)
        self.workerSourcePath = workerSourcePath ?? normalizedURL.path
        self.mediaIdentifier = mediaIdentifier
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
        case "iso":
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
    static let makeMKVExecutablePaths = [
        "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon",
        "/Applications/MakeMKV/MakeMKV.app/Contents/MacOS/makemkvcon",
    ]
    static let makeMKVDownloadURL = URL(string: "https://www.makemkv.com/download/")

    static var makeMKVAvailable: Bool {
        hasMakeMKVExecutable(at: makeMKVExecutablePaths)
    }

    static func hasMakeMKVExecutable(
        at paths: [String],
        fileManager: FileManager = .default
    ) -> Bool {
        paths.contains(where: fileManager.isExecutableFile(atPath:))
    }

    /// A disc that is mounted but whose contents could not be listed.
    ///
    /// macOS asks for approval before an app may read a removable volume. Until that approval is
    /// granted the disc mounts and appears in Finder, yet listing it fails, so these volumes are
    /// reported instead of being dropped silently.
    struct InsertedDiscScan: Equatable {
        var discs: [ConversionSource] = []
        var unreadableVolumes: [URL] = []
    }

    enum VolumeProbeResult: Equatable {
        case bluRay(URL)
        case notBluRay
        case unreadable
    }

    static func insertedDiscs(fileManager: FileManager = .default) -> [ConversionSource] {
        scanInsertedDiscs(fileManager: fileManager).discs
    }

    static func scanInsertedDiscs(fileManager: FileManager = .default) -> InsertedDiscScan {
        let volumeKeys: [URLResourceKey] = [.volumeNameKey, .isVolumeKey, .volumeIsLocalKey]
        let volumes = fileManager.mountedVolumeURLs(
            includingResourceValuesForKeys: volumeKeys,
            options: [.skipHiddenVolumes]
        )?.filter { volumeURL in
            (try? volumeURL.resourceValues(forKeys: [.volumeIsLocalKey]).volumeIsLocal) != false
        } ?? []
        return scanInsertedDiscs(in: volumes, fileManager: fileManager)
    }

    static func insertedDiscs(
        in volumes: [URL],
        fileManager: FileManager = .default,
        devicePathResolver: (URL) -> String? = physicalDevicePath(for:)
    ) -> [ConversionSource] {
        scanInsertedDiscs(in: volumes, fileManager: fileManager, devicePathResolver: devicePathResolver).discs
    }

    static func scanInsertedDiscs(
        in volumes: [URL],
        fileManager: FileManager = .default,
        devicePathResolver: (URL) -> String? = physicalDevicePath(for:)
    ) -> InsertedDiscScan {
        var scan = InsertedDiscScan()
        for volumeURL in volumes {
            guard let devicePath = devicePathResolver(volumeURL) else {
                continue
            }
            switch probeVolume(volumeURL, fileManager: fileManager) {
            case .notBluRay:
                continue
            case .unreadable:
                scan.unreadableVolumes.append(volumeURL)
            case .bluRay:
                let values = try? volumeURL.resourceValues(forKeys: [.volumeNameKey])
                scan.discs.append(
                    ConversionSource(
                        kind: .physicalDisc,
                        url: volumeURL,
                        displayName: values?.volumeName ?? volumeURL.lastPathComponent,
                        workerSourcePath: devicePath,
                        mediaIdentifier: mediaIdentifier(for: volumeURL, fileManager: fileManager)
                    )
                )
            }
        }
        scan.discs.sort { $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedAscending }
        scan.unreadableVolumes.sort { $0.path.localizedCaseInsensitiveCompare($1.path) == .orderedAscending }
        return scan
    }

    static func isCurrentPhysicalDisc(
        _ source: ConversionSource,
        devicePathResolver: (URL) -> String? = physicalDevicePath(for:),
        mediaIdentifierResolver: (URL) -> String? = { mediaIdentifier(for: $0) }
    ) -> Bool {
        guard source.kind == .physicalDisc,
              let devicePath = devicePathResolver(source.url),
              devicePath == source.workerSourcePath
        else {
            return false
        }
        guard let expectedIdentifier = source.mediaIdentifier else {
            return true
        }
        return mediaIdentifierResolver(source.url) == expectedIdentifier
    }

    private static func mediaIdentifier(
        for volumeURL: URL,
        fileManager: FileManager = .default
    ) -> String? {
        if let volumeUUID = try? volumeURL.resourceValues(forKeys: [.volumeUUIDStringKey]).volumeUUIDString {
            return "uuid:\(volumeUUID)"
        }
        let indexURL = volumeURL
            .appendingPathComponent("BDMV", isDirectory: true)
            .appendingPathComponent("index.bdmv")
        guard let attributes = try? fileManager.attributesOfItem(atPath: indexURL.path),
              let size = attributes[.size] as? NSNumber,
              let modified = attributes[.modificationDate] as? Date
        else {
            return nil
        }
        return "bdmv:\(size.int64Value):\(modified.timeIntervalSince1970)"
    }

    private static func physicalDevicePath(for volumeURL: URL) -> String? {
        guard let session = DASessionCreate(kCFAllocatorDefault),
              let disk = DADiskCreateFromVolumePath(kCFAllocatorDefault, session, volumeURL as CFURL)
        else {
            return nil
        }
        let wholeDisk = DADiskCopyWholeDisk(disk) ?? disk
        guard let deviceName = DADiskGetBSDName(wholeDisk) else {
            return nil
        }
        return "/dev/\(String(cString: deviceName))"
    }

    static func isBluRayFolder(_ url: URL, fileManager: FileManager = .default) -> Bool {
        bluRayRoot(for: url, fileManager: fileManager) != nil
    }

    static func bluRayRoot(for url: URL, fileManager: FileManager = .default) -> URL? {
        guard case let .bluRay(rootURL) = probeVolume(url, fileManager: fileManager) else {
            return nil
        }
        return rootURL
    }

    /// Classifies a folder or volume, keeping "no disc structure here" distinct from "this exists
    /// but its contents cannot be listed". The caller needs that difference to explain why a
    /// mounted disc produced no source.
    static func probeVolume(_ url: URL, fileManager: FileManager = .default) -> VolumeProbeResult {
        let normalizedURL = url.standardizedFileURL
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: normalizedURL.path, isDirectory: &isDirectory), isDirectory.boolValue else {
            return .notBluRay
        }
        if normalizedURL.lastPathComponent.caseInsensitiveCompare("BDMV") == .orderedSame {
            return .bluRay(normalizedURL.deletingLastPathComponent())
        }
        let children: [URL]
        do {
            children = try fileManager.contentsOfDirectory(
                at: normalizedURL,
                includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles]
            )
        } catch {
            return isPermissionError(error) ? .unreadable : .notBluRay
        }
        let containsDiscFolder = children.contains { childURL in
            childURL.lastPathComponent.caseInsensitiveCompare("BDMV") == .orderedSame
                && (try? childURL.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true
        }
        return containsDiscFolder ? .bluRay(normalizedURL) : .notBluRay
    }

    static func isPermissionError(_ error: Error) -> Bool {
        let nsError = error as NSError
        if nsError.domain == NSCocoaErrorDomain {
            return nsError.code == NSFileReadNoPermissionError || nsError.code == NSFileWriteNoPermissionError
        }
        if nsError.domain == NSPOSIXErrorDomain {
            return nsError.code == Int(EPERM) || nsError.code == Int(EACCES)
        }
        return false
    }
}

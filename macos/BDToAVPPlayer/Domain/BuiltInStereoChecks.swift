import Foundation

struct InstalledStereoCheck: Equatable, Sendable {
    let item: MediaItem
    let url: URL
}

enum BuiltInStereoChecks {
    static let sideBySideID = "builtin:stereo-check-sbs"
    static let overUnderID = "builtin:stereo-check-ou"
    private static let fixtureSetVersion = "2"
    private static let versionFileName = ".fixture-version"

    private struct Descriptor {
        let id: String
        let title: String
        let fileName: String
        let format: StereoFormat
    }

    private static let descriptors = [
        Descriptor(
            id: sideBySideID,
            title: "Side-by-Side Stereo Check",
            fileName: "Stereo-Check-SBS.mov",
            format: .sideBySide
        ),
        Descriptor(
            id: overUnderID,
            title: "Over-Under Stereo Check",
            fileName: "Stereo-Check-OU.mov",
            format: .overUnder
        )
    ]

    static var orderedIDs: [String] {
        descriptors.map(\.id)
    }

    static func contains(_ item: MediaItem?) -> Bool {
        guard let item else { return false }
        return orderedIDs.contains(item.id)
    }

    static func install(
        bundle: Bundle = .main,
        fileManager: FileManager = .default,
        destinationDirectory: URL? = nil
    ) throws -> [InstalledStereoCheck] {
        let destinationDirectory = destinationDirectory ?? defaultDestinationDirectory(fileManager: fileManager)
        try fileManager.createDirectory(at: destinationDirectory, withIntermediateDirectories: true)

        let sources = try descriptors.map { descriptor -> (Descriptor, URL) in
            guard let sourceURL = resourceURL(for: descriptor.fileName, bundle: bundle) else {
                throw CocoaError(.fileNoSuchFile)
            }
            return (descriptor, sourceURL)
        }
        let versionURL = destinationDirectory.appendingPathComponent(versionFileName)
        let installedVersion = try? String(contentsOf: versionURL, encoding: .utf8)
        let installationIsCurrent = installedVersion == fixtureSetVersion
            && sources.allSatisfy { descriptor, sourceURL in
                let destinationURL = destinationDirectory.appendingPathComponent(descriptor.fileName)
                return fileSizesMatch(sourceURL, destinationURL, fileManager: fileManager)
            }

        if !installationIsCurrent {
            for (descriptor, sourceURL) in sources {
                let destinationURL = destinationDirectory.appendingPathComponent(descriptor.fileName)
                if fileManager.fileExists(atPath: destinationURL.path) {
                    try fileManager.removeItem(at: destinationURL)
                }
                try fileManager.copyItem(at: sourceURL, to: destinationURL)
            }
            try fixtureSetVersion.write(to: versionURL, atomically: true, encoding: .utf8)
        }

        return sources.map { descriptor, _ in
            let destinationURL = destinationDirectory.appendingPathComponent(descriptor.fileName)
            return InstalledStereoCheck(
                item: MediaItem(
                    id: descriptor.id,
                    title: descriptor.title,
                    fileName: descriptor.fileName,
                    format: descriptor.format
                ),
                url: destinationURL
            )
        }
    }

    private static func fileSizesMatch(_ sourceURL: URL, _ destinationURL: URL, fileManager: FileManager) -> Bool {
        guard let sourceSize = try? fileManager.attributesOfItem(atPath: sourceURL.path)[.size] as? NSNumber,
              let destinationSize = try? fileManager.attributesOfItem(atPath: destinationURL.path)[.size] as? NSNumber
        else {
            return false
        }
        return sourceSize == destinationSize
    }

    private static func defaultDestinationDirectory(fileManager: FileManager) -> URL {
        fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("BDToAVPPlayer", isDirectory: true)
            .appendingPathComponent("BuiltInStereoChecks", isDirectory: true)
    }

    private static func resourceURL(for fileName: String, bundle: Bundle) -> URL? {
        let resourceName = (fileName as NSString).deletingPathExtension
        let fileExtension = (fileName as NSString).pathExtension
        return bundle.url(forResource: resourceName, withExtension: fileExtension)
            ?? bundle.url(forResource: resourceName, withExtension: fileExtension, subdirectory: "Resources")
    }
}

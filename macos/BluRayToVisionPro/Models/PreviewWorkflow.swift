import Foundation

enum PreviewPhase: Equatable {
    case idle
    case preparing
    case encoding
    case ready
    case stopping
    case failed
    case expired
}

struct PreviewCache {
    static let expirationInterval: TimeInterval = 24 * 60 * 60

    let rootURL: URL
    let fileManager: FileManager

    init(rootURL: URL, fileManager: FileManager = .default) {
        self.rootURL = rootURL.standardizedFileURL
        self.fileManager = fileManager
    }

    static func automatic(fileManager: FileManager = .default) -> PreviewCache {
        let cachesURL = try? fileManager.url(
            for: .cachesDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let baseURL = cachesURL ?? fileManager.temporaryDirectory
        return PreviewCache(
            rootURL: baseURL
                .appendingPathComponent("com.shinycomputers.bd-to-avp", isDirectory: true)
                .appendingPathComponent("Previews", isDirectory: true),
            fileManager: fileManager
        )
    }

    func prepareDirectory(jobID: UUID) throws -> URL {
        try fileManager.createDirectory(at: rootURL, withIntermediateDirectories: true)
        let directoryURL = rootURL.appendingPathComponent(jobID.uuidString, isDirectory: true)
        try remove(directoryURL)
        try fileManager.createDirectory(at: directoryURL, withIntermediateDirectories: false)
        return directoryURL
    }

    func contains(_ fileURL: URL, in directoryURL: URL) -> Bool {
        let filePath = fileURL.resolvingSymlinksInPath().standardizedFileURL.path
        let directoryPath = directoryURL.resolvingSymlinksInPath().standardizedFileURL.path
        let prefix = directoryPath.hasSuffix("/") ? directoryPath : "\(directoryPath)/"
        return filePath.hasPrefix(prefix)
    }

    func remove(_ directoryURL: URL) throws {
        let directoryPath = directoryURL.resolvingSymlinksInPath().standardizedFileURL.path
        let rootPath = rootURL.resolvingSymlinksInPath().standardizedFileURL.path
        let rootPrefix = rootPath.hasSuffix("/") ? rootPath : "\(rootPath)/"
        guard directoryPath != rootPath, directoryPath.hasPrefix(rootPrefix) else {
            return
        }
        if fileManager.fileExists(atPath: directoryURL.path) {
            try fileManager.removeItem(at: directoryURL)
        }
    }

    func removeExpired(now: Date = Date()) {
        guard let children = try? fileManager.contentsOfDirectory(
            at: rootURL,
            includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles]
        ) else {
            return
        }
        for childURL in children {
            let modifiedAt = try? childURL.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate
            guard let modifiedAt,
                  now.timeIntervalSince(modifiedAt) >= Self.expirationInterval else {
                continue
            }
            try? remove(childURL)
        }
    }
}

final class PreviewArtifactLease {
    let artifact: PreviewArtifact
    let directoryURL: URL

    private let cache: PreviewCache
    init(artifact: PreviewArtifact, directoryURL: URL, cache: PreviewCache) {
        self.artifact = artifact
        self.directoryURL = directoryURL
        self.cache = cache
    }

    deinit {
        try? cache.remove(directoryURL)
    }
}

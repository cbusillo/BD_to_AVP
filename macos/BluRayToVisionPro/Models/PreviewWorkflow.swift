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

enum PreviewWorkspaceScope: Equatable, Sendable {
    case localCache
    case selectedDestination
}

extension ConversionSourceKind {
    var previewWorkspaceScope: PreviewWorkspaceScope {
        switch self {
        case .discImage, .bluRayFolder:
            .selectedDestination
        case .matroska, .transportStream, .physicalDisc, .sourceFolder:
            .localCache
        }
    }
}

struct PreviewWorkspacePlan: @unchecked Sendable {
    let scope: PreviewWorkspaceScope
    let cache: PreviewCache
    let capacityProbeURL: URL

    static func resolve(draft: PreviewDraft, localCache: PreviewCache) -> PreviewWorkspacePlan {
        let scope = draft.conversion.source.kind.previewWorkspaceScope
        switch scope {
        case .localCache:
            return PreviewWorkspacePlan(
                scope: scope,
                cache: localCache,
                capacityProbeURL: localCache.rootURL
            )
        case .selectedDestination:
            let destinationURL = draft.conversion.destinationURL
            return PreviewWorkspacePlan(
                scope: scope,
                cache: .destinationScoped(
                    destinationURL: destinationURL,
                    fileManager: localCache.fileManager
                ),
                capacityProbeURL: destinationURL
            )
        }
    }
}

enum PreviewVolumeName {
    static func displayName(for destinationURL: URL) -> String {
        let values = try? destinationURL.resourceValues(forKeys: [.volumeNameKey])
        return values?.volumeName?.trimmingCharacters(in: .whitespacesAndNewlines).nonEmpty
            ?? fallbackName(for: destinationURL)
    }

    static func fallbackName(for destinationURL: URL) -> String {
        let components = destinationURL.standardizedFileURL.pathComponents
        if let volumesIndex = components.firstIndex(of: "Volumes"),
           components.indices.contains(volumesIndex + 1) {
            return components[volumesIndex + 1]
        }
        return "Mac startup volume"
    }
}

struct PreviewCapacityReadings: Equatable, Sendable {
    let importantUsageBytes: Int64?
    let availableBytes: Int64?
}

protocol PreviewCapacityProviding: Sendable {
    func readings(for workspaceURL: URL) -> PreviewCapacityReadings
}

struct SystemPreviewCapacityProvider: PreviewCapacityProviding, @unchecked Sendable {
    let fileManager: FileManager

    init(fileManager: FileManager = .default) {
        self.fileManager = fileManager
    }

    func readings(for workspaceURL: URL) -> PreviewCapacityReadings {
        guard fileManager.fileExists(atPath: workspaceURL.path),
              let values = try? workspaceURL.resourceValues(forKeys: [
                  .volumeAvailableCapacityForImportantUsageKey,
                  .volumeAvailableCapacityKey,
              ])
        else {
            return PreviewCapacityReadings(importantUsageBytes: nil, availableBytes: nil)
        }
        return PreviewCapacityReadings(
            importantUsageBytes: values.volumeAvailableCapacityForImportantUsage,
            availableBytes: values.volumeAvailableCapacity.map(Int64.init)
        )
    }
}

enum PreviewCapacityAssessment: Equatable, Sendable {
    case sufficient(requiredBytes: Int64, availableBytes: Int64)
    case insufficient(requiredBytes: Int64, availableBytes: Int64)
    case unknown(requiredBytes: Int64?)
    case conflicting(requiredBytes: Int64, lowerBytes: Int64, upperBytes: Int64)

    static func evaluate(requiredBytes: Int64?, readings: PreviewCapacityReadings) -> PreviewCapacityAssessment {
        guard let requiredBytes else {
            return .unknown(requiredBytes: nil)
        }

        let important = readings.importantUsageBytes.flatMap(Self.nonNegative)
        let available = readings.availableBytes.flatMap(Self.nonNegative)
        switch (important, available) {
        case let (first?, second?):
            let lower = min(first, second)
            let upper = max(first, second)
            if upper < requiredBytes {
                return .insufficient(requiredBytes: requiredBytes, availableBytes: upper)
            }
            if lower >= requiredBytes {
                return .sufficient(requiredBytes: requiredBytes, availableBytes: lower)
            }
            return .conflicting(requiredBytes: requiredBytes, lowerBytes: lower, upperBytes: upper)
        case let (value?, nil), let (nil, value?):
            guard value > 0 else {
                return .unknown(requiredBytes: requiredBytes)
            }
            if value < requiredBytes {
                return .insufficient(requiredBytes: requiredBytes, availableBytes: value)
            }
            return .sufficient(requiredBytes: requiredBytes, availableBytes: value)
        case (nil, nil):
            return .unknown(requiredBytes: requiredBytes)
        }
    }

    private static func nonNegative(_ value: Int64) -> Int64? {
        value >= 0 ? value : nil
    }
}

struct PreviewCapacityRequirement {
    static let boundedRouteReserveBytes: Int64 = 8 * 1024 * 1024 * 1024

    static func requiredBytes(
        sourceKind: ConversionSourceKind,
        sourceURL: URL,
        inspectedSizeBytes: Int64?,
        fileManager: FileManager
    ) -> Int64? {
        guard sourceKind.previewWorkspaceScope == .selectedDestination,
              let sourceBytes = sourceMaterializationBytes(
                  sourceKind: sourceKind,
                  sourceURL: sourceURL,
                  inspectedSizeBytes: inspectedSizeBytes,
                  fileManager: fileManager
              )
        else {
            return nil
        }
        let (requiredBytes, overflow) = sourceBytes.addingReportingOverflow(boundedRouteReserveBytes)
        return overflow ? Int64.max : requiredBytes
    }

    private static func sourceMaterializationBytes(
        sourceKind: ConversionSourceKind,
        sourceURL: URL,
        inspectedSizeBytes: Int64?,
        fileManager: FileManager
    ) -> Int64? {
        if let inspectedBytes = inspectedSizeBytes, inspectedBytes > 0 {
            return inspectedBytes
        }

        switch sourceKind {
        case .discImage:
            let attributes = try? fileManager.attributesOfItem(atPath: sourceURL.path)
            return (attributes?[.size] as? NSNumber)?.int64Value
        case .bluRayFolder:
            return recursiveFileSize(at: sourceURL, fileManager: fileManager)
        case .physicalDisc, .matroska, .sourceFolder, .transportStream:
            return nil
        }
    }

    private static func recursiveFileSize(at rootURL: URL, fileManager: FileManager) -> Int64? {
        var encounteredError = false
        guard let enumerator = fileManager.enumerator(
            at: rootURL,
            includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey],
            options: [.skipsHiddenFiles, .skipsPackageDescendants],
            errorHandler: { _, _ in
                encounteredError = true
                return false
            }
        ) else {
            return nil
        }

        var totalBytes: Int64 = 0
        for case let fileURL as URL in enumerator {
            guard let values = try? fileURL.resourceValues(forKeys: [
                .isRegularFileKey,
                .isSymbolicLinkKey,
                .fileSizeKey,
            ]) else {
                encounteredError = true
                break
            }
            if values.isSymbolicLink == true {
                continue
            }
            guard values.isRegularFile == true else {
                continue
            }
            let fileBytes = Int64(values.fileSize ?? 0)
            let (updatedTotal, overflow) = totalBytes.addingReportingOverflow(fileBytes)
            if overflow {
                return Int64.max
            }
            totalBytes = updatedTotal
        }
        return encounteredError ? nil : totalBytes
    }
}

struct PreviewWorkspacePreparation: @unchecked Sendable {
    let cache: PreviewCache
    let directoryURL: URL
    let capacityAssessment: PreviewCapacityAssessment?
    let volumeName: String
}

enum PreviewWorkspacePreparationError: Error {
    case insufficientCapacity(volumeName: String, requiredBytes: Int64, availableBytes: Int64)
    case unavailable(volumeName: String)
}

enum PreviewWorkspacePreparer {
    static func prepare(
        jobID: UUID,
        plan: PreviewWorkspacePlan,
        sourceKind: ConversionSourceKind,
        sourceURL: URL,
        inspectedSizeBytes: Int64?,
        capacityProvider: any PreviewCapacityProviding
    ) throws -> PreviewWorkspacePreparation {
        try Task.checkCancellation()
        let volumeName = plan.scope == .selectedDestination
            ? PreviewVolumeName.displayName(for: plan.capacityProbeURL)
            : "Mac startup volume"
        let assessment: PreviewCapacityAssessment?
        if plan.scope == .selectedDestination {
            let requiredBytes = PreviewCapacityRequirement.requiredBytes(
                sourceKind: sourceKind,
                sourceURL: sourceURL,
                inspectedSizeBytes: inspectedSizeBytes,
                fileManager: plan.cache.fileManager
            )
            let evaluated = PreviewCapacityAssessment.evaluate(
                requiredBytes: requiredBytes,
                readings: capacityProvider.readings(for: plan.capacityProbeURL)
            )
            if case .insufficient(let requiredBytes, let availableBytes) = evaluated {
                throw PreviewWorkspacePreparationError.insufficientCapacity(
                    volumeName: volumeName,
                    requiredBytes: requiredBytes,
                    availableBytes: availableBytes
                )
            }
            assessment = evaluated
        } else {
            assessment = nil
        }

        var directoryURL: URL?
        do {
            try Task.checkCancellation()
            directoryURL = try plan.cache.prepareDirectory(jobID: jobID)
            try Task.checkCancellation()
        } catch is CancellationError {
            if let directoryURL {
                try? plan.cache.remove(directoryURL)
            }
            throw CancellationError()
        } catch {
            if let directoryURL {
                try? plan.cache.remove(directoryURL)
            }
            throw PreviewWorkspacePreparationError.unavailable(volumeName: volumeName)
        }

        guard let directoryURL else {
            throw PreviewWorkspacePreparationError.unavailable(volumeName: volumeName)
        }
        return PreviewWorkspacePreparation(
            cache: plan.cache,
            directoryURL: directoryURL,
            capacityAssessment: assessment,
            volumeName: volumeName
        )
    }
}

struct PreviewCache: @unchecked Sendable {
    static let expirationInterval: TimeInterval = 24 * 60 * 60
    static let destinationWorkspaceDirectoryName = ".BluRayToVisionProPreviews"
    private static let operationLock = NSRecursiveLock()

    let rootURL: URL
    let fileManager: FileManager
    private let removesRootWhenEmpty: Bool
    private let requiredRootName: String?

    init(
        rootURL: URL,
        fileManager: FileManager = .default,
        removesRootWhenEmpty: Bool = false,
        requiredRootName: String? = nil
    ) {
        self.rootURL = rootURL.standardizedFileURL
        self.fileManager = fileManager
        self.removesRootWhenEmpty = removesRootWhenEmpty
        self.requiredRootName = requiredRootName
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

    static func destinationScoped(
        destinationURL: URL,
        fileManager: FileManager = .default
    ) -> PreviewCache {
        PreviewCache(
            rootURL: destinationURL.appendingPathComponent(destinationWorkspaceDirectoryName, isDirectory: true),
            fileManager: fileManager,
            removesRootWhenEmpty: true,
            requiredRootName: destinationWorkspaceDirectoryName
        )
    }

    func prepareDirectory(jobID: UUID) throws -> URL {
        Self.operationLock.lock()
        defer { Self.operationLock.unlock() }
        try validateRootIfPresent()
        removeExpired()
        try fileManager.createDirectory(at: rootURL, withIntermediateDirectories: true)
        try validateRootIfPresent()
        var mutableRootURL = rootURL
        var resourceValues = URLResourceValues()
        resourceValues.isExcludedFromBackup = true
        try? mutableRootURL.setResourceValues(resourceValues)
        let directoryURL = rootURL.appendingPathComponent(jobID.uuidString, isDirectory: true)
        if fileManager.fileExists(atPath: directoryURL.path) {
            try remove(directoryURL)
            try fileManager.createDirectory(at: rootURL, withIntermediateDirectories: true)
            try validateRootIfPresent()
        }
        try fileManager.createDirectory(at: directoryURL, withIntermediateDirectories: false)
        return directoryURL
    }

    func contains(_ fileURL: URL, in directoryURL: URL) -> Bool {
        guard rootIsSafe,
              isStrictDescendant(directoryURL.standardizedFileURL, of: rootURL) else {
            return false
        }
        let resolvedRootURL = rootURL.resolvingSymlinksInPath().standardizedFileURL
        let resolvedDirectoryURL = directoryURL.resolvingSymlinksInPath().standardizedFileURL
        guard isStrictDescendant(resolvedDirectoryURL, of: resolvedRootURL) else {
            return false
        }
        let filePath = fileURL.resolvingSymlinksInPath().standardizedFileURL.path
        let directoryPath = resolvedDirectoryURL.path
        let prefix = directoryPath.hasSuffix("/") ? directoryPath : "\(directoryPath)/"
        return filePath.hasPrefix(prefix)
    }

    func remove(_ directoryURL: URL) throws {
        Self.operationLock.lock()
        defer { Self.operationLock.unlock() }
        try validateRootIfPresent()
        guard isStrictDescendant(directoryURL.standardizedFileURL, of: rootURL) else {
            return
        }
        let isSymbolicLink = (try? directoryURL.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) == true
        if fileManager.fileExists(atPath: directoryURL.path) || isSymbolicLink {
            try fileManager.removeItem(at: directoryURL)
        }
        try removeRootIfEmpty()
    }

    func removeExpired(now: Date = Date()) {
        Self.operationLock.lock()
        defer { Self.operationLock.unlock() }
        guard let children = try? fileManager.contentsOfDirectory(
            at: rootURL,
            includingPropertiesForKeys: [.contentModificationDateKey],
            options: []
        ) else {
            return
        }
        for childURL in children {
            guard UUID(uuidString: childURL.lastPathComponent) != nil else {
                continue
            }
            let modifiedAt = try? childURL.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate
            guard let modifiedAt,
                  now.timeIntervalSince(modifiedAt) >= Self.expirationInterval else {
                continue
            }
            try? remove(childURL)
        }
    }

    private var rootIsSafe: Bool {
        do {
            try validateRootIfPresent()
            return true
        } catch {
            return false
        }
    }

    private func validateRootIfPresent() throws {
        if let requiredRootName, rootURL.lastPathComponent != requiredRootName {
            throw CocoaError(.fileWriteInvalidFileName)
        }
        guard fileManager.fileExists(atPath: rootURL.path) else {
            return
        }
        let values = try rootURL.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
        guard values.isDirectory == true, values.isSymbolicLink != true else {
            throw CocoaError(.fileWriteInvalidFileName)
        }
    }

    private func isStrictDescendant(_ candidateURL: URL, of rootURL: URL) -> Bool {
        let candidatePath = candidateURL.standardizedFileURL.path
        let rootPath = rootURL.standardizedFileURL.path
        let rootPrefix = rootPath.hasSuffix("/") ? rootPath : "\(rootPath)/"
        return candidatePath != rootPath && candidatePath.hasPrefix(rootPrefix)
    }

    private func removeRootIfEmpty() throws {
        guard removesRootWhenEmpty, fileManager.fileExists(atPath: rootURL.path) else {
            return
        }
        try validateRootIfPresent()
        let children = try fileManager.contentsOfDirectory(at: rootURL, includingPropertiesForKeys: nil)
        guard children.isEmpty else {
            return
        }
        try fileManager.removeItem(at: rootURL)
    }
}

final class PreviewArtifactLease {
    let artifact: PreviewArtifact
    let directoryURL: URL

    private let cache: PreviewCache
    private let cleanupFailureHandler: @Sendable () -> Void

    init(
        artifact: PreviewArtifact,
        directoryURL: URL,
        cache: PreviewCache,
        cleanupFailureHandler: @escaping @Sendable () -> Void = {}
    ) {
        self.artifact = artifact
        self.directoryURL = directoryURL
        self.cache = cache
        self.cleanupFailureHandler = cleanupFailureHandler
    }

    deinit {
        let cache = cache
        let directoryURL = directoryURL
        let cleanupFailureHandler = cleanupFailureHandler
        Task.detached(priority: .utility) {
            do {
                try cache.remove(directoryURL)
            } catch {
                cleanupFailureHandler()
            }
        }
    }

    var fileManager: FileManager {
        cache.fileManager
    }
}

private extension String {
    var nonEmpty: String? {
        isEmpty ? nil : self
    }
}

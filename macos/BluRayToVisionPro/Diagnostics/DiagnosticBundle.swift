import Foundation

enum DiagnosticBundleError: Error, LocalizedError {
    case manifestTooLarge
    case payloadTooLarge(actualBytes: Int, maximumBytes: Int)
    case archiveTooLarge(actualBytes: Int, maximumBytes: Int)
    case missingStorageDirectory

    var errorDescription: String? {
        switch self {
        case .manifestTooLarge:
            return "The support bundle manifest exceeded its size limit."
        case let .payloadTooLarge(actualBytes, maximumBytes):
            return "The support bundle payload is \(actualBytes) bytes; the limit is \(maximumBytes) bytes."
        case let .archiveTooLarge(actualBytes, maximumBytes):
            return "The compressed support bundle is \(actualBytes) bytes; the limit is \(maximumBytes) bytes."
        case .missingStorageDirectory:
            return "The app could not locate a directory for the support bundle."
        }
    }
}

struct DiagnosticBundleFilePreview: Equatable {
    let name: String
    let uncompressedBytes: Int
    let truncated: Bool
}

struct DiagnosticBundlePreview: Equatable {
    let includedCategories: [String]
    let excludedCategories: [String]
    let files: [DiagnosticBundleFilePreview]
    let truncationNotices: [String]
    let archiveBytes: Int
    let maximumArchiveBytes: Int
}

struct DiagnosticBundleArtifact {
    let bundleID: UUID
    let createdAt: Date
    let archiveURL: URL
    let suggestedFilename: String
    let preview: DiagnosticBundlePreview

    var sharingItems: [URL] { [archiveURL] }

    @discardableResult
    func saveCopy(
        to destinationURL: URL,
        overwrite: Bool = false,
        fileManager: FileManager = .default
    ) throws -> URL {
        let resolvedURL = destinationURL.hasDirectoryPath
            ? destinationURL.appendingPathComponent(suggestedFilename, isDirectory: false)
            : destinationURL
        if fileManager.fileExists(atPath: resolvedURL.path) {
            guard overwrite else {
                throw CocoaError(.fileWriteFileExists)
            }
            try fileManager.removeItem(at: resolvedURL)
        }
        try fileManager.copyItem(at: archiveURL, to: resolvedURL)
        return resolvedURL
    }

    func removeLocalCopy(fileManager: FileManager = .default) throws {
        if fileManager.fileExists(atPath: archiveURL.path) {
            try fileManager.removeItem(at: archiveURL)
        }
    }
}

struct DiagnosticRuntimeMetadata: Equatable {
    let appVersion: String
    let appBuild: String
    let distributionChannel: String?
    let operatingSystemVersion: String
    let architecture: String

    static func current(bundle: Bundle = .main, processInfo: ProcessInfo = .processInfo) -> DiagnosticRuntimeMetadata {
        let version = bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "development"
        let build = bundle.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "development"
        let channel = bundle.object(forInfoDictionaryKey: "BDToAVPDistributionChannel") as? String
        let operatingSystem = processInfo.operatingSystemVersion
        return DiagnosticRuntimeMetadata(
            appVersion: version,
            appBuild: build,
            distributionChannel: channel,
            operatingSystemVersion: "\(operatingSystem.majorVersion).\(operatingSystem.minorVersion).\(operatingSystem.patchVersion)",
            architecture: Self.currentArchitecture
        )
    }

    private static var currentArchitecture: String {
        #if arch(arm64)
        return "arm64"
        #elseif arch(x86_64)
        return "x86_64"
        #else
        return "unknown"
        #endif
    }
}

private enum DiagnosticSizeRounding {
    static let fileSizeQuantumBytes: Int64 = 256 * 1_024 * 1_024
    static let volumeCapacityQuantumBytes: Int64 = 16 * 1_024 * 1_024 * 1_024

    static func fileSize(_ bytes: Int64?) -> Int64? {
        roundedDown(bytes, quantum: fileSizeQuantumBytes)
    }

    static func volumeCapacity(_ bytes: Int64?) -> Int64? {
        roundedDown(bytes, quantum: volumeCapacityQuantumBytes)
    }

    private static func roundedDown(_ bytes: Int64?, quantum: Int64) -> Int64? {
        guard let bytes, bytes >= 0 else {
            return nil
        }
        return bytes - (bytes % quantum)
    }
}

final class DiagnosticBundleBuilder {
    struct Configuration {
        let maximumArchiveBytes: Int
        let maximumUncompressedBytes: Int
        let maximumManifestBytes: Int
        let maximumEventsBytes: Int
        let maximumStorageBytes: Int
        let maximumToolTailBytes: Int

        static let production = Configuration(
            maximumArchiveBytes: 2 * 1_024 * 1_024,
            maximumUncompressedBytes: 1_500_000,
            maximumManifestBytes: 64 * 1_024,
            maximumEventsBytes: 320 * 1_024,
            maximumStorageBytes: 160 * 1_024,
            maximumToolTailBytes: 640 * 1_024
        )
    }

    private let configuration: Configuration
    private let storageProbe: any DiagnosticStorageProbing
    private let fileManager: FileManager
    private let bundleIDProvider: () -> UUID
    private let runtimeMetadataProvider: () -> DiagnosticRuntimeMetadata

    init(
        configuration: Configuration = .production,
        storageProbe: any DiagnosticStorageProbing = FileSystemDiagnosticStorageProbe(),
        fileManager: FileManager = .default,
        bundleIDProvider: @escaping () -> UUID = UUID.init,
        runtimeMetadataProvider: @escaping () -> DiagnosticRuntimeMetadata = { DiagnosticRuntimeMetadata.current() }
    ) {
        self.configuration = configuration
        self.storageProbe = storageProbe
        self.fileManager = fileManager
        self.bundleIDProvider = bundleIDProvider
        self.runtimeMetadataProvider = runtimeMetadataProvider
    }

    func createBundle(
        from snapshot: DiagnosticCaptureSnapshot,
        outputDirectory: URL? = nil
    ) throws -> DiagnosticBundleArtifact {
        let bundleID = bundleIDProvider()
        let redactor = DiagnosticRedactor(bundleID: bundleID)
        preRegisterSensitiveValues(from: snapshot, redactor: redactor)

        let currentStorageProbes = snapshot.jobContext?.storageTargets.map { role, url in
            storageProbe.probe(role: role, url: url, capturedAt: snapshot.capturedAt)
        } ?? []
        let eventResult = try buildEvents(snapshot.events, redactor: redactor)
        let storageResult = try buildStorage(
            probes: currentStorageProbes,
            samples: snapshot.storageSamples,
            totalSamples: snapshot.totalStorageSamples,
            historyDroppedSamples: snapshot.droppedStorageSamples,
            capturedAt: snapshot.capturedAt,
            redactor: redactor
        )
        let toolTailResult = buildToolTail(snapshot.process.toolOutput, redactor: redactor)
        let manifest = makeManifest(
            bundleID: bundleID,
            snapshot: snapshot,
            runtime: runtimeMetadataProvider(),
            redactor: redactor,
            eventResult: eventResult,
            storageResult: storageResult,
            toolTailResult: toolTailResult
        )
        let manifestData = try Self.encode(manifest, prettyPrinted: true)
        guard manifestData.count <= configuration.maximumManifestBytes else {
            throw DiagnosticBundleError.manifestTooLarge
        }

        let entries = [
            DiagnosticZipArchive.Entry(name: "manifest.json", data: manifestData),
            DiagnosticZipArchive.Entry(name: "events.jsonl", data: eventResult.data),
            DiagnosticZipArchive.Entry(name: "storage.json", data: storageResult.data),
            DiagnosticZipArchive.Entry(name: "tool-tail.txt", data: toolTailResult.data),
        ]
        let uncompressedBytes = entries.reduce(0) { $0 + $1.data.count }
        guard uncompressedBytes <= configuration.maximumUncompressedBytes else {
            throw DiagnosticBundleError.payloadTooLarge(
                actualBytes: uncompressedBytes,
                maximumBytes: configuration.maximumUncompressedBytes
            )
        }
        let archiveData = try DiagnosticZipArchive.data(
            entries: entries,
            modificationDate: snapshot.capturedAt
        )
        guard archiveData.count <= configuration.maximumArchiveBytes else {
            throw DiagnosticBundleError.archiveTooLarge(
                actualBytes: archiveData.count,
                maximumBytes: configuration.maximumArchiveBytes
            )
        }

        let directory = try outputDirectory ?? defaultOutputDirectory()
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        let filename = Self.suggestedFilename(createdAt: snapshot.capturedAt, bundleID: bundleID)
        let archiveURL = directory.appendingPathComponent(filename, isDirectory: false)
        try archiveData.write(to: archiveURL, options: .atomic)

        let privacy = Self.privacyManifest
        let files = [
            DiagnosticBundleFilePreview(name: "manifest.json", uncompressedBytes: manifestData.count, truncated: false),
            DiagnosticBundleFilePreview(
                name: "events.jsonl",
                uncompressedBytes: eventResult.data.count,
                truncated: eventResult.truncation.truncated
            ),
            DiagnosticBundleFilePreview(
                name: "storage.json",
                uncompressedBytes: storageResult.data.count,
                truncated: storageResult.truncation.truncated
            ),
            DiagnosticBundleFilePreview(
                name: "tool-tail.txt",
                uncompressedBytes: toolTailResult.data.count,
                truncated: toolTailResult.truncation.truncated
            ),
        ]
        var truncationNotices: [String] = []
        if eventResult.truncation.truncated {
            truncationNotices.append("Older diagnostic events were omitted.")
        }
        if storageResult.truncation.truncated {
            truncationNotices.append("Older storage samples were omitted.")
        }
        if toolTailResult.truncation.truncated {
            truncationNotices.append("Older tool output was omitted.")
        }
        return DiagnosticBundleArtifact(
            bundleID: bundleID,
            createdAt: snapshot.capturedAt,
            archiveURL: archiveURL,
            suggestedFilename: filename,
            preview: DiagnosticBundlePreview(
                includedCategories: privacy.included,
                excludedCategories: privacy.excluded,
                files: files,
                truncationNotices: truncationNotices,
                archiveBytes: archiveData.count,
                maximumArchiveBytes: configuration.maximumArchiveBytes
            )
        )
    }

    private func preRegisterSensitiveValues(
        from snapshot: DiagnosticCaptureSnapshot,
        redactor: DiagnosticRedactor
    ) {
        for context in snapshot.redactionContexts {
            _ = redactor.jobToken(for: context.jobID)
            for path in context.redactionPaths {
                _ = redactor.pathToken(for: path)
            }
            for (role, url) in context.storageTargets {
                let token = redactor.pathToken(for: url)
                if role == .source || role == .output {
                    redactor.registerSensitiveName(url.lastPathComponent, replacement: token)
                }
            }
            for name in context.sensitiveNames {
                redactor.registerSensitiveName(name)
            }
        }
        if let inspection = snapshot.lifecycle.result {
            redactor.registerSensitiveName(inspection.name)
            for title in inspection.titles {
                redactor.registerSensitiveName(title.name)
                redactor.registerSensitiveName(title.outputName)
            }
        }
        if let outputPath = snapshot.lifecycle.conversionResult?.outputPath {
            _ = redactor.pathToken(for: outputPath)
        }
        for event in snapshot.events.entries {
            _ = redactor.jobToken(for: event.jobID)
        }
        for sample in snapshot.storageSamples {
            _ = redactor.pathToken(for: sample.url)
        }
    }

    private func buildEvents(
        _ history: DiagnosticEventHistorySnapshot,
        redactor: DiagnosticRedactor
    ) throws -> EventBuildResult {
        let encodedEvents = try history.entries.map { event in
            let sanitized = BundleEvent(event: event, redactor: redactor)
            var data = try Self.encode(sanitized, prettyPrinted: false)
            data.append(0x0A)
            return EncodedEvent(data: data, textTruncated: sanitized.textTruncated)
        }
        var selected: [EncodedEvent] = []
        var selectedBytes = 0
        for event in encodedEvents.reversed() {
            guard selectedBytes + event.data.count <= configuration.maximumEventsBytes else {
                break
            }
            selected.append(event)
            selectedBytes += event.data.count
        }
        selected.reverse()
        var data = Data()
        for event in selected {
            data.append(event.data)
        }
        let archiveDropped = encodedEvents.count - selected.count
        let fieldsTruncated = selected.filter(\.textTruncated).count
        let truncation = BundleEventTruncation(
            totalRecorded: history.totalRecordedEntries,
            included: selected.count,
            historyDropped: history.droppedEntries,
            historyDroppedBytes: history.droppedBytes,
            archiveDropped: archiveDropped,
            fieldsTruncated: fieldsTruncated,
            truncated: history.droppedEntries > 0 || archiveDropped > 0 || fieldsTruncated > 0
        )
        return EventBuildResult(data: data, truncation: truncation)
    }

    private func buildStorage(
        probes: [RawDiagnosticStorageProbe],
        samples: [RawDiagnosticStorageSample],
        totalSamples: Int,
        historyDroppedSamples: Int,
        capturedAt: Date,
        redactor: DiagnosticRedactor
    ) throws -> StorageBuildResult {
        let sanitizedProbes = probes.map { BundleStorageProbe(probe: $0, redactor: redactor) }
        let sanitizedSamples = samples.map { BundleStorageSample(sample: $0, redactor: redactor) }
        var includedSamples = sanitizedSamples
        var archiveDropped = 0
        var document: BundleStorageDocument
        var encoded: Data
        repeat {
            let truncation = BundleStorageTruncation(
                totalSamples: totalSamples,
                includedSamples: includedSamples.count,
                historyDropped: historyDroppedSamples,
                archiveDropped: archiveDropped,
                truncated: historyDroppedSamples > 0 || archiveDropped > 0
            )
            document = BundleStorageDocument(
                schemaVersion: 1,
                capturedAt: Self.timestamp(capturedAt),
                probes: sanitizedProbes,
                samples: includedSamples,
                truncation: truncation
            )
            encoded = try Self.encode(document, prettyPrinted: true)
            if encoded.count > configuration.maximumStorageBytes, !includedSamples.isEmpty {
                includedSamples.removeFirst()
                archiveDropped += 1
            } else {
                break
            }
        } while true
        guard encoded.count <= configuration.maximumStorageBytes else {
            throw DiagnosticBundleError.payloadTooLarge(
                actualBytes: encoded.count,
                maximumBytes: configuration.maximumStorageBytes
            )
        }
        return StorageBuildResult(data: encoded, truncation: document.truncation)
    }

    private func buildToolTail(
        _ snapshot: DiagnosticTextSnapshot,
        redactor: DiagnosticRedactor
    ) -> ToolTailBuildResult {
        let redactedText = redactor.redact(snapshot.text)
        let bodyBudget = max(0, configuration.maximumToolTailBytes - 512)
        let boundedBody = Self.boundedUTF8Suffix(redactedText, maximumBytes: bodyBudget)
        let archiveDropped = max(0, redactedText.utf8.count - boundedBody.utf8.count)
        let truncation = BundleToolTailTruncation(
            totalInputBytes: snapshot.totalBytes,
            retainedInputBytes: snapshot.retainedBytes,
            bufferDroppedBytes: snapshot.droppedBytes,
            archiveDroppedBytes: archiveDropped,
            truncated: snapshot.truncated || archiveDropped > 0
        )
        let header = [
            "# bd_to_avp_support_tool_tail schema_version=1",
            "# total_input_bytes=\(truncation.totalInputBytes)",
            "# retained_input_bytes=\(truncation.retainedInputBytes)",
            "# buffer_dropped_bytes=\(truncation.bufferDroppedBytes)",
            "# archive_dropped_bytes=\(truncation.archiveDroppedBytes)",
            "# truncated=\(truncation.truncated)",
            "",
        ]
        .joined(separator: "\n")
        let data = Data((header + boundedBody).utf8)
        return ToolTailBuildResult(data: data, truncation: truncation)
    }

    private func makeManifest(
        bundleID: UUID,
        snapshot: DiagnosticCaptureSnapshot,
        runtime: DiagnosticRuntimeMetadata,
        redactor: DiagnosticRedactor,
        eventResult: EventBuildResult,
        storageResult: StorageBuildResult,
        toolTailResult: ToolTailBuildResult
    ) -> BundleManifest {
        let lifecycle = snapshot.lifecycle
        let job = snapshot.jobContext.map { context in
            BundleJob(
                token: redactor.jobToken(for: context.jobID)!,
                operation: context.operation,
                sourceKind: context.sourceKind,
                sourcePathToken: redactor.pathToken(for: context.sourceURL),
                destinationPathToken: context.destinationURL.map { redactor.pathToken(for: $0) },
                outputPathToken: context.outputURL.map { redactor.pathToken(for: $0) },
                settings: context.settings
            )
        }
        let payloadBytes = eventResult.data.count + storageResult.data.count + toolTailResult.data.count
        return BundleManifest(
            schemaVersion: 1,
            bundleID: bundleID.uuidString.lowercased(),
            createdAt: Self.timestamp(snapshot.capturedAt),
            archive: BundleArchiveContract(
                format: "zip-deflate",
                maximumCompressedBytes: configuration.maximumArchiveBytes,
                maximumUncompressedBytes: configuration.maximumUncompressedBytes,
                payloadBytesExcludingManifest: payloadBytes
            ),
            app: BundleApp(
                version: runtime.appVersion,
                build: runtime.appBuild,
                distributionChannel: runtime.distributionChannel,
                operatingSystemVersion: runtime.operatingSystemVersion,
                architecture: runtime.architecture,
                workerProtocolVersion: WorkerJobSpec.protocolVersion
            ),
            worker: BundleWorker(
                version: redactor.redact(snapshot.workerVersion),
                active: snapshot.process.isRunning,
                cancellationRequested: snapshot.process.cancellationRequested
            ),
            lifecycle: BundleLifecycle(
                phase: lifecycle.phase.rawValue,
                operation: lifecycle.operationKind == .inspection ? "inspection" : "conversion",
                activeMode: snapshot.activeMode,
                jobToken: redactor.jobToken(for: lifecycle.jobID),
                stage: Self.boundedRedacted(lifecycle.stageMessage, maximumBytes: 2_048, redactor: redactor),
                activity: Self.boundedRedacted(lifecycle.activityMessage, maximumBytes: 4_096, redactor: redactor),
                warning: Self.boundedRedacted(lifecycle.warningMessage, maximumBytes: 4_096, redactor: redactor),
                elapsedSeconds: lifecycle.elapsedSeconds,
                progress: lifecycle.progress.map(BundleProgress.init),
                failureCode: redactor.redact(lifecycle.failureCode),
                failureMessage: Self.boundedRedacted(lifecycle.failureMessage, maximumBytes: 4_096, redactor: redactor),
                failureDetails: Self.boundedRedacted(lifecycle.failureDetails, maximumBytes: 8_192, redactor: redactor),
                failureRetryable: lifecycle.failureRetryable,
                recoveryDecision: lifecycle.recoveryDecision.map {
                    BundleRecoveryDecision(
                        identifier: redactor.redact($0.identifier),
                        choices: $0.choices.map { redactor.redact($0) }
                    )
                }
            ),
            job: job,
            batch: snapshot.batchSummary.map(BundleBatch.init),
            files: [
                BundleFile(
                    name: "events.jsonl",
                    uncompressedBytes: eventResult.data.count,
                    truncated: eventResult.truncation.truncated
                ),
                BundleFile(
                    name: "storage.json",
                    uncompressedBytes: storageResult.data.count,
                    truncated: storageResult.truncation.truncated
                ),
                BundleFile(
                    name: "tool-tail.txt",
                    uncompressedBytes: toolTailResult.data.count,
                    truncated: toolTailResult.truncation.truncated
                ),
            ],
            truncation: BundleTruncation(
                events: eventResult.truncation,
                storage: storageResult.truncation,
                toolTail: toolTailResult.truncation
            ),
            privacy: Self.privacyManifest
        )
    }

    private func defaultOutputDirectory() throws -> URL {
        guard let applicationSupport = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first else {
            throw DiagnosticBundleError.missingStorageDirectory
        }
        let identifier = Bundle.main.bundleIdentifier ?? "com.shinycomputers.bd-to-avp"
        return applicationSupport
            .appendingPathComponent(identifier, isDirectory: true)
            .appendingPathComponent("Support Bundles", isDirectory: true)
    }

    private static func encode<T: Encodable>(_ value: T, prettyPrinted: Bool) throws -> Data {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = prettyPrinted
            ? [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
            : [.sortedKeys, .withoutEscapingSlashes]
        return try encoder.encode(value)
    }

    fileprivate static func timestamp(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }

    private static func suggestedFilename(createdAt: Date, bundleID: UUID) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyyMMdd'T'HHmmss'Z'"
        let identifier = String(bundleID.uuidString.replacingOccurrences(of: "-", with: "").prefix(8)).lowercased()
        return "BD-to-AVP-Support-\(formatter.string(from: createdAt))-\(identifier).zip"
    }

    private static func boundedRedacted(
        _ value: String?,
        maximumBytes: Int,
        redactor: DiagnosticRedactor
    ) -> String? {
        guard let value else {
            return nil
        }
        return boundedUTF8Prefix(redactor.redact(value), maximumBytes: maximumBytes).value
    }

    fileprivate static func boundedUTF8Prefix(
        _ value: String,
        maximumBytes: Int
    ) -> (value: String, truncated: Bool) {
        let data = Data(value.utf8)
        guard data.count > maximumBytes else {
            return (value, false)
        }
        var count = maximumBytes
        while count > 0 {
            let prefix = Data(data.prefix(count))
            if let decoded = String(data: prefix, encoding: .utf8) {
                return (decoded + "<truncated>", true)
            }
            count -= 1
        }
        return ("<truncated>", true)
    }

    private static func boundedUTF8Suffix(_ value: String, maximumBytes: Int) -> String {
        let data = Data(value.utf8)
        guard data.count > maximumBytes else {
            return value
        }
        var start = data.count - maximumBytes
        while start < data.count, data[start] & 0b1100_0000 == 0b1000_0000 {
            start += 1
        }
        return String(decoding: data[start...], as: UTF8.self)
    }

    private static let privacyManifest = BundlePrivacy(
        rulesVersion: 2,
        pathTokenScope: "bundle",
        sizeRoundingMode: "down",
        fileSizeQuantumBytes: DiagnosticSizeRounding.fileSizeQuantumBytes,
        volumeCapacityQuantumBytes: DiagnosticSizeRounding.volumeCapacityQuantumBytes,
        included: [
            "app/build and worker protocol versions",
            "worker lifecycle, stage, heartbeat, progress, warnings, and terminal state",
            "selected non-identifying conversion settings",
            "source kind and per-bundle path correlation tokens",
            "coarsely rounded destination capacity and artifact size, accessibility, and modification age",
            "bounded redacted worker tool-output tail",
            "batch status counts and retry/cancellation transitions",
        ],
        excluded: [
            "source media and generated media contents",
            "screenshots",
            "raw full paths, filenames, volume names, and movie titles",
            "raw job requests and command arguments",
            "environment variables and credentials",
            "raw process identifiers",
            "hardware serial numbers and reusable file hashes",
        ]
    )
}

private struct EncodedEvent {
    let data: Data
    let textTruncated: Bool
}

private struct EventBuildResult {
    let data: Data
    let truncation: BundleEventTruncation
}

private struct StorageBuildResult {
    let data: Data
    let truncation: BundleStorageTruncation
}

private struct ToolTailBuildResult {
    let data: Data
    let truncation: BundleToolTailTruncation
}

private struct BundleManifest: Encodable {
    let schemaVersion: Int
    let bundleID: String
    let createdAt: String
    let archive: BundleArchiveContract
    let app: BundleApp
    let worker: BundleWorker
    let lifecycle: BundleLifecycle
    let job: BundleJob?
    let batch: BundleBatch?
    let files: [BundleFile]
    let truncation: BundleTruncation
    let privacy: BundlePrivacy
}

private struct BundleArchiveContract: Encodable {
    let format: String
    let maximumCompressedBytes: Int
    let maximumUncompressedBytes: Int
    let payloadBytesExcludingManifest: Int
}

private struct BundleApp: Encodable {
    let version: String
    let build: String
    let distributionChannel: String?
    let operatingSystemVersion: String
    let architecture: String
    let workerProtocolVersion: Int
}

private struct BundleWorker: Encodable {
    let version: String?
    let active: Bool
    let cancellationRequested: Bool
}

private struct BundleLifecycle: Encodable {
    let phase: String
    let operation: String
    let activeMode: String?
    let jobToken: String?
    let stage: String?
    let activity: String?
    let warning: String?
    let elapsedSeconds: Int
    let progress: BundleProgress?
    let failureCode: String?
    let failureMessage: String?
    let failureDetails: String?
    let failureRetryable: Bool
    let recoveryDecision: BundleRecoveryDecision?
}

private struct BundleProgress: Encodable {
    let currentStage: Int
    let totalStages: Int
    let stageFraction: Double?

    init(_ progress: WorkerProgress) {
        currentStage = progress.currentStage
        totalStages = progress.totalStages
        stageFraction = progress.stageFraction
    }
}

private struct BundleRecoveryDecision: Encodable {
    let identifier: String
    let choices: [String]
}

private struct BundleJob: Encodable {
    let token: String
    let operation: String
    let sourceKind: String
    let sourcePathToken: String
    let destinationPathToken: String?
    let outputPathToken: String?
    let settings: DiagnosticJobSettings?
}

private struct BundleBatch: Encodable {
    let kind: String
    let totalItems: Int
    let activeItems: Int
    let statusCounts: [String: Int]

    init(_ summary: DiagnosticBatchSummary) {
        kind = summary.kind
        totalItems = summary.totalItems
        activeItems = summary.activeItems
        statusCounts = summary.statusCounts
    }
}

private struct BundleFile: Encodable {
    let name: String
    let uncompressedBytes: Int
    let truncated: Bool
}

private struct BundleTruncation: Encodable {
    let events: BundleEventTruncation
    let storage: BundleStorageTruncation
    let toolTail: BundleToolTailTruncation
}

private struct BundleEventTruncation: Encodable {
    let totalRecorded: Int
    let included: Int
    let historyDropped: Int
    let historyDroppedBytes: Int
    let archiveDropped: Int
    let fieldsTruncated: Int
    let truncated: Bool
}

private struct BundleStorageTruncation: Encodable {
    let totalSamples: Int
    let includedSamples: Int
    let historyDropped: Int
    let archiveDropped: Int
    let truncated: Bool
}

private struct BundleToolTailTruncation: Encodable {
    let totalInputBytes: Int
    let retainedInputBytes: Int
    let bufferDroppedBytes: Int
    let archiveDroppedBytes: Int
    let truncated: Bool
}

private struct BundlePrivacy: Encodable {
    let rulesVersion: Int
    let pathTokenScope: String
    let sizeRoundingMode: String
    let fileSizeQuantumBytes: Int64
    let volumeCapacityQuantumBytes: Int64
    let included: [String]
    let excluded: [String]
}

private struct BundleEvent: Encodable {
    let schemaVersion = 1
    let recordedAt: String
    let source: String
    let name: String
    let jobToken: String?
    let sequence: Int?
    let phase: String
    let operation: String?
    let activeMode: String?
    let stage: String?
    let message: String?
    let details: String?
    let level: String?
    let elapsedSeconds: Int?
    let progress: DiagnosticProgressSnapshot?
    let warningCode: String?
    let failureCode: String?
    let retryable: Bool?
    let choices: [String]?
    let resultSizeRoundedBytes: Int64?
    let workerVersion: String?
    let exitStatus: Int32?
    let textTruncated: Bool

    init(event: DiagnosticEventRecord, redactor: DiagnosticRedactor) {
        var didTruncate = false
        func bounded(_ value: String?, maximumBytes: Int) -> String? {
            guard let value else {
                return nil
            }
            let result = DiagnosticBundleBuilder.boundedUTF8Prefix(
                redactor.redact(value),
                maximumBytes: maximumBytes
            )
            didTruncate = didTruncate || result.truncated
            return result.value
        }

        recordedAt = DiagnosticBundleBuilder.timestamp(event.recordedAt)
        source = bounded(event.source, maximumBytes: 128) ?? "unknown"
        name = bounded(event.name, maximumBytes: 256) ?? "unknown"
        jobToken = redactor.jobToken(for: event.jobID)
        sequence = event.sequence
        phase = bounded(event.phase, maximumBytes: 128) ?? "unknown"
        operation = bounded(event.operation, maximumBytes: 256)
        activeMode = bounded(event.activeMode, maximumBytes: 256)
        stage = bounded(event.stage, maximumBytes: 1_024)
        message = bounded(event.message, maximumBytes: 4_096)
        details = bounded(event.details, maximumBytes: 8_192)
        level = bounded(event.level, maximumBytes: 128)
        elapsedSeconds = event.elapsedSeconds
        progress = event.progress
        warningCode = bounded(event.warningCode, maximumBytes: 512)
        failureCode = bounded(event.failureCode, maximumBytes: 512)
        retryable = event.retryable
        choices = event.choices?.map { bounded($0, maximumBytes: 512) ?? "<redacted>" }
        resultSizeRoundedBytes = DiagnosticSizeRounding.fileSize(event.resultSizeBytes)
        workerVersion = bounded(event.workerVersion, maximumBytes: 512)
        exitStatus = event.exitStatus
        textTruncated = didTruncate
    }
}

private struct BundleStorageDocument: Encodable {
    let schemaVersion: Int
    let capturedAt: String
    let probes: [BundleStorageProbe]
    let samples: [BundleStorageSample]
    let truncation: BundleStorageTruncation
}

private struct BundleStorageProbe: Encodable {
    let capturedAt: String
    let role: String
    let pathToken: String
    let status: String
    let isDirectory: Bool?
    let isReadable: Bool?
    let isWritable: Bool?
    let fileSizeRoundedBytes: Int64?
    let modificationAgeSeconds: Int64?
    let volumeAvailableRoundedBytes: Int64?
    let volumeTotalRoundedBytes: Int64?
    let volumeReadOnly: Bool?
    let errorKind: String?

    init(probe: RawDiagnosticStorageProbe, redactor: DiagnosticRedactor) {
        capturedAt = DiagnosticBundleBuilder.timestamp(probe.capturedAt)
        role = probe.role.rawValue
        pathToken = redactor.pathToken(for: probe.url)
        status = probe.status.rawValue
        isDirectory = probe.isDirectory
        isReadable = probe.isReadable
        isWritable = probe.isWritable
        fileSizeRoundedBytes = DiagnosticSizeRounding.fileSize(probe.fileSizeBytes)
        modificationAgeSeconds = probe.modificationAgeSeconds
        volumeAvailableRoundedBytes = DiagnosticSizeRounding.volumeCapacity(probe.volumeAvailableBytes)
        volumeTotalRoundedBytes = DiagnosticSizeRounding.volumeCapacity(probe.volumeTotalBytes)
        volumeReadOnly = probe.volumeReadOnly
        errorKind = probe.errorKind?.rawValue
    }
}

private struct BundleStorageSample: Encodable {
    let capturedAt: String
    let role: String
    let pathToken: String
    let status: String
    let fileSizeRoundedBytes: Int64?
    let modificationAgeSeconds: Int64?
    let volumeAvailableRoundedBytes: Int64?

    init(sample: RawDiagnosticStorageSample, redactor: DiagnosticRedactor) {
        capturedAt = DiagnosticBundleBuilder.timestamp(sample.capturedAt)
        role = sample.role.rawValue
        pathToken = redactor.pathToken(for: sample.url)
        status = sample.status.rawValue
        fileSizeRoundedBytes = DiagnosticSizeRounding.fileSize(sample.fileSizeBytes)
        modificationAgeSeconds = sample.modificationAgeSeconds
        volumeAvailableRoundedBytes = DiagnosticSizeRounding.volumeCapacity(sample.volumeAvailableBytes)
    }
}

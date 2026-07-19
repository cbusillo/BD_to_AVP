import Darwin
import Foundation

struct DiagnosticTextSnapshot: Equatable, Sendable {
    let text: String
    let retainedBytes: Int
    let totalBytes: Int
    let droppedBytes: Int

    var truncated: Bool { droppedBytes > 0 }

    static let empty = DiagnosticTextSnapshot(
        text: "",
        retainedBytes: 0,
        totalBytes: 0,
        droppedBytes: 0
    )
}

final class BoundedDiagnosticTextBuffer: @unchecked Sendable {
    private let maximumBytes: Int
    private let lock = NSLock()
    private var data = Data()
    private var totalBytes = 0

    init(maximumBytes: Int) {
        precondition(maximumBytes > 0)
        self.maximumBytes = maximumBytes
    }

    func append(_ incomingData: Data) {
        guard !incomingData.isEmpty else {
            return
        }
        lock.withDiagnosticLock {
            appendLocked(incomingData)
        }
    }

    func appendLine(_ line: String) {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return
        }
        lock.withDiagnosticLock {
            let separator = data.isEmpty ? "" : "\n"
            let incomingData = Data((separator + trimmed).utf8)
            appendLocked(incomingData)
        }
    }

    func reset() {
        lock.withDiagnosticLock {
            data.removeAll(keepingCapacity: true)
            totalBytes = 0
        }
    }

    func snapshot() -> DiagnosticTextSnapshot {
        lock.withDiagnosticLock {
            let text = String(decoding: data, as: UTF8.self)
                .trimmingCharacters(in: .newlines)
            return DiagnosticTextSnapshot(
                text: text,
                retainedBytes: data.count,
                totalBytes: totalBytes,
                droppedBytes: max(0, totalBytes - data.count)
            )
        }
    }

    private func appendLocked(_ incomingData: Data) {
        let (updatedTotal, overflowed) = totalBytes.addingReportingOverflow(incomingData.count)
        totalBytes = overflowed ? Int.max : updatedTotal

        if incomingData.count >= maximumBytes {
            data = Self.validUTF8Suffix(incomingData, maximumBytes: maximumBytes)
            return
        }

        let overflow = max(0, data.count + incomingData.count - maximumBytes)
        if overflow > 0 {
            data.removeFirst(overflow)
            trimLeadingUTF8ContinuationBytes()
        }
        data.append(incomingData)
    }

    private func trimLeadingUTF8ContinuationBytes() {
        while let firstByte = data.first, firstByte & 0b1100_0000 == 0b1000_0000 {
            data.removeFirst()
        }
    }

    private static func validUTF8Suffix(_ source: Data, maximumBytes: Int) -> Data {
        var suffix = Data(source.suffix(maximumBytes))
        while let firstByte = suffix.first, firstByte & 0b1100_0000 == 0b1000_0000 {
            suffix.removeFirst()
        }
        return suffix
    }
}

struct WorkerProcessDiagnosticSnapshot: Equatable, Sendable {
    let isRunning: Bool
    let processIdentifier: Int32?
    let processGroupIdentifier: Int32?
    let cancellationRequested: Bool
    let toolOutput: DiagnosticTextSnapshot

    static let empty = WorkerProcessDiagnosticSnapshot(
        isRunning: false,
        processIdentifier: nil,
        processGroupIdentifier: nil,
        cancellationRequested: false,
        toolOutput: .empty
    )
}

enum DiagnosticStorageRole: String, Codable, CaseIterable, Sendable {
    case source
    case destination
    case output
}

enum DiagnosticStorageStatus: String, Codable, Sendable {
    case available
    case missing
    case inaccessible
    case error
}

enum DiagnosticStorageErrorKind: String, Codable, Sendable {
    case permissionDenied = "permission_denied"
    case inputOutput = "input_output"
    case unknown
}

struct RawDiagnosticStorageProbe: Equatable, Sendable {
    let capturedAt: Date
    let role: DiagnosticStorageRole
    let url: URL
    let status: DiagnosticStorageStatus
    let isDirectory: Bool?
    let isReadable: Bool?
    let isWritable: Bool?
    let fileSizeBytes: Int64?
    let modificationAgeSeconds: Int64?
    let volumeAvailableBytes: Int64?
    let volumeTotalBytes: Int64?
    let volumeReadOnly: Bool?
    let errorKind: DiagnosticStorageErrorKind?
}

struct RawDiagnosticStorageSample: Equatable, Sendable {
    let capturedAt: Date
    let role: DiagnosticStorageRole
    let url: URL
    let status: DiagnosticStorageStatus
    let fileSizeBytes: Int64?
    let modificationAgeSeconds: Int64?
    let volumeAvailableBytes: Int64?

    init(probe: RawDiagnosticStorageProbe) {
        capturedAt = probe.capturedAt
        role = probe.role
        url = probe.url
        status = probe.status
        fileSizeBytes = probe.fileSizeBytes
        modificationAgeSeconds = probe.modificationAgeSeconds
        volumeAvailableBytes = probe.volumeAvailableBytes
    }
}

protocol DiagnosticStorageProbing: Sendable {
    func probe(role: DiagnosticStorageRole, url: URL, capturedAt: Date) -> RawDiagnosticStorageProbe
}

struct FileSystemDiagnosticStorageProbe: DiagnosticStorageProbing {
    typealias ResourceReader = @Sendable (URL, Set<URLResourceKey>) throws -> URLResourceValues

    private static let itemKeys: Set<URLResourceKey> = [
        .isDirectoryKey,
        .isReadableKey,
        .isWritableKey,
        .fileSizeKey,
        .contentModificationDateKey,
        .volumeAvailableCapacityForImportantUsageKey,
        .volumeTotalCapacityKey,
        .volumeIsReadOnlyKey,
    ]
    private static let volumeKeys: Set<URLResourceKey> = [
        .volumeAvailableCapacityForImportantUsageKey,
        .volumeTotalCapacityKey,
        .volumeIsReadOnlyKey,
    ]

    private let resourceReader: ResourceReader

    init(
        resourceReader: @escaping ResourceReader = { url, keys in
            try url.resourceValues(forKeys: keys)
        }
    ) {
        self.resourceReader = resourceReader
    }

    func probe(role: DiagnosticStorageRole, url: URL, capturedAt: Date) -> RawDiagnosticStorageProbe {
        let normalizedURL = url.standardizedFileURL
        do {
            let values = try resourceReader(normalizedURL, Self.itemKeys)
            let modificationAge = values.contentModificationDate.map {
                Int64(max(0, capturedAt.timeIntervalSince($0)).rounded(.down))
            }
            return RawDiagnosticStorageProbe(
                capturedAt: capturedAt,
                role: role,
                url: normalizedURL,
                status: .available,
                isDirectory: values.isDirectory,
                isReadable: values.isReadable,
                isWritable: values.isWritable,
                fileSizeBytes: values.fileSize.map(Int64.init),
                modificationAgeSeconds: modificationAge,
                volumeAvailableBytes: values.volumeAvailableCapacityForImportantUsage,
                volumeTotalBytes: values.volumeTotalCapacity.map(Int64.init),
                volumeReadOnly: values.volumeIsReadOnly,
                errorKind: nil
            )
        } catch {
            let errorKind = Self.errorKind(for: error)
            if errorKind == .permissionDenied {
                return failedProbe(
                    role: role,
                    url: normalizedURL,
                    capturedAt: capturedAt,
                    status: .inaccessible,
                    errorKind: errorKind
                )
            }
            if Self.isMissing(error) {
                let volumeValues = nearestVolumeValues(for: normalizedURL.deletingLastPathComponent())
                return RawDiagnosticStorageProbe(
                    capturedAt: capturedAt,
                    role: role,
                    url: normalizedURL,
                    status: .missing,
                    isDirectory: nil,
                    isReadable: nil,
                    isWritable: nil,
                    fileSizeBytes: nil,
                    modificationAgeSeconds: nil,
                    volumeAvailableBytes: volumeValues?.volumeAvailableCapacityForImportantUsage,
                    volumeTotalBytes: volumeValues?.volumeTotalCapacity.map(Int64.init),
                    volumeReadOnly: volumeValues?.volumeIsReadOnly,
                    errorKind: nil
                )
            }
            return failedProbe(
                role: role,
                url: normalizedURL,
                capturedAt: capturedAt,
                status: .error,
                errorKind: errorKind
            )
        }
    }

    private func failedProbe(
        role: DiagnosticStorageRole,
        url: URL,
        capturedAt: Date,
        status: DiagnosticStorageStatus,
        errorKind: DiagnosticStorageErrorKind
    ) -> RawDiagnosticStorageProbe {
        RawDiagnosticStorageProbe(
            capturedAt: capturedAt,
            role: role,
            url: url,
            status: status,
            isDirectory: nil,
            isReadable: false,
            isWritable: false,
            fileSizeBytes: nil,
            modificationAgeSeconds: nil,
            volumeAvailableBytes: nil,
            volumeTotalBytes: nil,
            volumeReadOnly: nil,
            errorKind: errorKind
        )
    }

    private func nearestVolumeValues(for url: URL) -> URLResourceValues? {
        var candidate = url
        while true {
            do {
                return try resourceReader(candidate, Self.volumeKeys)
            } catch {
                guard Self.errorKind(for: error) != .permissionDenied else {
                    return nil
                }
                guard Self.isMissing(error) else {
                    return nil
                }
            }
            let parent = candidate.deletingLastPathComponent()
            guard parent.path != candidate.path else {
                return nil
            }
            candidate = parent
        }
    }

    private static func errorKind(for error: Error) -> DiagnosticStorageErrorKind {
        let error = error as NSError
        if error.domain == NSPOSIXErrorDomain,
           error.code == Int(EACCES) || error.code == Int(EPERM)
        {
            return .permissionDenied
        }
        if error.domain == NSCocoaErrorDomain,
           error.code == NSFileReadNoPermissionError || error.code == NSFileWriteNoPermissionError
        {
            return .permissionDenied
        }
        if error.domain == NSPOSIXErrorDomain, error.code == Int(EIO) {
            return .inputOutput
        }
        if let underlyingError = error.userInfo[NSUnderlyingErrorKey] as? Error {
            return errorKind(for: underlyingError)
        }
        return .unknown
    }

    private static func isMissing(_ error: Error) -> Bool {
        let error = error as NSError
        if error.domain == NSPOSIXErrorDomain,
           error.code == Int(ENOENT) || error.code == Int(ENOTDIR)
        {
            return true
        }
        if error.domain == NSCocoaErrorDomain,
           error.code == NSFileNoSuchFileError || error.code == NSFileReadNoSuchFileError
        {
            return true
        }
        if let underlyingError = error.userInfo[NSUnderlyingErrorKey] as? Error {
            return isMissing(underlyingError)
        }
        return false
    }
}

struct DiagnosticJobSettings: Encodable, Equatable {
    let profileKind: String
    let builtInProfileID: String?
    let videoOutputMode: String
    let av1CRF: Int
    let hevcQuality: Int
    let leftRightBitrate: Int
    let upscaleEnabled: Bool
    let upscaleQuality: Int
    let fieldOfView: Int
    let frameRateOverrideSet: Bool
    let resolutionOverrideSet: Bool
    let cropBlackBars: Bool
    let swapEyes: Bool
    let audioHandling: String
    let audioBitrate: Int
    let subtitleMode: String
    let startStage: Int
    let keepStageFiles: Bool
    let overwriteExisting: Bool
    let removeOriginalAfterSuccess: Bool
    let continueOnError: Bool
    let softwareEncoder: Bool
    let outputCommands: Bool
    let keepAwake: Bool

    init(draft: ConversionDraft) {
        let encoding = draft.options.encoding
        let job = draft.options.job
        profileKind = draft.profile.kind.rawValue
        builtInProfileID = draft.profile.isBuiltIn ? draft.profile.id : nil
        videoOutputMode = encoding.videoOutputMode.rawValue
        av1CRF = encoding.av1CRF
        hevcQuality = encoding.hevcQuality
        leftRightBitrate = encoding.leftRightBitrate
        upscaleEnabled = encoding.upscaleEnabled
        upscaleQuality = encoding.upscaleQuality
        fieldOfView = encoding.fieldOfView
        frameRateOverrideSet = !encoding.frameRateOverride.isEmpty
        resolutionOverrideSet = !encoding.resolutionOverride.isEmpty
        cropBlackBars = encoding.cropBlackBars
        swapEyes = encoding.swapEyes
        audioHandling = encoding.audioHandling.rawValue
        audioBitrate = encoding.audioBitrate
        subtitleMode = encoding.subtitles.mode.rawValue
        startStage = job.startStage.rawValue
        keepStageFiles = job.keepStageFiles
        overwriteExisting = job.overwriteExisting
        removeOriginalAfterSuccess = job.removeOriginalAfterSuccess
        continueOnError = job.continueOnError
        softwareEncoder = job.softwareEncoder
        outputCommands = job.outputCommands
        keepAwake = job.keepAwake
    }
}

struct DiagnosticJobContext: Equatable {
    let jobID: UUID
    let operation: String
    let sourceKind: String
    let sourceURL: URL
    let destinationURL: URL?
    let outputURL: URL?
    let settings: DiagnosticJobSettings?
    let redactionPaths: [String]
    let sensitiveNames: [String]

    init(jobID: UUID, source: ConversionSource) {
        self.jobID = jobID
        operation = "inspect_source"
        sourceKind = source.kind.rawValue
        sourceURL = source.url
        destinationURL = nil
        outputURL = nil
        settings = nil
        redactionPaths = [source.url.path, source.workerSourcePath]
        sensitiveNames = [source.displayName, source.url.lastPathComponent]
    }

    init(jobID: UUID, draft: ConversionDraft) {
        self.jobID = jobID
        operation = "convert_source"
        sourceKind = draft.source.kind.rawValue
        sourceURL = draft.source.url
        destinationURL = draft.destinationURL
        outputURL = draft.proposedOutputURL
        settings = DiagnosticJobSettings(draft: draft)
        redactionPaths = [
            draft.source.url.path,
            draft.source.workerSourcePath,
            draft.destinationURL.path,
            draft.proposedOutputURL.path,
        ]
        sensitiveNames = [
            draft.source.displayName,
            draft.source.url.lastPathComponent,
            draft.sourceDetails?.name,
            draft.selectedTitle?.name,
            draft.selectedTitle?.outputName,
            draft.destinationURL.lastPathComponent,
            draft.proposedOutputURL.lastPathComponent,
        ]
        .compactMap { $0 }
    }

    var storageTargets: [(DiagnosticStorageRole, URL)] {
        var targets: [(DiagnosticStorageRole, URL)] = [(.source, sourceURL)]
        if let destinationURL {
            targets.append((.destination, destinationURL))
        }
        if let outputURL {
            targets.append((.output, outputURL))
        }
        return targets
    }
}

struct DiagnosticProgressSnapshot: Encodable, Equatable, Sendable {
    let currentStage: Int
    let totalStages: Int
    let stageFraction: Double?

    init(_ progress: WorkerProgress) {
        currentStage = progress.currentStage
        totalStages = progress.totalStages
        stageFraction = progress.stageFraction
    }
}

struct DiagnosticEventRecord: Equatable, Sendable {
    let recordedAt: Date
    let source: String
    let name: String
    let jobID: UUID?
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
    let resultSizeBytes: Int64?
    let workerVersion: String?
    let exitStatus: Int32?

    var serializedByteCount: Int {
        let strings = [
            source,
            name,
            phase,
            operation,
            activeMode,
            stage,
            message,
            details,
            level,
            warningCode,
            failureCode,
            workerVersion,
        ]
        .compactMap { $0 }
        .reduce(0) { $0 + DiagnosticJSONByteCount.string($1) }
        let choicesBytes = choices?.reduce(2) {
            $0 + DiagnosticJSONByteCount.string($1) + 1
        } ?? 0
        let numericBytes = [sequence, elapsedSeconds]
            .compactMap { $0 }
            .reduce(0) { $0 + String($1).utf8.count }
        let progressBytes = progress.map {
            64
                + String($0.currentStage).utf8.count
                + String($0.totalStages).utf8.count
                + ($0.stageFraction.map { String($0).utf8.count } ?? 0)
        } ?? 0
        let resultSizeBytes = resultSizeBytes.map { String($0).utf8.count } ?? 0
        let exitStatusBytes = exitStatus.map { String($0).utf8.count } ?? 0
        let fixedValueBytes = (jobID == nil ? 0 : 38)
            + (retryable == nil ? 0 : 5)
            + 36
        return 256
            + strings
            + choicesBytes
            + numericBytes
            + progressBytes
            + resultSizeBytes
            + exitStatusBytes
            + fixedValueBytes
    }
}

struct DiagnosticEventHistorySnapshot: Equatable, Sendable {
    let entries: [DiagnosticEventRecord]
    let totalRecordedEntries: Int
    let droppedEntries: Int
    let droppedBytes: Int
}

struct DiagnosticEventHistory {
    private let maximumEntries: Int
    private let maximumBytes: Int
    private var entries: [DiagnosticEventRecord] = []
    private var retainedBytes = 0
    private var droppedEntries = 0
    private var droppedBytes = 0

    init(maximumEntries: Int = 512, maximumBytes: Int = 384 * 1_024) {
        precondition(maximumEntries > 0 && maximumBytes > 0)
        self.maximumEntries = maximumEntries
        self.maximumBytes = maximumBytes
    }

    mutating func append(_ entry: DiagnosticEventRecord) {
        entries.append(entry)
        retainedBytes += entry.serializedByteCount
        while entries.count > maximumEntries || retainedBytes > maximumBytes {
            let removed = entries.removeFirst()
            retainedBytes -= removed.serializedByteCount
            droppedEntries += 1
            droppedBytes += removed.serializedByteCount
        }
    }

    func snapshot() -> DiagnosticEventHistorySnapshot {
        DiagnosticEventHistorySnapshot(
            entries: entries,
            totalRecordedEntries: entries.count + droppedEntries,
            droppedEntries: droppedEntries,
            droppedBytes: droppedBytes
        )
    }
}

struct DiagnosticBatchSummary: Equatable, Sendable {
    let kind: String
    let totalItems: Int
    let activeItems: Int
    let statusCounts: [String: Int]
}

struct DiagnosticStorageSampleTarget: Equatable, Sendable {
    let role: DiagnosticStorageRole
    let url: URL
}

struct DiagnosticStorageSampleRequest: Equatable, Sendable {
    let jobID: UUID
    let capturedAt: Date
    let targets: [DiagnosticStorageSampleTarget]
}

struct DiagnosticCaptureSnapshot: @unchecked Sendable {
    let capturedAt: Date
    let lifecycle: WorkerLifecycleState
    let activeMode: String?
    let jobContext: DiagnosticJobContext?
    let redactionContexts: [DiagnosticJobContext]
    let batchSummary: DiagnosticBatchSummary?
    let process: WorkerProcessDiagnosticSnapshot
    let processExitStatus: Int32?
    let workerVersion: String?
    let events: DiagnosticEventHistorySnapshot
    let storageSamples: [RawDiagnosticStorageSample]
    let totalStorageSamples: Int
    let droppedStorageSamples: Int
}

final class DiagnosticSessionRecorder {
    private let maximumRedactionContexts: Int
    private let maximumEvents: Int
    private let maximumEventBytes: Int
    private let maximumStorageSamples: Int
    private let storageSampleInterval: TimeInterval
    private var history: DiagnosticEventHistory
    private var storageSamples: [RawDiagnosticStorageSample] = []
    private var droppedStorageSamples = 0
    private var lastStorageSampleAt: [String: Date] = [:]
    private var recentJobContexts: [DiagnosticJobContext] = []
    private(set) var currentJobContext: DiagnosticJobContext?
    private(set) var latestProcessSnapshot = WorkerProcessDiagnosticSnapshot.empty
    private(set) var latestProcessExitStatus: Int32?
    private(set) var workerVersion: String?
    private(set) var latestLifecycle: WorkerLifecycleState?

    init(
        maximumEvents: Int = 512,
        maximumEventBytes: Int = 384 * 1_024,
        maximumStorageSamples: Int = 128,
        storageSampleInterval: TimeInterval = 10
    ) {
        self.maximumEvents = maximumEvents
        self.maximumEventBytes = maximumEventBytes
        maximumRedactionContexts = maximumEvents
        self.maximumStorageSamples = maximumStorageSamples
        self.storageSampleInterval = storageSampleInterval
        history = DiagnosticEventHistory(
            maximumEntries: maximumEvents,
            maximumBytes: maximumEventBytes
        )
    }

    func reset() {
        history = DiagnosticEventHistory(
            maximumEntries: maximumEvents,
            maximumBytes: maximumEventBytes
        )
        storageSamples.removeAll(keepingCapacity: true)
        droppedStorageSamples = 0
        lastStorageSampleAt.removeAll(keepingCapacity: true)
        recentJobContexts.removeAll(keepingCapacity: true)
        currentJobContext = nil
        latestProcessSnapshot = .empty
        latestProcessExitStatus = nil
        workerVersion = nil
        latestLifecycle = nil
    }

    func beginJob(
        context: DiagnosticJobContext,
        lifecycle: WorkerLifecycleState,
        activeMode: String,
        recordedAt: Date
    ) {
        currentJobContext = context
        recentJobContexts.removeAll { $0.jobID == context.jobID }
        recentJobContexts.append(context)
        while recentJobContexts.count > maximumRedactionContexts {
            recentJobContexts.removeFirst()
        }
        latestProcessSnapshot = .empty
        latestProcessExitStatus = nil
        recordWorkflow(
            name: "job.launch_requested",
            lifecycle: lifecycle,
            activeMode: activeMode,
            recordedAt: recordedAt,
            jobID: context.jobID
        )
    }

    func record(
        event: WorkerEvent,
        lifecycle: WorkerLifecycleState,
        activeMode: String?,
        recordedAt: Date
    ) {
        let observabilityEvent = event.payload.observabilityEvent
        let terminalPhase: String?
        switch event.type {
        case .jobCompleted:
            terminalPhase = WorkerPhase.completed.rawValue
        case .jobFailed:
            terminalPhase = WorkerPhase.failed.rawValue
        case .jobCancelled:
            terminalPhase = WorkerPhase.cancelled.rawValue
        case .jobDecisionRequired:
            terminalPhase = WorkerPhase.decisionRequired.rawValue
        default:
            terminalPhase = nil
        }
        let resultSize = event.payload.conversionResult?.sizeBytes
            ?? event.payload.artifact?.sizeBytes
            ?? event.payload.previewResult?.sizeBytes
            ?? event.payload.result?.sizeBytes
        let record = DiagnosticEventRecord(
            recordedAt: recordedAt,
            source: observabilityEvent?.emitter ?? "worker",
            name: observabilityEvent?.kind ?? event.type.rawValue,
            jobID: event.jobID,
            sequence: event.sequence,
            phase: terminalPhase ?? lifecycle.phase.rawValue,
            operation: event.payload.operation ?? context(for: event.jobID)?.operation,
            activeMode: activeMode,
            stage: event.payload.stage ?? observabilityEvent?.context.stage?.id,
            message: event.payload.message
                ?? event.payload.error?.message
                ?? event.payload.decision?.prompt
                ?? observabilityEvent?.data.message?.value,
            details: event.payload.error?.details
                ?? event.payload.decision?.details
                ?? observabilityEvent?.data.detail?.value,
            level: event.payload.level ?? observabilityEvent?.severity,
            elapsedSeconds: event.payload.elapsedSeconds,
            progress: event.payload.progress.map(DiagnosticProgressSnapshot.init),
            warningCode: event.payload.warningCode,
            failureCode: event.payload.error?.code
                ?? event.payload.decision?.identifier
                ?? observabilityEvent?.data.failure?.code,
            retryable: event.payload.error?.retryable ?? observabilityEvent?.data.failure?.retryable,
            choices: event.payload.decision?.choices,
            resultSizeBytes: resultSize,
            workerVersion: event.payload.workerVersion,
            exitStatus: nil
        )
        history.append(record)
        if let incomingWorkerVersion = event.payload.workerVersion {
            workerVersion = incomingWorkerVersion
        }
        latestLifecycle = lifecycle
    }

    func recordWorkflow(
        name: String,
        lifecycle: WorkerLifecycleState,
        activeMode: String?,
        recordedAt: Date,
        message: String? = nil,
        details: String? = nil,
        jobID: UUID? = nil,
        exitStatus: Int32? = nil
    ) {
        let jobContext = context(for: jobID)
        history.append(
            DiagnosticEventRecord(
                recordedAt: recordedAt,
                source: "client",
                name: name,
                jobID: jobID,
                sequence: nil,
                phase: lifecycle.phase.rawValue,
                operation: jobContext?.operation,
                activeMode: activeMode,
                stage: lifecycle.stageMessage,
                message: message,
                details: details,
                level: nil,
                elapsedSeconds: lifecycle.elapsedSeconds,
                progress: lifecycle.progress.map(DiagnosticProgressSnapshot.init),
                warningCode: nil,
                failureCode: lifecycle.failureCode,
                retryable: lifecycle.failureRetryable,
                choices: lifecycle.recoveryDecision?.choices,
                resultSizeBytes: lifecycle.conversionResult?.sizeBytes,
                workerVersion: workerVersion,
                exitStatus: exitStatus
            )
        )
        if let exitStatus {
            latestProcessExitStatus = exitStatus
        }
        latestLifecycle = lifecycle
    }

    func updateProcessSnapshot(_ snapshot: WorkerProcessDiagnosticSnapshot) {
        latestProcessSnapshot = snapshot
    }

    func makeStorageSampleRequest(
        recordedAt: Date,
        force: Bool = false
    ) -> DiagnosticStorageSampleRequest? {
        guard let context = currentJobContext else {
            return nil
        }
        var targets: [DiagnosticStorageSampleTarget] = []
        for (role, url) in context.storageTargets where role != .source {
            let key = "\(role.rawValue):\(url.standardizedFileURL.path)"
            if !force,
               let lastSampleAt = lastStorageSampleAt[key],
               recordedAt.timeIntervalSince(lastSampleAt) < storageSampleInterval
            {
                continue
            }
            lastStorageSampleAt[key] = recordedAt
            targets.append(DiagnosticStorageSampleTarget(role: role, url: url))
        }
        guard !targets.isEmpty else {
            return nil
        }
        return DiagnosticStorageSampleRequest(
            jobID: context.jobID,
            capturedAt: recordedAt,
            targets: targets
        )
    }

    func recordStorageSamples(
        _ samples: [RawDiagnosticStorageSample],
        for jobID: UUID
    ) {
        guard context(for: jobID) != nil else {
            return
        }
        storageSamples.append(contentsOf: samples)
        storageSamples.sort {
            if $0.capturedAt == $1.capturedAt {
                return $0.role.rawValue < $1.role.rawValue
            }
            return $0.capturedAt < $1.capturedAt
        }
        while storageSamples.count > maximumStorageSamples {
            storageSamples.removeFirst()
            droppedStorageSamples += 1
        }
    }

    func snapshot(
        capturedAt: Date,
        lifecycle: WorkerLifecycleState,
        activeMode: String?,
        batchSummary: DiagnosticBatchSummary?,
        process: WorkerProcessDiagnosticSnapshot
    ) -> DiagnosticCaptureSnapshot {
        let meaningfulLifecycle = lifecycle.phase == .empty ? latestLifecycle ?? lifecycle : lifecycle
        return DiagnosticCaptureSnapshot(
            capturedAt: capturedAt,
            lifecycle: meaningfulLifecycle,
            activeMode: activeMode,
            jobContext: currentJobContext,
            redactionContexts: recentJobContexts,
            batchSummary: batchSummary,
            process: process,
            processExitStatus: latestProcessExitStatus,
            workerVersion: workerVersion,
            events: history.snapshot(),
            storageSamples: storageSamples,
            totalStorageSamples: storageSamples.count + droppedStorageSamples,
            droppedStorageSamples: droppedStorageSamples
        )
    }

    private func context(for jobID: UUID?) -> DiagnosticJobContext? {
        guard let jobID else {
            return nil
        }
        return recentJobContexts.last { $0.jobID == jobID }
    }
}

private enum DiagnosticJSONByteCount {
    static func string(_ value: String) -> Int {
        2 + value.unicodeScalars.reduce(0) { count, scalar in
            count + escapedScalar(scalar)
        }
    }

    private static func escapedScalar(_ scalar: Unicode.Scalar) -> Int {
        switch scalar.value {
        case 0x08, 0x09, 0x0A, 0x0C, 0x0D, 0x22, 0x5C:
            return 2
        case 0x00 ... 0x1F:
            return 6
        case 0x00 ... 0x7F:
            return 1
        case 0x80 ... 0x7FF:
            return 2
        case 0x800 ... 0xFFFF:
            return 3
        default:
            return 4
        }
    }
}

private extension NSLock {
    func withDiagnosticLock<Result>(_ operation: () throws -> Result) rethrows -> Result {
        lock()
        defer { unlock() }
        return try operation()
    }
}

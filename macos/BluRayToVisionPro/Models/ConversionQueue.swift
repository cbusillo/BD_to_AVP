import Foundation

enum ConversionQueueItemStatus: Equatable {
    case waiting
    case processing
    case attention(String)
    case completed(ConversionResult)
    case failed(String)
    case cancelled
}

struct ConversionQueueItem: Identifiable, Equatable {
    let id: UUID
    let draft: ConversionDraft
    var status: ConversionQueueItemStatus

    init(id: UUID = UUID(), draft: ConversionDraft, status: ConversionQueueItemStatus = .waiting) {
        self.id = id
        self.draft = draft
        self.status = status
    }

    var displayName: String {
        draft.selectedTitle?.name ?? draft.source.displayName
    }

    var plannedOutputURL: URL {
        draft.proposedOutputURL
    }
}

enum SourceFolderQueueItemStatus: String, Equatable {
    case pending
    case inspecting
    case converting
    case stopping
    case completed
    case failed
    case stopped
    case notStarted

    var title: String {
        switch self {
        case .pending:
            "Pending"
        case .inspecting:
            "Inspecting"
        case .converting:
            "Converting"
        case .stopping:
            "Stopping"
        case .completed:
            "Completed"
        case .failed:
            "Failed"
        case .stopped:
            "Stopped"
        case .notStarted:
            "Not Started"
        }
    }

    var systemImage: String {
        switch self {
        case .pending:
            "clock"
        case .inspecting:
            "doc.text.magnifyingglass"
        case .converting:
            "gearshape.2"
        case .stopping:
            "stop.circle"
        case .completed:
            "checkmark.circle.fill"
        case .failed:
            "exclamationmark.triangle.fill"
        case .stopped:
            "stop.circle.fill"
        case .notStarted:
            "pause.circle"
        }
    }
}

struct SourceFolderQueueItem: Identifiable, Equatable {
    let id: UUID
    let source: ConversionSource
    var draft: ConversionDraft?
    var status: SourceFolderQueueItemStatus
    var inspection: SourceInspection?
    var conversionResult: ConversionResult?
    var failureMessage: String?
    var failureDetails: String?
    var failureRetryable: Bool
    var recoveryDecision: WorkerDecision?
    var diagnosticLog: String

    init(id: UUID = UUID(), source: ConversionSource) {
        self.id = id
        self.source = source
        draft = nil
        status = .pending
        inspection = nil
        conversionResult = nil
        failureMessage = nil
        failureDetails = nil
        failureRetryable = false
        recoveryDecision = nil
        diagnosticLog = ""
    }

    var canRetry: Bool {
        status == .failed && (failureRetryable || recoveryDecision != nil)
    }
}

struct SourceFolderQueueState: Equatable {
    let folderSource: ConversionSource
    var items: [SourceFolderQueueItem]
    var activeItemID: UUID?
    var stopRequested: Bool
    var hasStarted: Bool
    var completionID: UUID?

    init(folderSource: ConversionSource, sources: [ConversionSource]) {
        self.folderSource = folderSource
        items = sources.map { SourceFolderQueueItem(source: $0) }
        activeItemID = nil
        stopRequested = false
        hasStarted = false
        completionID = nil
    }

    var activeItem: SourceFolderQueueItem? {
        guard let activeItemID else {
            return nil
        }
        return items.first(where: { $0.id == activeItemID })
    }

    var activeItemIndex: Int? {
        guard let activeItemID else {
            return nil
        }
        return items.firstIndex(where: { $0.id == activeItemID })
    }

    var nextPendingIndex: Int? {
        items.firstIndex(where: { $0.status == .pending })
    }

    var totalCount: Int { items.count }
    var completedCount: Int { items.filter { $0.status == .completed }.count }
    var failedCount: Int { items.filter { $0.status == .failed }.count }
    var stoppedCount: Int { items.filter { $0.status == .stopped }.count }
    var notStartedCount: Int { items.filter { $0.status == .notStarted }.count }
    var pendingCount: Int { items.filter { $0.status == .pending }.count }

    var countsText: String {
        var counts = ["\(completedCount) done", "\(failedCount) failed"]
        if stoppedCount > 0 {
            counts.append("\(stoppedCount) stopped")
        }
        if notStartedCount > 0 {
            counts.append("\(notStartedCount) not started")
        } else if pendingCount > 0 {
            counts.append("\(pendingCount) waiting")
        }
        return counts.joined(separator: " · ")
    }

    var isRunning: Bool {
        activeItemID != nil
    }

    var isFinished: Bool {
        hasStarted
            && activeItemID == nil
            && !items.contains(where: { item in
                item.status == .pending
                    || item.status == .inspecting
                    || item.status == .converting
                    || item.status == .stopping
            })
    }

    var completedOutputURLs: [URL] {
        items.compactMap { $0.conversionResult?.outputURL }
    }

    var summaryText: String {
        guard !items.isEmpty else {
            return "No supported sources"
        }
        if isRunning, let activeItem {
            let position = (activeItemIndex ?? 0) + 1
            return "Item \(position) of \(totalCount): \(activeItem.source.displayName)"
        }
        if isFinished {
            if failedCount > 0 || stoppedCount > 0 || notStartedCount > 0 {
                return "\(completedCount) completed, \(failedCount) failed, \(stoppedCount) stopped, \(notStartedCount) not started"
            }
            return "\(completedCount) of \(totalCount) completed"
        }
        return "\(totalCount) sources ready"
    }

    mutating func prepareForRun(
        profile: EncodingProfile,
        destinationURL: URL,
        options: ConversionOptions
    ) {
        activeItemID = nil
        stopRequested = false
        hasStarted = true
        completionID = nil

        var outputCounts: [String: Int] = [:]
        for index in items.indices {
            let draft = ConversionDraft(
                source: items[index].source,
                sourceDetails: nil,
                profile: profile,
                destinationURL: destinationURL,
                options: options
            )
            items[index].draft = draft
            let outputKey = draft.proposedOutputURL.standardizedFileURL.path.lowercased()
            outputCounts[outputKey, default: 0] += 1
        }

        for index in items.indices {
            items[index].status = .pending
            items[index].inspection = nil
            items[index].conversionResult = nil
            items[index].failureMessage = nil
            items[index].failureDetails = nil
            items[index].failureRetryable = false
            items[index].recoveryDecision = nil
            items[index].diagnosticLog = ""

            guard let draft = items[index].draft else {
                continue
            }
            let outputKey = draft.proposedOutputURL.standardizedFileURL.path.lowercased()
            if outputCounts[outputKey, default: 0] > 1 {
                items[index].status = .failed
                items[index].failureMessage = "Another queued source would create the same output file."
                items[index].failureDetails = draft.proposedOutputURL.path
            }
        }
    }

    mutating func markPendingItemsStopped() {
        for index in items.indices where items[index].status == .pending {
            items[index].status = .notStarted
        }
    }
}

enum SourceFolderDiscovery {
    private static let supportedExtensions = Set(["iso", "mkv", "mts", "m2ts"])

    static func discoverSources(
        in folderURL: URL,
        fileManager: FileManager = .default
    ) -> [ConversionSource] {
        let resourceKeys: Set<URLResourceKey> = [
            .isDirectoryKey,
            .isRegularFileKey,
            .isSymbolicLinkKey,
        ]
        guard let enumerator = fileManager.enumerator(
            at: folderURL.standardizedFileURL,
            includingPropertiesForKeys: Array(resourceKeys),
            options: [.skipsHiddenFiles, .skipsPackageDescendants]
        ) else {
            return []
        }

        var sources: [ConversionSource] = []
        for case let candidateURL as URL in enumerator {
            guard let values = try? candidateURL.resourceValues(forKeys: resourceKeys) else {
                continue
            }
            if values.isSymbolicLink == true {
                if values.isDirectory == true {
                    enumerator.skipDescendants()
                }
                continue
            }
            if values.isDirectory == true {
                if candidateURL.lastPathComponent.caseInsensitiveCompare("BDMV") == .orderedSame {
                    enumerator.skipDescendants()
                }
                continue
            }
            guard values.isRegularFile == true,
                  supportedExtensions.contains(candidateURL.pathExtension.lowercased()),
                  let source = ConversionSource.infer(from: candidateURL, fileManager: fileManager)
            else {
                continue
            }
            sources.append(source)
        }

        return sources.sorted { first, second in
            let firstPath = first.url.standardizedFileURL.path
            let secondPath = second.url.standardizedFileURL.path
            let firstKey = firstPath.lowercased()
            let secondKey = secondPath.lowercased()
            if firstKey == secondKey {
                return firstPath < secondPath
            }
            return firstKey < secondKey
        }
    }
}

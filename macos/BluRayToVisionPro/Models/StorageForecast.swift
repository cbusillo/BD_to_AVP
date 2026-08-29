import Foundation

enum StorageForecastFormatting {
    static let gibibyte: Int64 = 1_024 * 1_024 * 1_024

    static func coarse(_ bytes: Int64?) -> String {
        guard let bytes, bytes >= 0 else {
            return "Unavailable"
        }
        let gibibytes = bytes == 0 ? 0 : ((bytes - 1) / gibibyte) + 1
        return "\(gibibytes) GiB"
    }

    static func about(_ bytes: Int64?) -> String {
        guard let bytes, bytes >= 0 else {
            return "Unavailable"
        }
        return "About \(coarse(bytes))"
    }
}

struct StorageForecastItem: Equatable, Sendable {
    static let safetyMarginRate = 0.10
    static let minimumSafetyMarginBytes: Int64 = 2 * StorageForecastFormatting.gibibyte
    static let maximumSafetyMarginBytes: Int64 = 20 * StorageForecastFormatting.gibibyte

    let estimatedOutputBytes: Int64?
    let temporaryWorkingBytes: Int64?
    let retainedIntermediateBytes: Int64?
    let safetyMarginBytes: Int64?
    let totalPeakRequiredBytes: Int64?
    let unavailableReason: String?
    let conservativeUpperBound: Bool

    init(draft: ConversionDraft) {
        let estimate = VideoStorageEstimate(drafts: [draft])
        estimatedOutputBytes = estimate.finalOutputBytes
        temporaryWorkingBytes = Self.temporaryBytes(
            peakWorkingBytes: estimate.peakWorkingBytes,
            finalOutputBytes: estimate.finalOutputBytes
        )
        retainedIntermediateBytes = estimate.retainedIntermediateBytes
        unavailableReason = estimate.unavailableReason
        conservativeUpperBound = estimate.conservativeFallbackReserve

        guard let peakWorkingBytes = estimate.peakWorkingBytes,
              let safetyMarginBytes = Self.safetyMargin(for: peakWorkingBytes),
              let totalPeakRequiredBytes = Self.adding(peakWorkingBytes, safetyMarginBytes)
        else {
            self.safetyMarginBytes = nil
            self.totalPeakRequiredBytes = nil
            return
        }
        self.safetyMarginBytes = safetyMarginBytes
        self.totalPeakRequiredBytes = totalPeakRequiredBytes
    }

    var isEstimated: Bool {
        totalPeakRequiredBytes != nil
    }

    var outputDescription: String {
        estimateDescription(estimatedOutputBytes)
    }

    var temporaryWorkingDescription: String {
        estimateDescription(temporaryWorkingBytes)
    }

    var retainedIntermediateDescription: String {
        guard let retainedIntermediateBytes else {
            return unavailableReason ?? "Unavailable"
        }
        return retainedIntermediateBytes == 0
            ? "None after success"
            : estimateDescription(retainedIntermediateBytes)
    }

    var safetyMarginDescription: String {
        guard let safetyMarginBytes else {
            return unavailableReason ?? "Unavailable"
        }
        return "\(StorageForecastFormatting.coarse(safetyMarginBytes)) safety margin"
    }

    var totalPeakDescription: String {
        guard let totalPeakRequiredBytes else {
            return unavailableReason ?? "Unavailable"
        }
        return estimateDescription(totalPeakRequiredBytes)
    }

    var assumptionDescription: String {
        if let unavailableReason {
            return unavailableReason
        }
        return "Video-only forecast with a coarse 10% safety margin, rounded to whole GiB. Audio, subtitles, and source preparation may add space."
    }

    static func safetyMargin(for peakWorkingBytes: Int64) -> Int64? {
        guard peakWorkingBytes >= 0 else {
            return nil
        }
        let rawMargin = Double(peakWorkingBytes) * safetyMarginRate
        guard rawMargin.isFinite, rawMargin <= Double(Int64.max) else {
            return nil
        }
        let roundedMargin = Int64(rawMargin.rounded(.up))
        let quantizedMargin: Int64
        if roundedMargin == 0 {
            quantizedMargin = minimumSafetyMarginBytes
        } else {
            let quotient = (roundedMargin - 1) / StorageForecastFormatting.gibibyte + 1
            guard quotient <= Int64.max / StorageForecastFormatting.gibibyte else {
                return nil
            }
            quantizedMargin = quotient * StorageForecastFormatting.gibibyte
        }
        return min(max(quantizedMargin, minimumSafetyMarginBytes), maximumSafetyMarginBytes)
    }

    private static func temporaryBytes(peakWorkingBytes: Int64?, finalOutputBytes: Int64?) -> Int64? {
        guard let peakWorkingBytes, let finalOutputBytes else {
            return nil
        }
        return max(0, peakWorkingBytes - finalOutputBytes)
    }

    private static func adding(_ first: Int64, _ second: Int64) -> Int64? {
        guard first <= Int64.max - second else {
            return nil
        }
        return first + second
    }

    private func estimateDescription(_ bytes: Int64?) -> String {
        guard let bytes else {
            return "Unavailable"
        }
        let qualifier = conservativeUpperBound ? "Up to" : "About"
        return "\(qualifier) \(StorageForecastFormatting.coarse(bytes))"
    }
}

struct StorageForecastDestinationSummary: Identifiable, Equatable, Sendable {
    let destinationPath: String
    let estimatedOutputBytes: Int64?
    let temporaryWorkingBytes: Int64?
    let retainedIntermediateBytes: Int64?
    let safetyMarginBytes: Int64?
    let totalPeakRequiredBytes: Int64?
    let estimatedItemCount: Int
    let unestimatedItemCount: Int
    let unestimatedReasons: [String]
    let conservativeUpperBound: Bool

    var id: String { destinationPath }

    var hasUnestimatedItems: Bool {
        unestimatedItemCount > 0
    }

    var outputDescription: String {
        estimateDescription(estimatedOutputBytes)
    }

    var temporaryWorkingDescription: String {
        estimateDescription(temporaryWorkingBytes)
    }

    var retainedIntermediateDescription: String {
        retainedIntermediateBytes == 0
            ? "None after success"
            : estimateDescription(retainedIntermediateBytes)
    }

    var safetyMarginDescription: String {
        StorageForecastFormatting.about(safetyMarginBytes)
    }

    var totalPeakDescription: String {
        estimateDescription(totalPeakRequiredBytes)
    }

    private func estimateDescription(_ bytes: Int64?) -> String {
        guard let bytes, bytes >= 0 else {
            return "Unavailable"
        }
        let qualifier = conservativeUpperBound ? "Up to" : "About"
        return "\(qualifier) \(StorageForecastFormatting.coarse(bytes))"
    }
}

struct QueueStorageSummary: Equatable, Sendable {
    let destinations: [StorageForecastDestinationSummary]

    init(items: [PersistentQueueItem]) {
        struct Accumulator {
            let destinationPath: String
            var estimatedOutputBytes: Int64 = 0
            var temporaryWorkingBytes: Int64 = 0
            var retainedIntermediateBytes: Int64 = 0
            var peakRequiredBytes: Int64 = 0
            var estimatedItemCount = 0
            var unestimatedItemCount = 0
            var unestimatedReasons: [String] = []
            var committedBytes: Int64 = 0
            var hasOverflow = false
            var conservativeUpperBound = false

            mutating func add(_ item: PersistentQueueItem) {
                if let deferredReason = item.storageForecastDeferredReason {
                    unestimatedItemCount += 1
                    if !unestimatedReasons.contains(deferredReason) {
                        unestimatedReasons.append(deferredReason)
                    }
                    return
                }
                let forecast = StorageForecastItem(draft: item.draft)
                conservativeUpperBound = conservativeUpperBound || forecast.conservativeUpperBound
                var itemOverflow = false
                if let outputBytes = forecast.estimatedOutputBytes,
                   let temporaryBytes = forecast.temporaryWorkingBytes,
                   let retainedBytes = forecast.retainedIntermediateBytes,
                   let itemPeakBytes = Self.adding(outputBytes, temporaryBytes),
                   let candidatePeakBytes = Self.adding(committedBytes, itemPeakBytes),
                   let nextOutputBytes = Self.adding(estimatedOutputBytes, outputBytes),
                   let nextRetainedBytes = Self.adding(retainedIntermediateBytes, retainedBytes),
                   let nextCommittedBytes = Self.adding(committedBytes, outputBytes),
                   let finalCommittedBytes = Self.adding(nextCommittedBytes, retainedBytes)
                {
                    estimatedOutputBytes = nextOutputBytes
                    temporaryWorkingBytes = max(temporaryWorkingBytes, temporaryBytes)
                    retainedIntermediateBytes = nextRetainedBytes
                    peakRequiredBytes = max(peakRequiredBytes, candidatePeakBytes)
                    committedBytes = finalCommittedBytes
                    estimatedItemCount += 1
                } else {
                    hasOverflow = true
                    itemOverflow = true
                }
                if let unavailableReason = forecast.unavailableReason {
                    unestimatedItemCount += 1
                    if !unestimatedReasons.contains(unavailableReason) {
                        unestimatedReasons.append(unavailableReason)
                    }
                } else if itemOverflow {
                    unestimatedItemCount += 1
                    let reason = "The storage forecast exceeds the supported size range."
                    if !unestimatedReasons.contains(reason) {
                        unestimatedReasons.append(reason)
                    }
                }
            }

            func summary() -> StorageForecastDestinationSummary {
                let margin = hasOverflow || unestimatedItemCount > 0
                    ? nil
                    : StorageForecastItem.safetyMargin(for: peakRequiredBytes)
                let total = margin.flatMap { Self.adding(peakRequiredBytes, $0) }
                return StorageForecastDestinationSummary(
                    destinationPath: destinationPath,
                    estimatedOutputBytes: estimatedItemCount > 0 ? estimatedOutputBytes : nil,
                    temporaryWorkingBytes: estimatedItemCount > 0 ? temporaryWorkingBytes : nil,
                    retainedIntermediateBytes: estimatedItemCount > 0 ? retainedIntermediateBytes : nil,
                    safetyMarginBytes: margin,
                    totalPeakRequiredBytes: total,
                    estimatedItemCount: estimatedItemCount,
                    unestimatedItemCount: unestimatedItemCount,
                    unestimatedReasons: unestimatedReasons,
                    conservativeUpperBound: conservativeUpperBound
                )
            }

            private static func adding(_ first: Int64, _ second: Int64) -> Int64? {
                guard first <= Int64.max - second else {
                    return nil
                }
                return first + second
            }
        }

        var accumulators: [String: Accumulator] = [:]
        var order: [String] = []
        for item in items.sorted(by: { $0.ordinal < $1.ordinal }) where item.isEligibleForStorageForecast {
            let destinationPath = item.draft.destinationURL.standardizedFileURL.path
            if accumulators[destinationPath] == nil {
                accumulators[destinationPath] = Accumulator(destinationPath: destinationPath)
                order.append(destinationPath)
            }
            accumulators[destinationPath]?.add(item)
        }
        destinations = order.compactMap { accumulators[$0]?.summary() }
    }
}

private extension PersistentQueueItem {
    var isEligibleForStorageForecast: Bool {
        switch status {
        case .waiting, .needsChoice, .interrupted, .attention, .stopped, .notStarted:
            true
        case let .failed(failure):
            failure.retryable
        case .inspecting, .processing, .stopping, .completed:
            false
        }
    }

    var storageForecastDeferredReason: String? {
        switch status {
        case .needsChoice:
            "Resolve the queued route-quality choice before its storage can be estimated."
        case .attention:
            "Choose the queued recovery action before its storage can be estimated."
        default:
            nil
        }
    }
}

enum QueueStoragePreflightVerdict: Equatable, Sendable {
    case available
    case unconfirmed(String)
    case unavailable(String)
    case insufficient(requiredBytes: Int64, availableBytes: Int64)
}

protocol QueueStoragePreflighting: Sendable {
    func preflight(destinationURL: URL, requiredBytes: Int64?) -> QueueStoragePreflightVerdict
}

struct SystemQueueStoragePreflight: QueueStoragePreflighting, @unchecked Sendable {
    let fileManager: FileManager
    let capacityProvider: any PreviewCapacityProviding

    init(
        fileManager: FileManager = .default,
        capacityProvider: (any PreviewCapacityProviding)? = nil
    ) {
        self.fileManager = fileManager
        self.capacityProvider = capacityProvider ?? SystemPreviewCapacityProvider(fileManager: fileManager)
    }

    func preflight(destinationURL: URL, requiredBytes: Int64?) -> QueueStoragePreflightVerdict {
        let destinationURL = destinationURL.standardizedFileURL
        let path = destinationURL.path
        guard fileManager.fileExists(atPath: path) else {
            return .unavailable("Destination \(path) is not connected or no longer exists.")
        }
        guard let values = try? destinationURL.resourceValues(forKeys: [.isDirectoryKey, .isWritableKey, .volumeIsReadOnlyKey]),
              values.isDirectory == true
        else {
            return .unavailable("Destination \(path) is not an accessible folder.")
        }
        if values.volumeIsReadOnly == true || values.isWritable == false || !fileManager.isWritableFile(atPath: path) {
            return .unavailable("Destination \(path) is read-only or not writable.")
        }

        let readings = capacityProvider.readings(for: destinationURL)
        if readings.readOnly == true || readings.writable == false {
            return .unavailable("Destination \(path) is read-only or not writable.")
        }
        let result = StorageCapacityResult.evaluate(
            requiredBytes: requiredBytes,
            evidence: readings.evidence
        )
        if result.isKnownInsufficient,
           let requiredBytes = result.requiredBytes,
           let availableBytes = result.availableBytes
        {
            return .insufficient(requiredBytes: requiredBytes, availableBytes: availableBytes)
        }
        switch result.state {
        case .known where result.sufficiency == .sufficient:
            return .available
        case .known where requiredBytes == nil:
            return .unconfirmed("Destination is writable, but the required output size is not estimated.")
        case .unknown:
            return .unconfirmed("Destination is writable, but free space could not be confirmed.")
        case .conflicting:
            return .unconfirmed("Destination is writable, but its free-space readings conflict.")
        case .known:
            return .unconfirmed("Destination is writable, but free space could not be confirmed.")
        }
    }
}

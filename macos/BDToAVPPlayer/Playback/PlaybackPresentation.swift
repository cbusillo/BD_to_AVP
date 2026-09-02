import Foundation

enum PlaybackTimeFormatter {
    static func string(for seconds: TimeInterval) -> String {
        guard seconds.isFinite, seconds >= 0 else {
            return "0:00"
        }

        let totalSeconds = Int(seconds.rounded(.down))
        let hours = totalSeconds / 3_600
        let minutes = (totalSeconds % 3_600) / 60
        let remainingSeconds = totalSeconds % 60

        if hours > 0 {
            return String(format: "%d:%02d:%02d", hours, minutes, remainingSeconds)
        }
        return String(format: "%d:%02d", minutes, remainingSeconds)
    }
}

enum PlaybackSeekPolicy {
    static func clampedTime(_ requestedTime: TimeInterval, duration: TimeInterval) -> TimeInterval {
        guard requestedTime.isFinite else {
            return 0
        }

        let lowerBound = max(0, requestedTime)
        guard duration.isFinite, duration > 0 else {
            return lowerBound
        }
        return min(lowerBound, duration)
    }
}

struct PlaybackPendingResumeState: Equatable {
    private(set) var value: TimeInterval? = nil

    mutating func store(_ requestedTime: TimeInterval?, duration: TimeInterval) {
        guard let requestedTime, requestedTime.isFinite, requestedTime >= 0 else {
            value = nil
            return
        }
        value = PlaybackSeekPolicy.clampedTime(requestedTime, duration: duration)
    }

    mutating func consume() -> TimeInterval? {
        defer { value = nil }
        return value
    }

    mutating func clear() {
        value = nil
    }
}

struct PlaybackIntentState: Equatable {
    private(set) var isSceneActive = true
    private(set) var wantsPlayback = false

    var shouldPlay: Bool {
        isSceneActive && wantsPlayback
    }

    mutating func requestPlayback() {
        wantsPlayback = isSceneActive
    }

    mutating func preservePlaybackIntent(wasPlaying: Bool) {
        wantsPlayback = isSceneActive && wasPlaying
    }

    mutating func pause() {
        wantsPlayback = false
    }

    mutating func sceneBecameActive() {
        isSceneActive = true
    }

    mutating func sceneBecameInactive() {
        isSceneActive = false
        wantsPlayback = false
    }

    mutating func reset() {
        wantsPlayback = false
    }
}

struct PlaybackScrubState: Equatable {
    private(set) var value: TimeInterval? = nil

    mutating func begin(currentTime: TimeInterval) {
        value = max(0, currentTime.isFinite ? currentTime : 0)
    }

    mutating func update(requestedTime: TimeInterval, duration: TimeInterval) {
        value = PlaybackSeekPolicy.clampedTime(requestedTime, duration: duration)
    }

    mutating func finish() -> TimeInterval? {
        defer { value = nil }
        return value
    }

    mutating func cancel() {
        value = nil
    }
}

struct PlaybackHUDVisibilityState: Equatable {
    static let autoHideDelay: TimeInterval = 3

    private(set) var isVisible = true
    private(set) var isAutoHideScheduled = false
    private(set) var autoHideGeneration = 0
    private var isInteracting = false

    mutating func reconcile(isPlaying: Bool) {
        guard PlaybackHUDVisibilityPolicy.shouldAutoHide(
            isPlaying: isPlaying,
            isInteracting: isInteracting
        )
        else {
            showAndCancelAutomaticHiding()
            return
        }

        if isVisible, !isAutoHideScheduled {
            scheduleAutomaticHiding()
        }
    }

    mutating func setInteracting(_ isInteracting: Bool, isPlaying: Bool) {
        self.isInteracting = isInteracting
        reconcile(isPlaying: isPlaying)
    }

    mutating func hoverBegan(isPlaying: Bool) {
        reveal(isPlaying: isPlaying)
    }

    mutating func reveal(isPlaying: Bool) {
        isVisible = true
        cancelAutomaticHiding()
        reconcile(isPlaying: isPlaying)
    }

    mutating func autoHideTimerFired(generation: Int) {
        guard generation == autoHideGeneration, isAutoHideScheduled else {
            return
        }

        isAutoHideScheduled = false
        isVisible = false
    }

    private mutating func showAndCancelAutomaticHiding() {
        isVisible = true
        cancelAutomaticHiding()
    }

    private mutating func scheduleAutomaticHiding() {
        autoHideGeneration += 1
        isAutoHideScheduled = true
    }

    private mutating func cancelAutomaticHiding() {
        guard isAutoHideScheduled else {
            return
        }

        autoHideGeneration += 1
        isAutoHideScheduled = false
    }
}

enum PlaybackHUDVisibilityPolicy {
    static func shouldAutoHide(isPlaying: Bool, isInteracting: Bool) -> Bool {
        isPlaying && !isInteracting
    }
}

struct PlaybackEyeOrderPresentation: Equatable {
    let title: String
    let systemImage: String
    let isSelected: Bool

    static func value(isEyeSwapped: Bool) -> PlaybackEyeOrderPresentation {
        PlaybackEyeOrderPresentation(
            title: isEyeSwapped ? "Reversed" : "Normal",
            systemImage: isEyeSwapped
                ? "arrow.left.arrow.right.circle.fill"
                : "arrow.left.arrow.right",
            isSelected: isEyeSwapped
        )
    }
}

struct PlaybackAudioOptionLabelMetadata: Equatable {
    let baseName: String
    let title: String?
    let role: String?
    let channelLayout: String?
    let index: Int
}

enum PlaybackAudioOptionLabelPolicy {
    static func labels(for options: [PlaybackAudioOptionLabelMetadata]) -> [String] {
        let baseNames = options.map { baseName(for: $0.baseName) }
        let groups = Dictionary(grouping: options.indices) { optionIndex in
            normalized(baseNames[optionIndex])
        }
        var labels = baseNames

        for optionIndices in groups.values where optionIndices.count > 1 {
            for optionIndex in optionIndices {
                let detail = detail(for: options[optionIndex], baseName: baseNames[optionIndex])
                labels[optionIndex] = detail.map {
                    "\(baseNames[optionIndex]) — \($0)"
                } ?? "\(baseNames[optionIndex]) — Track \(options[optionIndex].index + 1)"
            }

            let labelsByNormalizedValue = Dictionary(grouping: optionIndices) { optionIndex in
                normalized(labels[optionIndex])
            }
            for duplicateIndices in labelsByNormalizedValue.values where duplicateIndices.count > 1 {
                for optionIndex in duplicateIndices {
                    labels[optionIndex] = "\(labels[optionIndex]) — Track \(options[optionIndex].index + 1)"
                }
            }
        }

        return labels
    }

    private static func baseName(for value: String) -> String {
        let trimmedValue = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmedValue.isEmpty ? "Audio" : trimmedValue
    }

    private static func detail(
        for option: PlaybackAudioOptionLabelMetadata,
        baseName: String
    ) -> String? {
        var details: [String] = []
        for value in [option.title, option.role, option.channelLayout].compactMap({ $0 }) {
            let trimmedValue = value.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmedValue.isEmpty,
                  normalized(trimmedValue) != normalized(baseName),
                  !details.contains(where: { normalized($0) == normalized(trimmedValue) })
            else {
                continue
            }
            details.append(trimmedValue)
        }
        return details.isEmpty ? nil : details.joined(separator: ", ")
    }

    private static func normalized(_ value: String) -> String {
        value
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .lowercased()
    }
}

struct PlaybackAudioSelectionResolution: Equatable {
    let selectedIndex: Int?
    let requiresExplicitSelection: Bool
}

enum PlaybackAudioSelectionPolicy {
    static func resolve(currentIndex: Int?, optionCount: Int) -> PlaybackAudioSelectionResolution {
        guard optionCount > 0 else {
            return PlaybackAudioSelectionResolution(
                selectedIndex: nil,
                requiresExplicitSelection: false
            )
        }
        if let currentIndex, (0..<optionCount).contains(currentIndex) {
            return PlaybackAudioSelectionResolution(
                selectedIndex: currentIndex,
                requiresExplicitSelection: false
            )
        }
        return PlaybackAudioSelectionResolution(
            selectedIndex: 0,
            requiresExplicitSelection: true
        )
    }
}

enum PackedStereoStatusPresentation {
    static func message(
        mediaItem: MediaItem?,
        isReady: Bool,
        failureMessage: String?,
        preparationPhase: PlaybackPreparationPhase = .preparingMedia
    ) -> String {
        if let failureMessage {
            return failureMessage
        }
        if BuiltInStereoChecks.contains(mediaItem) {
            return isReady ? "Cover one eye at a time" : "Preparing the built-in stereo check…"
        }
        if isReady {
            return "Packed stereo playback"
        }
        return preparationPhase.message(for: mediaItem?.title)
    }
}

enum PlaybackPreparationPhase: Equatable {
    case openingSource
    case preparingMedia

    var title: String {
        switch self {
        case .openingSource:
            return "Opening Source"
        case .preparingMedia:
            return "Preparing Playback"
        }
    }

    func message(for mediaTitle: String?) -> String {
        let title = mediaTitle ?? "your movie"
        switch self {
        case .openingSource:
            return "Opening \(title) from Files. This may take longer while its source becomes available."
        case .preparingMedia:
            return "Preparing \(title)…"
        }
    }
}

struct PlaybackFailurePresentation: Equatable {
    let title: String
    let message: String
    let canRetry: Bool
    let canLocate: Bool

    static let unsupported = PlaybackFailurePresentation(
        title: "Format Not Supported",
        message: "This media format is not supported for playback.",
        canRetry: false,
        canLocate: false
    )

    static let sourceNeedsLocation = PlaybackFailurePresentation(
        title: "Source Needs Attention",
        message: "Locate this movie in Files, then try again.",
        canRetry: false,
        canLocate: true
    )

    static let sourceUnavailable = PlaybackFailurePresentation(
        title: "Source Unavailable",
        message: "The movie's source is not responding. Make it available in Files, then try again or locate the file.",
        canRetry: true,
        canLocate: true
    )

    static func sourceMismatch(detectedFormat: StereoFormat, expectedFormat: StereoFormat) -> PlaybackFailurePresentation {
        PlaybackFailurePresentation(
            title: "Different Movie Found",
            message: "This movie is \(detectedFormat.displayName), not \(expectedFormat.displayName). Locate the intended source and try again.",
            canRetry: false,
            canLocate: true
        )
    }

    static func preparationFailed(_ message: String) -> PlaybackFailurePresentation {
        PlaybackFailurePresentation(
            title: "Movie Unavailable",
            message: message,
            canRetry: true,
            canLocate: true
        )
    }

    static func builtInStereoCheckUnavailable(_ message: String) -> PlaybackFailurePresentation {
        PlaybackFailurePresentation(
            title: "Stereo Check Unavailable",
            message: message,
            canRetry: false,
            canLocate: false
        )
    }
}

enum ResumeWriteDecision: Equatable {
    case write(TimeInterval)
    case remove
    case skip
}

enum ResumeWritePolicy {
    static let completedPlaybackThreshold: TimeInterval = 5

    static func allowsWrite(
        isChangingEyeOrder: Bool,
        isFinishing: Bool,
        hasEstablishedPlayback: Bool
    ) -> Bool {
        hasEstablishedPlayback && (!isChangingEyeOrder || isFinishing)
    }

    static func position(
        currentTime: TimeInterval,
        eyeOrderChangeTime: TimeInterval?,
        isChangingEyeOrder: Bool,
        isFinishing: Bool
    ) -> TimeInterval {
        if isChangingEyeOrder,
           isFinishing,
           let eyeOrderChangeTime,
           eyeOrderChangeTime.isFinite
        {
            return eyeOrderChangeTime
        }
        return currentTime
    }

    static func decision(currentTime: TimeInterval, duration: TimeInterval) -> ResumeWriteDecision {
        guard currentTime.isFinite, currentTime >= 0 else {
            return .skip
        }

        guard duration.isFinite, duration > 0 else {
            return .write(currentTime)
        }

        let position = PlaybackSeekPolicy.clampedTime(currentTime, duration: duration)
        if position >= max(0, duration - completedPlaybackThreshold) {
            return .remove
        }
        return .write(position)
    }
}

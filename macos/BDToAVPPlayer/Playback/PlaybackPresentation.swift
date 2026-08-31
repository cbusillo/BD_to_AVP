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

enum PackedStereoStatusPresentation {
    static func message(
        mediaItem: MediaItem?,
        isReady: Bool,
        failureMessage: String?
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
        return "Preparing \(mediaItem?.title ?? "your movie")…"
    }
}

enum ResumeWriteDecision: Equatable {
    case write(TimeInterval)
    case remove
    case skip
}

enum ResumeWritePolicy {
    static let completedPlaybackThreshold: TimeInterval = 5

    static func allowsWrite(isChangingEyeOrder: Bool, isFinishing: Bool) -> Bool {
        !isChangingEyeOrder || isFinishing
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

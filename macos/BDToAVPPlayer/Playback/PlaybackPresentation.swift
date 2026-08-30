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

enum ResumeWriteDecision: Equatable {
    case write(TimeInterval)
    case remove
    case skip
}

enum ResumeWritePolicy {
    static let completedPlaybackThreshold: TimeInterval = 5

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

import CoreGraphics
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

/// The scrub bar's span and its loaded range. The span is the title's full length whenever that
/// is known up front, so the bar does not grow while a live stream or a movie loads; the loaded
/// range shows how far playback can seek right now.
struct PlaybackTimeline: Equatable {
    let duration: TimeInterval
    let loadedStart: TimeInterval
    let loadedEnd: TimeInterval

    /// - Parameters:
    ///   - titleDuration: The title's full length as the source reports it before playback,
    ///     such as the Mac's live title index. Ignored once `isFinal` reports the real length.
    ///   - playerDuration: The length the player knows so far.
    ///   - loadedRange: What can be played right now, or nil when that is the whole timeline.
    ///   - isFinal: Whether the source has reported its complete length.
    init(
        titleDuration: TimeInterval?,
        playerDuration: TimeInterval,
        loadedRange: ClosedRange<TimeInterval>?,
        isFinal: Bool = false
    ) {
        let candidates = [
            isFinal ? nil : titleDuration,
            playerDuration,
            loadedRange?.upperBound,
        ]
        let span = candidates
            .compactMap { $0 }
            .filter { $0.isFinite && $0 > 0 }
            .max() ?? 0
        duration = span

        let lower = loadedRange?.lowerBound ?? 0
        let upper = loadedRange?.upperBound ?? span
        loadedStart = min(max(0, lower.isFinite ? lower : 0), span)
        loadedEnd = min(max(loadedStart, upper.isFinite ? upper : 0), span)
    }

    var loadedStartFraction: Double {
        fraction(of: loadedStart)
    }

    var loadedEndFraction: Double {
        fraction(of: loadedEnd)
    }

    func fraction(of time: TimeInterval) -> Double {
        guard duration > 0, time.isFinite else {
            return 0
        }
        return min(max(0, time / duration), 1)
    }

    func time(atFraction fraction: Double) -> TimeInterval {
        guard fraction.isFinite else {
            return 0
        }
        return min(max(0, fraction), 1) * duration
    }

    /// The buffered range that holds `time`, from the player's loaded time ranges.
    static func bufferedRange(
        containing time: TimeInterval,
        in ranges: [ClosedRange<TimeInterval>]
    ) -> ClosedRange<TimeInterval>? {
        ranges.first { $0.contains(time) }
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

    /// Only leaving the app takes away permission to play. An inactive scene, which on
    /// visionOS includes Control Center or looking at another window, keeps playing.
    mutating func sceneEnteredBackground() {
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

    /// Reasons the controls stay up. While any hold is active the controls
    /// never auto-hide; the auto-hide delay starts again once the last hold ends.
    enum Hold: Hashable {
        case hover
        case scrubbing
        case trackPicker
    }

    /// Whether the controls are fully shown. When false they are faded, not removed: they stay
    /// interactive, and the system's hover effect brightens them while the wearer looks at them.
    private(set) var isVisible = true
    private(set) var isAutoHideScheduled = false
    private(set) var autoHideGeneration = 0
    private var holds: Set<Hold> = []

    var isFaded: Bool {
        !isVisible
    }

    mutating func reconcile(isPlaying: Bool) {
        guard PlaybackHUDVisibilityPolicy.shouldAutoHide(
            isPlaying: isPlaying,
            isInteracting: !holds.isEmpty
        )
        else {
            showAndCancelAutomaticHiding()
            return
        }

        if isVisible, !isAutoHideScheduled {
            scheduleAutomaticHiding()
        }
    }

    mutating func setHold(_ hold: Hold, isActive: Bool, isPlaying: Bool) {
        if isActive {
            holds.insert(hold)
        } else {
            holds.remove(hold)
        }
        reconcile(isPlaying: isPlaying)
    }

    mutating func hoverBegan(isPlaying: Bool) {
        isVisible = true
        setHold(.hover, isActive: true, isPlaying: isPlaying)
    }

    mutating func hoverEnded(isPlaying: Bool) {
        setHold(.hover, isActive: false, isPlaying: isPlaying)
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

enum PlaybackTrackPicker: Equatable {
    case audio
    case subtitles
}

/// Which track list the controls show inline. The lists render inside the
/// controls ornament instead of as system menus, which open behind the video.
struct PlaybackTrackPickerState: Equatable {
    private(set) var openPicker: PlaybackTrackPicker? = nil

    var isOpen: Bool {
        openPicker != nil
    }

    mutating func toggle(_ picker: PlaybackTrackPicker) {
        openPicker = openPicker == picker ? nil : picker
    }

    mutating func close() {
        openPicker = nil
    }
}

/// How tall a track list grows. Short lists show every entry; longer ones stop at a fixed number
/// of rows and scroll, so a disc with a dozen or more tracks still fits above the controls.
enum PlaybackTrackListLayout {
    static let rowHeight: CGFloat = 48
    static let maximumVisibleRows = 5

    static func listHeight(rowCount: Int) -> CGFloat {
        CGFloat(min(max(0, rowCount), maximumVisibleRows)) * rowHeight
    }

    static func scrolls(rowCount: Int) -> Bool {
        rowCount > maximumVisibleRows
    }
}

/// The line a track list shows when it has nothing to choose between, so a single-track or
/// unsubtitled title says so instead of graying out its button. The subtitle list's own "Off"
/// entry is not a subtitle.
enum PlaybackTrackListNote {
    static func note(for picker: PlaybackTrackPicker, optionCount: Int) -> String? {
        switch picker {
        case .audio:
            switch optionCount {
            case 0: "This title has no selectable audio tracks."
            case 1: "This title has one audio track."
            default: nil
            }
        case .subtitles:
            optionCount <= 1 ? "This title has no subtitles." : nil
        }
    }
}

/// How faded controls look. visionOS never reports where the wearer looks; the system renders
/// the hover effect, so `isLookedAt` comes from the effect itself, never from app state.
enum PlaybackHUDPresentation {
    static let fadedOpacity = 0.08
    /// Looking at faded controls brings them back quickly.
    static let revealDuration: TimeInterval = 0.2
    /// After the wearer looks away, the controls stay up this long before they start to fade,
    /// so a glance at the video and back does not flicker them.
    static let fadeOutHold: TimeInterval = 1.5
    /// The fade-out itself is slower than the reveal.
    static let fadeOutDuration: TimeInterval = 0.8

    struct LookTransition: Equatable {
        let delay: TimeInterval
        let duration: TimeInterval
    }

    static func opacity(isFaded: Bool, isLookedAt: Bool) -> Double {
        isFaded && !isLookedAt ? fadedOpacity : 1
    }

    /// The timing of the opacity change when the look starts (`isLookedAt`) or ends.
    static func lookTransition(isLookedAt: Bool) -> LookTransition {
        isLookedAt
            ? LookTransition(delay: 0, duration: revealDuration)
            : LookTransition(delay: fadeOutHold, duration: fadeOutDuration)
    }
}

enum PlaybackHUDVisibilityPolicy {
    static func shouldAutoHide(isPlaying: Bool, isInteracting: Bool) -> Bool {
        isPlaying && !isInteracting
    }

    static func allowsAutomaticHiding(
        isPlaying: Bool,
        isVoiceOverEnabled: Bool,
        isSwitchControlEnabled: Bool
    ) -> Bool {
        isPlaying && !isVoiceOverEnabled && !isSwitchControlEnabled
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
    let role: String?
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
                let option = options[optionIndex]
                let role = option.role?.trimmingCharacters(in: .whitespacesAndNewlines)
                let roleDetail = role.flatMap { value in
                    value.isEmpty || normalized(value) == normalized(baseNames[optionIndex]) ? nil : value
                }
                labels[optionIndex] = [
                    baseNames[optionIndex],
                    roleDetail,
                    "Track \(option.index + 1)",
                ]
                .compactMap { $0 }
                .joined(separator: " — ")
            }
        }

        return labels
    }

    private static func baseName(for value: String) -> String {
        let trimmedValue = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmedValue.isEmpty ? "Audio" : trimmedValue
    }

    private static func normalized(_ value: String) -> String {
        value
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .lowercased()
    }
}

enum PlaybackAudioSelectionPolicy {
    static func preferredIndex(
        currentIndex: Int?,
        defaultIndex: Int?,
        playableIndices: [Int]
    ) -> Int? {
        if let currentIndex, playableIndices.contains(currentIndex) {
            return currentIndex
        }
        if let defaultIndex, playableIndices.contains(defaultIndex) {
            return defaultIndex
        }
        return playableIndices.first
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

    static func relayPreparationFailed(_ message: String) -> PlaybackFailurePresentation {
        PlaybackFailurePresentation(
            title: "Relay Playback Unavailable",
            message: message,
            canRetry: true,
            canLocate: false
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
    /// How often a position is saved while playing, so a forced quit loses only seconds.
    static let periodicSaveInterval: TimeInterval = 10
    /// A live title watched for less than this is not worth mentioning when it starts over.
    static let liveResumeNoticeMinimumPosition: TimeInterval = 30

    static func isPeriodicSaveDue(lastSaveUptime: TimeInterval?, now: TimeInterval) -> Bool {
        guard let lastSaveUptime else {
            return true
        }
        return now - lastSaveUptime >= periodicSaveInterval
    }

    /// What a live title says when the wearer comes back to it. Live titles can only start from
    /// the beginning until the Mac can start the stream mid-title.
    static func liveResumeNotice(savedPosition: TimeInterval) -> String? {
        guard savedPosition.isFinite, savedPosition >= liveResumeNoticeMinimumPosition else {
            return nil
        }
        return "Live titles can't resume yet, so this starts from the beginning. You stopped at \(PlaybackTimeFormatter.string(for: savedPosition))."
    }

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

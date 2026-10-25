import AVFoundation
import Combine
import Foundation
import RealityKit
import SwiftUI

struct PlaybackMediaOption: Identifiable, Equatable, Hashable {
    let id: String
    let displayName: String
}

#if BD_TO_AVP_QUALIFICATION
struct PlaybackQualificationProgrammaticSeek: Equatable {
    let id: UUID
    let targetTime: TimeInterval
    var isInFlight: Bool
    var expiresAtUptime: TimeInterval
}

enum PlaybackQualificationNativeControlPolicy {
    static let naturalCompletionTolerance: TimeInterval = 2

    static func shouldRecordPause(
        previousStatus: AVPlayer.TimeControlStatus?,
        currentStatus: AVPlayer.TimeControlStatus,
        shouldPlay: Bool,
        isChangingEyeOrder: Bool,
        hasCurrentItem: Bool,
        playerTime: TimeInterval,
        duration: TimeInterval
    ) -> Bool {
        guard previousStatus == .playing,
              currentStatus == .paused,
              shouldPlay,
              !isChangingEyeOrder,
              hasCurrentItem
        else {
            return false
        }
        return !isNaturalCompletionTimeJump(playerTime: playerTime, duration: duration)
    }

    static func isNaturalCompletionTimeJump(
        playerTime: TimeInterval,
        duration: TimeInterval
    ) -> Bool {
        guard playerTime.isFinite, duration.isFinite, duration > 0 else {
            return false
        }
        return abs(duration - playerTime) <= naturalCompletionTolerance
    }

    static func shouldRecordSeek(
        isSceneActive: Bool,
        isChangingEyeOrder: Bool,
        hasEstablishedPlayback: Bool,
        playerTime: TimeInterval
    ) -> Bool {
        isSceneActive
            && !isChangingEyeOrder
            && hasEstablishedPlayback
            && playerTime.isFinite
            && playerTime > 0.05
    }

    static func programmaticSeekSuppressionIndex(
        playerTime: TimeInterval,
        currentUptime: TimeInterval,
        suppressions: [PlaybackQualificationProgrammaticSeek]
    ) -> Int? {
        guard playerTime.isFinite else {
            return nil
        }
        let activeSuppressions = suppressions.indices.filter {
            suppressions[$0].expiresAtUptime >= currentUptime
        }
        return activeSuppressions.first {
            abs(suppressions[$0].targetTime - playerTime) <= 1
        } ?? activeSuppressions.first {
            suppressions[$0].isInFlight
        }
    }

    static func isDuplicateNativeSeek(
        playerTime: TimeInterval,
        currentUptime: TimeInterval,
        previousPlayerTime: TimeInterval?,
        previousUptime: TimeInterval?
    ) -> Bool {
        guard let previousPlayerTime,
              let previousUptime,
              currentUptime >= previousUptime
        else {
            return false
        }
        return currentUptime - previousUptime <= 1
            && abs(playerTime - previousPlayerTime) <= 1
    }

    static func shouldSuppressSeekAfterNativeResume(
        playerTime: TimeInterval,
        nativePausePlayerTime: TimeInterval?,
        nativePausePending: Bool,
        currentUptime: TimeInterval,
        suppressionUntilUptime: TimeInterval
    ) -> Bool {
        if currentUptime <= suppressionUntilUptime {
            return true
        }
        guard nativePausePending,
              let nativePausePlayerTime,
              playerTime.isFinite,
              nativePausePlayerTime.isFinite
        else {
            return false
        }
        return abs(playerTime - nativePausePlayerTime) <= 1
    }
}
#endif

enum MVHEVCPlayerSessionState: Equatable {
    case idle
    case loading
    case ready
    case failed
}

enum RelayPlaybackTerminationReason: Equatable {
    case sessionEnded
    case serverUnreachable
    case rejected
    /// The live stream stopped or failed on the Mac. The session stays valid.
    case streamEnded
}

/// What `prepareRelayPlayback` did. Only `installed` hands the playback to the caller.
enum RelayPlaybackPreparationOutcome: Equatable {
    case installed
    case failed(PlaybackFailurePresentation)
    case superseded
}

/// The parts of a live ISO stream the player session needs beyond the relay configuration.
struct RelayLivePlaybackContext: Sendable {
    let title: String
    let streamID: UUID
    /// The title's full length from the Mac's live title index, when the Mac knows it.
    let durationSeconds: TimeInterval?
    /// Where the last position in this title is kept. A live title cannot resume yet, so the
    /// position is only used to say so when the wearer comes back to it.
    let resumeID: String?
    /// The Mac's parent-signed live status, for the busy notice and why a stream ended.
    let status: @Sendable () async -> LiveStreamStatus?
    /// Called once when installed live playback is torn down for any reason (failure,
    /// stream end, Done, replacement), so the owner of the stream's cleanup stops it.
    let ended: @MainActor @Sendable () -> Void

    init(
        title: String,
        streamID: UUID,
        durationSeconds: TimeInterval? = nil,
        resumeID: String? = nil,
        status: @escaping @Sendable () async -> LiveStreamStatus?,
        ended: @escaping @MainActor @Sendable () -> Void = {}
    ) {
        self.title = title
        self.streamID = streamID
        self.durationSeconds = durationSeconds
        self.resumeID = resumeID
        self.status = status
        self.ended = ended
    }
}

struct RelayPlaybackTerminationEvent: Equatable {
    let id: UUID
    let sessionID: String
    let reason: RelayPlaybackTerminationReason

    init(id: UUID = UUID(), sessionID: String, reason: RelayPlaybackTerminationReason) {
        self.id = id
        self.sessionID = sessionID
        self.reason = reason
    }
}

@MainActor
final class MVHEVCPlayerSession: ObservableObject {
    let player = AVPlayer()
    let playerEntity = Entity()

    @Published private(set) var state: MVHEVCPlayerSessionState = .idle
    @Published private(set) var mediaItem: MediaItem?
    @Published private(set) var failureMessage: String?
    @Published private(set) var failurePresentation: PlaybackFailurePresentation?
    @Published private(set) var preparationPhase: PlaybackPreparationPhase = .openingSource
    @Published private(set) var isPlaying = false
    @Published private(set) var currentTime: TimeInterval = 0
    @Published private(set) var duration: TimeInterval = 0
    /// The title's full length as reported before playback, for a live title whose stream is
    /// still growing. Nil when the player's own duration is already the full length.
    @Published private(set) var titleDuration: TimeInterval?
    /// The player's buffered range around the playhead, for file and shared-movie playback.
    @Published private(set) var bufferedRange: ClosedRange<TimeInterval>?
    @Published private(set) var seekNotice: String?
    @Published private(set) var audioOptions: [PlaybackMediaOption] = []
    @Published private(set) var subtitleOptions: [PlaybackMediaOption] = []
    @Published private(set) var selectedAudioID = ""
    @Published private(set) var selectedSubtitleID = "off"
    @Published private(set) var isEyeSwapped = false
    @Published private(set) var isChangingEyeOrder = false
    @Published private(set) var relayTerminationEvent: RelayPlaybackTerminationEvent?
    /// For live ISO playback, a notice such as the Mac being busy converting.
    @Published private(set) var liveNotice: String?
    /// Says why a title the wearer watched before starts from the beginning.
    @Published private(set) var resumeNotice: String?
    /// Native Blu-ray 3D playback is waiting for more of the title to load.
    @Published private(set) var isBuffering = false
    /// Replaces the subtitle list's note, for a title whose subtitles cannot show yet.
    @Published private(set) var subtitleListNote: String?

    static let liveBusyNotice = "The Mac is busy converting; playback may pause."
    static let liveForwardBufferDuration: TimeInterval = 20

    private(set) var playerItem: AVPlayerItem?

    private var resourceLease: SecurityScopedResourceLease?
    /// Blu-ray 3D playback decoded on the headset, used instead of `player`.
    private var nativePlayback: NativeBlurayPlayback?
    private var nativeStatusTask: Task<Void, Never>?
    private var remotePlaybackSource: RelayRemotePlaybackSource?
    private var relayTransport: (any RelayTransport)?
    private var relayWindowRefreshTask: Task<Void, Never>?
    private var consecutiveRelayRefreshFailures = 0
    private var liveContext: RelayLivePlaybackContext?
    /// The live context whose playback is installed; its `ended` runs when that playback goes.
    private var installedLiveContext: RelayLivePlaybackContext?
    private var liveStatusRefreshCount = 0
    private var resumeStore: ResumeStore?
    /// The key the resume position is saved under, or nil when this playback keeps none.
    private var resumeID: String?
    private var lastResumeSaveUptime: TimeInterval?
    private var itemStatusObservation: NSKeyValueObservation?
    private var timeControlStatusObservation: NSKeyValueObservation?
    private var timeObserver: Any?
    private var playbackFinishedObserver: NSObjectProtocol?
    private var audioGroup: AVMediaSelectionGroup?
    private var subtitleGroup: AVMediaSelectionGroup?
    private var audioSelectionByID: [String: AVMediaSelectionOption] = [:]
    private var subtitleSelectionByID: [String: AVMediaSelectionOption] = [:]
    private var packedStereoSource: PackedStereoSource?
    private var pendingItemRestoration: PlaybackItemRestorationState?
    private var playbackIntent = PlaybackIntentState()
    private var eyeOrderChangeResumeTime: TimeInterval?
    private var eyeOrderChangeTask: Task<Void, Never>?
#if BD_TO_AVP_QUALIFICATION
    private var pendingQualificationEyeOrderChange = false
    private var qualificationRecorder: PlaybackQualificationRecorder?
    private var qualificationLastTimeControlStatus: AVPlayer.TimeControlStatus?
    private var qualificationNativePausePending = false
    private var qualificationNativePausePlayerTime: TimeInterval?
    private var qualificationProgrammaticSeeks: [PlaybackQualificationProgrammaticSeek] = []
    private var qualificationLastNativeSeekTime: TimeInterval?
    private var qualificationLastNativeSeekUptime: TimeInterval?
    private var qualificationNativeResumeSeekSuppressionUntilUptime: TimeInterval = 0
    private var qualificationTimeJumpedObserver: NSObjectProtocol?
#endif
    private var preparationGeneration = 0
    private var pendingResume = PlaybackPendingResumeState()
    private var hasEstablishedPlayback = false

    init() {
        timeControlStatusObservation = player.observe(\.timeControlStatus, options: [.initial, .new]) { [weak self] observedPlayer, change in
            let status = change.newValue ?? observedPlayer.timeControlStatus
#if BD_TO_AVP_QUALIFICATION
            let capturedAt = Date()
            let capturedUptime = ProcessInfo.processInfo.systemUptime
            let playerTimeSeconds = observedPlayer.currentTime().seconds
#endif
            Task { @MainActor in
                self?.isPlaying = status == .playing
#if BD_TO_AVP_QUALIFICATION
                self?.qualificationRecorder?.recordTimeControlChanged(
                    status: status,
                    playerTimeSeconds: playerTimeSeconds,
                    capturedAt: capturedAt,
                    capturedUptime: capturedUptime
                )
                self?.handleNativeTimeControlTransition(
                    to: status,
                    playerTimeSeconds: playerTimeSeconds,
                    capturedUptime: capturedUptime
                )
#endif
            }
        }
    }

    deinit {
        if let timeObserver {
            player.removeTimeObserver(timeObserver)
        }
        if let playbackFinishedObserver {
            NotificationCenter.default.removeObserver(playbackFinishedObserver)
        }
#if BD_TO_AVP_QUALIFICATION
        if let qualificationTimeJumpedObserver {
            NotificationCenter.default.removeObserver(qualificationTimeJumpedObserver)
        }
#endif
        resourceLease?.close()
    }

    var isLoading: Bool {
        state == .loading
    }

    var isReady: Bool {
        state == .ready
    }

    var canControlPlayback: Bool {
        playbackIntent.isSceneActive
            && isReady
            && !isChangingEyeOrder
            && (nativePlayback != nil || playerItem?.status == .readyToPlay)
    }

    var canSeek: Bool {
        guard canControlPlayback else {
            return false
        }
        if let remotePlaybackSource {
            return remotePlaybackSource.retainedSeekPolicy.latestAvailableTime
                > remotePlaybackSource.retainedSeekPolicy.earliestPlayableTime
        }
        if nativePlayback != nil {
            return bufferedRange.map { $0.upperBound > $0.lowerBound } ?? false
        }
        return duration.isFinite && duration > 0
    }

    var earliestSeekTime: TimeInterval {
        remotePlaybackSource?.retainedSeekPolicy.earliestPlayableTime ?? 0
    }

    /// The scrub bar's full span and the range that can be played right now.
    var timeline: PlaybackTimeline {
        if let remotePlaybackSource {
            let policy = remotePlaybackSource.retainedSeekPolicy
            return PlaybackTimeline(
                titleDuration: titleDuration,
                playerDuration: duration,
                loadedRange: policy.earliestPlayableTime ... max(policy.earliestPlayableTime, policy.latestAvailableTime),
                isFinal: policy.window?.isFinalized == true
            )
        }
        return PlaybackTimeline(
            titleDuration: nil,
            playerDuration: duration,
            loadedRange: bufferedRange ?? currentTime ... currentTime
        )
    }

    var supportsEyeSwap: Bool {
        packedStereoSource != nil
    }

    var isRelayPlayback: Bool {
        mediaItem?.id.hasPrefix("relay:") == true
    }

    /// The live stream being played, if this relay playback is a live ISO title.
    var liveStreamID: UUID? { liveContext?.streamID }

    func prepare(
        mediaItem: MediaItem,
        bookmarkStore: BookmarkStore,
        resumeStore: ResumeStore,
        sharedMovie: SharedMoviePlayback? = nil
    ) async {
        preparationGeneration += 1
        let generation = preparationGeneration
        finishCurrentSession(persistResume: true)
        liveContext = nil

        self.mediaItem = mediaItem
        if mediaItem.format != .mvHEVC {
            playerEntity.components.remove(VideoPlayerComponent.self)
        }
        self.resumeStore = resumeStore
        resumeID = nil
        state = .loading
        failureMessage = nil
        failurePresentation = nil
        relayTerminationEvent = nil
        seekNotice = nil
        preparationPhase = .openingSource
        currentTime = 0
        duration = 0
        titleDuration = nil
        bufferedRange = nil
        audioOptions = []
        subtitleOptions = []
        selectedAudioID = ""
        selectedSubtitleID = "off"
        isEyeSwapped = false
        isChangingEyeOrder = false
        playbackIntent.requestPlayback()
#if BD_TO_AVP_QUALIFICATION
        configureQualificationRecorder(for: mediaItem)
        qualificationRecorder?.recordPrepare(player: player)
#endif

        guard sharedMovie != nil || mediaItem.format != .unsupported else {
            presentFailure(.unsupported)
            return
        }
        if sharedMovie == nil, mediaItem.format == .bluray3D {
            await prepareNativeBluray(mediaItem, bookmarkStore: bookmarkStore, resumeStore: resumeStore, generation: generation)
            return
        }

        let openedLease: SecurityScopedResourceLease
        let asset: AVURLAsset
        let makeAsset: () -> AVURLAsset
        do {
            if let sharedMovie {
                let loader = try MovieResourceLoader(movie: sharedMovie.movie) { offset, count in
                    try await sharedMovie.read(sharedMovie.movie, offset, count)
                }
                makeAsset = { loader.makeAsset() }
                asset = makeAsset()
                openedLease = SecurityScopedResourceLease(url: asset.url, startAccessing: { true }, stopAccessing: {
                    asset.cancelLoading()
                    loader.cancel()
                })
            } else {
                openedLease = try await bookmarkStore.open(id: mediaItem.id)
                makeAsset = { AVURLAsset(url: openedLease.url) }
                asset = makeAsset()
            }
        } catch {
            guard generation == preparationGeneration, !Task.isCancelled else {
                return
            }
            presentFailure(Self.sourceFailurePresentation(for: error))
            return
        }

        guard generation == preparationGeneration, !Task.isCancelled else {
            openedLease.close()
            return
        }
        resourceLease = openedLease
        preparationPhase = .preparingMedia

        do {
            let detectedFormat = try await MediaFormatInspector.inspect(asset: asset, fileName: mediaItem.fileName)
            guard generation == preparationGeneration, !Task.isCancelled else {
                if resourceLease === openedLease {
                    resourceLease = nil
                }
                openedLease.close()
                return
            }
            let mediaItem = sharedMovie.map {
                MediaItem(id: $0.movie.resumeID, title: $0.movie.title, fileName: $0.movie.fileName, format: detectedFormat)
            } ?? mediaItem
            self.mediaItem = mediaItem
            resumeID = mediaItem.id
            if let sharedMovie {
                try? resumeStore.adoptPosition(from: sharedMovie.movie.legacyResumeID, to: sharedMovie.movie.resumeID)
            }
            guard detectedFormat != .unsupported else {
                resourceLease = nil
                openedLease.close()
                presentFailure(.unsupported)
                return
            }
            guard detectedFormat == mediaItem.format else {
                if resourceLease === openedLease {
                    resourceLease = nil
                }
                openedLease.close()
                presentFailure(.sourceMismatch(detectedFormat: detectedFormat, expectedFormat: mediaItem.format))
                return
            }

            let item = AVPlayerItem(asset: asset)
            async let loadedDuration = asset.load(.duration)
            async let mediaSelections = prepareMediaSelections(for: asset, item: item)
            let preparedDuration = try await loadedDuration
            let preparedSelections = try await mediaSelections
            let preparedPackedStereo: PackedStereoSource?
            switch mediaItem.format {
            case .sideBySide, .overUnder:
                let spatialMetadataFallback: PackedStereoSpatialMetadata? =
                    BuiltInStereoChecks.contains(mediaItem) ? .qualificationFixture : nil
                item.videoComposition = try await PackedStereoComposition.make(
                    asset: asset,
                    format: mediaItem.format,
                    duration: preparedDuration,
                    eyeOrder: .normal,
                    spatialMetadataFallback: spatialMetadataFallback
                )
                item.seekingWaitsForVideoCompositionRendering = true
                preparedPackedStereo = PackedStereoSource(
                    makeAsset: makeAsset,
                    format: mediaItem.format,
                    duration: preparedDuration,
                    spatialMetadataFallback: spatialMetadataFallback
                )
            case .mvHEVC, .bluray3D, .unsupported:
                preparedPackedStereo = nil
            }

            guard generation == preparationGeneration, !Task.isCancelled else {
                if resourceLease === openedLease {
                    resourceLease = nil
                }
                openedLease.close()
                return
            }

            playerItem = item
            packedStereoSource = preparedPackedStereo
            duration = preparedDuration.seconds.isFinite ? max(0, preparedDuration.seconds) : 0
            configureMediaSelections(preparedSelections)
            pendingResume.store(resumeStore.resumeTime(for: mediaItem.id), duration: duration)
            observe(item, generation: generation)
            player.replaceCurrentItem(with: item)
        } catch is CancellationError {
            if resourceLease === openedLease {
                resourceLease = nil
            }
            openedLease.close()
            if generation == preparationGeneration {
                pendingResume.clear()
            }
        } catch {
            if resourceLease === openedLease {
                resourceLease = nil
            }
            openedLease.close()
            guard generation == preparationGeneration else {
                return
            }
            presentFailure(
                .preparationFailed("The movie could not be prepared: \(Self.playbackFailureMessage(for: error))")
            )
        }
    }

    @discardableResult
    func prepareRelayPlayback(
        _ configuration: RelayRemotePlaybackConfiguration,
        live: RelayLivePlaybackContext? = nil,
        resumeStore liveResumeStore: ResumeStore? = nil
    ) async -> RelayPlaybackPreparationOutcome {
        preparationGeneration += 1
        let generation = preparationGeneration
        finishCurrentSession(persistResume: true)

        // A live stream's session ID is its stream ID, so the item identity names the stream.
        let relayItem = MediaItem(
            id: "relay:\(configuration.session.sessionID.rawValue)",
            title: live?.title ?? "\(configuration.serverName) Live Relay",
            fileName: "live.m3u8",
            format: .mvHEVC
        )
        liveContext = live
        mediaItem = relayItem
        resumeID = live?.resumeID
        resumeStore = resumeID == nil ? nil : liveResumeStore
        resumeNotice = resumeID
            .flatMap { liveResumeStore?.resumeTime(for: $0) }
            .flatMap(ResumeWritePolicy.liveResumeNotice(savedPosition:))
        state = .loading
        failureMessage = nil
        failurePresentation = nil
        relayTerminationEvent = nil
        seekNotice = nil
        preparationPhase = .preparingMedia
        currentTime = 0
        duration = 0
        titleDuration = nil
        bufferedRange = nil
        audioOptions = []
        subtitleOptions = []
        selectedAudioID = ""
        selectedSubtitleID = "off"
        isEyeSwapped = false
        isChangingEyeOrder = false
        playbackIntent.requestPlayback()

        do {
            var source = try RelayRemotePlaybackSource(
                session: configuration.session,
                serverBaseURL: configuration.serverBaseURL
            )
            var sourceInstalled = false
            defer { if !sourceInstalled { source.cancelLoader() } }
            try await source.refreshRetainedWindow(transport: configuration.transport)
            let (asset, _) = try await source.makeAssetAndLoader(transport: configuration.transport)
            let item = AVPlayerItem(asset: asset)
            if live != nil {
                // The Mac paces production to the reported playhead; a modest forward buffer
                // keeps AVPlayer from racing to the live edge.
                item.preferredForwardBufferDuration = Self.liveForwardBufferDuration
            }
            let preparedSelections = try await prepareMediaSelections(for: asset, item: item)
            guard generation == preparationGeneration, !Task.isCancelled else {
                source.cancelLoader()
                return .superseded
            }

            sourceInstalled = true
            packedStereoSource = nil
            configureMediaSelections(preparedSelections)
            installRelayPlaybackResources(
                source: source,
                transport: configuration.transport,
                item: item,
                mediaItem: relayItem
            )
            return .installed
        } catch is CancellationError {
            if generation == preparationGeneration {
                pendingResume.clear()
            }
            return .superseded
        } catch {
            guard generation == preparationGeneration else {
                return .superseded
            }
            let presentation = PlaybackFailurePresentation.relayPreparationFailed(
                await relayFailureMessage(for: error, generation: generation)
            )
            guard generation == preparationGeneration else {
                return .superseded
            }
            terminateRelayPlayback(
                presentation: presentation,
                reason: Self.relayTerminationReason(for: error),
                emitEvent: true
            )
            return .failed(presentation)
        }
    }

    /// The message for a relay failure. A live stream that ended shows the Mac's reason, never
    /// a request to pair again; only a revoked session asks for that.
    private func relayFailureMessage(for error: Error, generation: Int) async -> String {
        switch error {
        case RelayTransportError.sessionRevoked, RelayTransportError.sessionExpired, RelayTransportError.unpaired:
            return Self.sessionEndedMessage
        case let RelayTransportError.streamEnded(code, message):
            let status = await liveContext?.status()
            let reason = status?.failure?.message ?? message ?? Self.liveStopDescription(status?.stopReason ?? code)
            return "Live playback stopped on the Mac: \(reason)"
        default:
            return Self.playbackFailureMessage(for: error)
        }
    }

    static let sessionEndedMessage = "The relay session ended. Pair with the Mac again to continue."

    static func liveStopDescription(_ code: String) -> String {
        switch code {
        case "viewerStopped": "playback was stopped."
        case "replaced": "another live title started."
        case "cancelled": "the start was cancelled."
        case "sessionEnded": "this Vision Pro's access ended."
        case "idle": "the stream was idle too long."
        case "sharingStopped": "Movie Sharing was turned off."
        case "appQuit": "the Mac app quit."
        case "sourceChanged": "the disc image changed or is no longer available."
        case RelayLiveStreamEnded.notCurrentCode: "this stream is no longer current."
        case "live_start_timeout": "the stream did not start in time."
        default: code
        }
    }

    func installRelayPlaybackResources(
        source: RelayRemotePlaybackSource,
        transport: any RelayTransport,
        item: AVPlayerItem,
        mediaItem: MediaItem,
        observeItemStatus: Bool = true,
        startRefreshLoop: Bool = true,
        live: RelayLivePlaybackContext? = nil
    ) {
        self.mediaItem = mediaItem
        if let live { liveContext = live }
        installedLiveContext = liveContext
        titleDuration = liveContext?.durationSeconds.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
        remotePlaybackSource = source
        relayTransport = transport
        playerItem = item
        state = .loading
        consecutiveRelayRefreshFailures = 0
        if observeItemStatus {
            observe(item, generation: preparationGeneration)
        }
        player.replaceCurrentItem(with: item)
        if startRefreshLoop {
            startRelayWindowRefresh(generation: preparationGeneration)
        }
    }

    func installPlayerComponent() {
        if let nativePlayback {
            guard playerEntity.components[VideoPlayerComponent.self] == nil else { return }
            var component = VideoPlayerComponent(videoRenderer: nativePlayback.renderer)
            component.desiredViewingMode = .stereo
            playerEntity.components.set(component)
            return
        }
        guard mediaItem?.format == .mvHEVC else {
            playerEntity.components.remove(VideoPlayerComponent.self)
            return
        }
        var component = playerEntity.components[VideoPlayerComponent.self] ?? VideoPlayerComponent(avPlayer: player)
        component.desiredViewingMode = .stereo
        component.desiredSpatialVideoMode = .screen
        component.desiredImmersiveViewingMode = .portal
        playerEntity.components.set(component)
    }

    func play() {
        playbackIntent.requestPlayback()
        if let nativePlayback {
            guard canControlPlayback, playbackIntent.shouldPlay else { return }
            nativePlayback.setPlaying(true)
            isPlaying = nativePlayback.status.wantsPlayback
            return
        }
        guard canControlPlayback,
              playbackIntent.shouldPlay,
              player.timeControlStatus != .playing
        else {
            return
        }
#if BD_TO_AVP_QUALIFICATION
        qualificationRecorder?.recordPlayRequested(player: player)
#endif
        player.play()
    }

    func pause() {
        playbackIntent.pause()
        if let nativePlayback {
            nativePlayback.setPlaying(false)
            isPlaying = false
            persistResume()
            return
        }
        guard playerItem != nil else {
            return
        }
        let shouldPausePlayer = player.timeControlStatus != .paused
#if BD_TO_AVP_QUALIFICATION
        if shouldPausePlayer {
            qualificationRecorder?.recordPauseRequested(player: player)
        }
#endif
        if shouldPausePlayer {
            player.pause()
        }
        persistResume()
        reportLivePositionNow()
    }

    func togglePlayback() {
        isPlaying ? pause() : play()
    }

    func seek(to requestedTime: TimeInterval) {
        if nativePlayback != nil {
            seekNative(to: requestedTime)
            return
        }
        seek(to: requestedTime, origin: .user, completion: nil)
    }

    private func seek(
        to requestedTime: TimeInterval,
        origin: PlaybackSeekOrigin,
        completion: ((Bool) -> Void)?
    ) {
        guard let playerItem else {
            return
        }

        let generation = preparationGeneration
        let adjustedRequestedTime: TimeInterval
        if origin == .user, let remotePlaybackSource {
            switch remotePlaybackSource.retainedSeekPolicy.validateSeek(to: requestedTime) {
            case .playable:
                seekNotice = nil
                adjustedRequestedTime = requestedTime
            case let .beforeRetainedHistory(earliestPlayableTime):
                seekNotice = "That part of the live relay is no longer retained. Continuing at \(PlaybackTimeFormatter.string(for: earliestPlayableTime))."
                adjustedRequestedTime = earliestPlayableTime
            case let .notYetAvailable(latestAvailableTime):
                seekNotice = "The live relay is available through \(PlaybackTimeFormatter.string(for: latestAvailableTime))."
                adjustedRequestedTime = max(earliestSeekTime, latestAvailableTime - 0.001)
            case let .ended(finalDuration):
                seekNotice = nil
                adjustedRequestedTime = min(requestedTime, finalDuration)
            case .invalidTime:
                return
            }
        } else {
            adjustedRequestedTime = requestedTime
        }
        let targetTime = PlaybackSeekPolicy.clampedTime(adjustedRequestedTime, duration: duration)
        if origin == .user {
            guard canSeek else {
                return
            }
            let currentPlayerTime = player.currentTime().seconds
            if currentPlayerTime.isFinite, abs(currentPlayerTime - targetTime) < 0.05 {
                completion?(true)
                return
            }
        }
#if BD_TO_AVP_QUALIFICATION
        qualificationRecorder?.recordSeekStarted(
            player: player,
            detail: origin.qualificationDetail
        )
        let qualificationSeekID = registerQualificationProgrammaticSeek(targetTime: targetTime)
#endif
        player.seek(
            to: CMTime(seconds: targetTime, preferredTimescale: 600),
            toleranceBefore: .zero,
            toleranceAfter: .zero
        ) { [weak self, weak playerItem] completed in
            Task { @MainActor in
                guard let self,
                      generation == self.preparationGeneration,
                      playerItem === self.playerItem
                else {
                    return
                }
                if completed {
                    self.currentTime = targetTime
                    self.reportLivePositionNow()
                }
#if BD_TO_AVP_QUALIFICATION
                if completed {
                    self.qualificationRecorder?.recordSeekCompleted(
                        player: self.player,
                        detail: origin.qualificationDetail
                    )
                }
                self.completeQualificationProgrammaticSeek(id: qualificationSeekID)
#endif
                completion?(completed)
            }
        }
    }

    func seekBackward() {
        seek(to: currentTime - 10)
    }

    func seekForward() {
        seek(to: currentTime + 30)
    }

    func selectAudio(id: String) {
        if let nativePlayback {
            guard let track = nativePlayback.audioTracks.first(where: { $0.mediaOption.id == id }) else { return }
            nativePlayback.selectAudio(trackID: track.id)
            selectedAudioID = id
            return
        }
        guard let audioGroup,
              let selection = audioSelectionByID[id],
              let playerItem
        else {
            return
        }
        playerItem.select(selection, in: audioGroup)
        refreshSelectedMediaOptionIDs()
    }

    func selectSubtitle(id: String) {
        if nativePlayback != nil {
            // Blu-ray subtitles are not drawn yet (#1114), so Off is the only choice.
            return
        }
        guard let subtitleGroup, let playerItem else {
            return
        }

        if id == "off" {
            playerItem.select(nil, in: subtitleGroup)
            selectedSubtitleID = id
            return
        }

        guard let selection = subtitleSelectionByID[id] else {
            return
        }
        playerItem.select(selection, in: subtitleGroup)
        selectedSubtitleID = id
    }

    func toggleEyeSwap() {
        guard let packedStereoSource,
              let playerItem,
              canControlPlayback,
              !isChangingEyeOrder
        else {
            return
        }

        let targetEyeOrder: PackedStereoEyeOrder = isEyeSwapped ? .normal : .reversed
        let restoration = PlaybackItemRestorationState(
            time: player.currentTime(),
            wasPlaying: player.timeControlStatus == .playing,
            mediaSelection: currentMediaSelectionRestoration()
        )
        let generation = preparationGeneration
        eyeOrderChangeResumeTime = restoration.time.seconds.isFinite
            ? restoration.time.seconds
            : currentTime
        isChangingEyeOrder = true
#if BD_TO_AVP_QUALIFICATION
        pendingQualificationEyeOrderChange = true
        qualificationRecorder?.recordEyeOrderChangeStarted(player: player)
#endif
        playbackIntent.preservePlaybackIntent(wasPlaying: restoration.wasPlaying)
        player.pause()

        eyeOrderChangeTask = Task { [weak self, weak playerItem] in
            guard let self else {
                return
            }
            defer { eyeOrderChangeTask = nil }
            do {
                let replacementAsset = packedStereoSource.makeAsset()
                let replacementItem = AVPlayerItem(asset: replacementAsset)
                async let replacementComposition = PackedStereoComposition.make(
                    asset: replacementAsset,
                    format: packedStereoSource.format,
                    duration: packedStereoSource.duration,
                    eyeOrder: targetEyeOrder,
                    spatialMetadataFallback: packedStereoSource.spatialMetadataFallback
                )
                async let replacementSelections = prepareMediaSelections(
                    for: replacementAsset,
                    item: replacementItem
                )
                replacementItem.videoComposition = try await replacementComposition
                replacementItem.seekingWaitsForVideoCompositionRendering = true
                let preparedSelections = try await replacementSelections

                guard generation == preparationGeneration,
                      playerItem === self.playerItem,
                      !Task.isCancelled
                else {
                    if generation == preparationGeneration {
                        isChangingEyeOrder = false
                        playbackIntent.pause()
                        eyeOrderChangeResumeTime = nil
                    }
                    return
                }

                preparationGeneration += 1
                let replacementGeneration = preparationGeneration
                self.playerItem = replacementItem
                configureMediaSelections(preparedSelections)
                pendingItemRestoration = restoration
                isEyeSwapped = targetEyeOrder == .reversed
                failureMessage = nil
                observe(replacementItem, generation: replacementGeneration)
                player.replaceCurrentItem(with: replacementItem)
            } catch {
                guard generation == preparationGeneration,
                      playerItem === self.playerItem
                else {
                    return
                }
                isChangingEyeOrder = false
#if BD_TO_AVP_QUALIFICATION
                qualificationRecorder?.recordEyeOrderChangeFailed(player: player)
                pendingQualificationEyeOrderChange = false
#endif
                failureMessage = "Eye order could not be changed: \(error.localizedDescription)"
                if playbackIntent.shouldPlay {
                    player.play()
                }
                eyeOrderChangeResumeTime = nil
            }
        }
    }

    /// Follows the scene's phase. Playback pauses only when the app goes to the background.
    /// An inactive scene, such as while Control Center is open or the wearer looks at another
    /// window, keeps playing and only saves the resume position.
    func handleScenePhase(_ phase: ScenePhase) {
        switch phase {
        case .active:
            applicationBecameActive()
        case .inactive:
            applicationBecameInactive()
        case .background:
            applicationEnteredBackground()
        @unknown default:
            applicationEnteredBackground()
        }
    }

    func applicationBecameInactive() {
        guard playerItem != nil || nativePlayback != nil else {
            return
        }
        persistResume()
    }

    func applicationEnteredBackground() {
        playbackIntent.sceneEnteredBackground()
#if BD_TO_AVP_QUALIFICATION
        qualificationNativePausePending = false
        qualificationNativePausePlayerTime = nil
        qualificationRecorder?.recordSceneInactive(player: player)
#endif
        if let nativePlayback {
            nativePlayback.setPlaying(false)
            isPlaying = false
            persistResume()
            return
        }
        guard playerItem != nil else {
            return
        }
        player.pause()
        persistResume()
    }

    func applicationBecameActive() {
        playbackIntent.sceneBecameActive()
#if BD_TO_AVP_QUALIFICATION
        qualificationRecorder?.recordSceneActive(player: player)
#endif
    }

    func finish() {
        preparationGeneration += 1
        finishCurrentSession(persistResume: true)
        liveContext = nil
        mediaItem = nil
        resumeStore = nil
        resumeID = nil
        state = .idle
        failureMessage = nil
        failurePresentation = nil
        relayTerminationEvent = nil
        preparationPhase = .openingSource
    }

    // MARK: Native Blu-ray 3D

    /// Opens a Blu-ray 3D source and plays its title with `NativeBlurayPlayback` (#1111).
    private func prepareNativeBluray(
        _ mediaItem: MediaItem,
        bookmarkStore: BookmarkStore,
        resumeStore: ResumeStore,
        generation: Int
    ) async {
        let openedLease: SecurityScopedResourceLease
        do {
            openedLease = try await bookmarkStore.open(id: mediaItem.id)
        } catch {
            guard generation == preparationGeneration, !Task.isCancelled else { return }
            presentFailure(Self.sourceFailurePresentation(for: error))
            return
        }
        guard generation == preparationGeneration, !Task.isCancelled else {
            openedLease.close()
            return
        }
        resourceLease = openedLease
        preparationPhase = .preparingMedia

        let url = openedLease.url
        let source: Bluray3DSource
        do {
            source = try await Task.detached(priority: .userInitiated) { try Bluray3DSourceOpener.open(url) }.value
        } catch {
            guard generation == preparationGeneration, !Task.isCancelled else { return }
            presentFailure(.preparationFailed(
                (error as? LocalizedError)?.errorDescription ?? Bluray3DSourceError.openFailed.localizedDescription
            ))
            return
        }
        guard generation == preparationGeneration, !Task.isCancelled else { return }
        let title = source.info.titles.first { $0.id == mediaItem.bluray3DTitle?.id } ?? source.info.mainTitle
        guard let title, title.isPlayable else {
            presentFailure(.preparationFailed(
                title?.unsupportedReason?.message ?? Bluray3DSourceError.titleUnavailable.localizedDescription
            ))
            return
        }

        let playback = NativeBlurayPlayback(source: source, title: title)
        nativePlayback = playback
        resumeID = mediaItem.id
        resumeNotice = resumeStore.resumeTime(for: mediaItem.id).flatMap(ResumeWritePolicy.blurayResumeNotice(savedPosition:))
        duration = title.durationSeconds
        audioOptions = playback.audioTracks.map(\.mediaOption)
        selectedAudioID = playback.audioTracks.first { $0.id == playback.selectedAudioTrackID }?.mediaOption.id ?? ""
        subtitleOptions = [PlaybackMediaOption(id: "off", displayName: "Off")]
        subtitleListNote = title.subtitleTracks.isEmpty ? nil : PlaybackTrackListNote.bluraySubtitlesLater
        isBuffering = true
        state = .ready
        playback.start()
        playback.setPlaying(playbackIntent.shouldPlay)
        isPlaying = playbackIntent.shouldPlay
        nativeStatusTask = Task { [weak self] in
            while !Task.isCancelled {
                self?.refreshNativeStatus(generation: generation)
                try? await Task.sleep(nanoseconds: 250_000_000)
            }
        }
    }

    private func refreshNativeStatus(generation: Int) {
        guard generation == preparationGeneration, let nativePlayback else { return }
        let status = nativePlayback.status
        if let failure = status.failure {
            nativeStatusTask?.cancel()
            nativeStatusTask = nil
            persistResume()
            presentFailure(.preparationFailed(failure))
            return
        }
        currentTime = status.currentTime
        bufferedRange = status.loadedRange
        if isBuffering != status.isBuffering {
            isBuffering = status.isBuffering
        }
        if isPlaying != status.wantsPlayback {
            isPlaying = status.wantsPlayback
            if !status.wantsPlayback {
                playbackIntent.pause()
                persistResume()
            }
        }
        // The resume position is kept once the title is actually playing, not while it loads.
        if !status.isBuffering {
            hasEstablishedPlayback = true
        }
        if isPlaying, ResumeWritePolicy.isPeriodicSaveDue(lastSaveUptime: lastResumeSaveUptime, now: ProcessInfo.processInfo.systemUptime) {
            persistResume()
        }
    }

    private func seekNative(to requestedTime: TimeInterval) {
        guard let nativePlayback, canSeek else { return }
        let target = PlaybackSeekPolicy.clampedTime(requestedTime, duration: duration)
        switch nativePlayback.seek(to: target) {
        case .seeking:
            seekNotice = nil
            currentTime = target
        case let .outside(loaded):
            seekNotice = PlaybackSeekPolicy.blurayOutsideLoadedNotice(target: target, loaded: loaded)
        }
    }

#if BD_TO_AVP_QUALIFICATION
    /// The native Blu-ray 3D engine, for the on-device qualification driver.
    var qualificationNativePlayback: NativeBlurayPlayback? { nativePlayback }
#endif

    private func observe(_ item: AVPlayerItem, generation: Int) {
        itemStatusObservation = item.observe(\.status, options: [.initial, .new]) { [weak self] observedItem, _ in
            Task { @MainActor in
                self?.handleItemStatus(observedItem, generation: generation)
            }
        }

        if let timeObserver {
            player.removeTimeObserver(timeObserver)
        }
        timeObserver = player.addPeriodicTimeObserver(
            forInterval: CMTime(seconds: 0.25, preferredTimescale: 600),
            queue: .main
        ) { [weak self] time in
            Task { @MainActor in
                guard let self,
                      generation == self.preparationGeneration,
                      item === self.playerItem
                else {
                    return
                }
                self.currentTime = max(0, time.seconds.isFinite ? time.seconds : 0)
                if self.isPlaying,
                   ResumeWritePolicy.isPeriodicSaveDue(
                       lastSaveUptime: self.lastResumeSaveUptime,
                       now: ProcessInfo.processInfo.systemUptime
                   )
                {
                    self.persistResume()
                }
                if self.remotePlaybackSource == nil {
                    let loadedRanges = item.loadedTimeRanges.compactMap { value -> ClosedRange<TimeInterval>? in
                        let range = value.timeRangeValue
                        let start = range.start.seconds
                        let end = range.end.seconds
                        guard start.isFinite, end.isFinite, end >= start else { return nil }
                        return start ... end
                    }
                    self.bufferedRange = PlaybackTimeline.bufferedRange(containing: self.currentTime, in: loadedRanges)
                }
#if BD_TO_AVP_QUALIFICATION
                self.qualificationRecorder?.recordSampleIfNeeded(
                    player: self.player,
                    durationSeconds: self.duration
                )
#endif
            }
        }

        if let playbackFinishedObserver {
            NotificationCenter.default.removeObserver(playbackFinishedObserver)
        }
        playbackFinishedObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime,
            object: item,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                guard let self,
                      generation == self.preparationGeneration,
                      item === self.playerItem
                else {
                    return
                }
                self.currentTime = self.duration
#if BD_TO_AVP_QUALIFICATION
                self.qualificationRecorder?.recordPlaybackFinished(
                    player: self.player,
                    durationSeconds: self.duration
                )
#endif
                self.persistResume()
            }
        }
#if BD_TO_AVP_QUALIFICATION
        if let qualificationTimeJumpedObserver {
            NotificationCenter.default.removeObserver(qualificationTimeJumpedObserver)
        }
        qualificationTimeJumpedObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemTimeJumped,
            object: item,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                self?.handleNativeTimeJump(item: item, generation: generation)
            }
        }
#endif
    }

    private func handleItemStatus(_ item: AVPlayerItem, generation: Int) {
        guard generation == preparationGeneration, item === playerItem else {
            return
        }

        switch item.status {
        case .unknown:
            break
        case .readyToPlay:
            state = .ready
            hasEstablishedPlayback = true
            failurePresentation = nil
#if BD_TO_AVP_QUALIFICATION
            qualificationRecorder?.recordReady(player: player)
#endif
            if let restoration = pendingItemRestoration {
                pendingItemRestoration = nil
                restoreMediaSelections(restoration.mediaSelection, on: item)
                refreshSelectedMediaOptionIDs()
                seek(
                    to: restoration.time.seconds,
                    origin: .eyeOrderRestoration
                ) { [weak self] completed in
                    guard let self else {
                        return
                    }
                    self.isChangingEyeOrder = false
                    self.eyeOrderChangeResumeTime = nil
                    if !completed {
                        self.failureMessage = "Eye order changed, but the previous playback position could not be restored."
#if BD_TO_AVP_QUALIFICATION
                        self.qualificationRecorder?.recordEyeOrderChangeFailed(player: self.player)
                        self.pendingQualificationEyeOrderChange = false
#endif
                    } else {
#if BD_TO_AVP_QUALIFICATION
                        if self.pendingQualificationEyeOrderChange {
                            self.qualificationRecorder?.recordEyeOrderChangeCompleted(player: self.player)
                        }
                        self.pendingQualificationEyeOrderChange = false
#endif
                    }
                    if self.playbackIntent.shouldPlay {
                        self.player.play()
                    }
                }
                return
            }
            assertInitialPackedStereoAudioSelection(on: item)
            refreshSelectedMediaOptionIDs()
            if let resumeTime = pendingResume.consume() {
                seek(to: resumeTime, origin: .resumeRestoration) { [weak self] _ in
                    guard let self, self.playbackIntent.shouldPlay else {
                        return
                    }
                    self.player.play()
                }
            } else if playbackIntent.shouldPlay {
                player.play()
            }
        case .failed:
            let message = Self.playbackFailureMessage(for: item.error)
            if isRelayPlayback, liveContext != nil {
                // AVPlayer only sees the loopback bridge's flattened 503. Ask the Mac, through
                // the verified snapshot route, whether the stream ended before reporting it.
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    await self.refreshRelayWindow(generation: generation)
                    guard generation == self.preparationGeneration, self.state != .failed else { return }
                    self.terminateRelayPlayback(
                        presentation: .relayPreparationFailed(message),
                        reason: item.error.map(Self.relayTerminationReason(for:)) ?? .serverUnreachable,
                        emitEvent: true
                    )
                }
            } else if isRelayPlayback {
                terminateRelayPlayback(
                    presentation: .relayPreparationFailed(message),
                    reason: item.error.map(Self.relayTerminationReason(for:)) ?? .serverUnreachable,
                    emitEvent: true
                )
            } else if BuiltInStereoChecks.contains(mediaItem) {
                presentFailure(.builtInStereoCheckUnavailable(message))
            } else {
                presentFailure(.preparationFailed(message))
            }
        @unknown default:
            presentFailure(
                .preparationFailed("The player returned an unknown playback status.")
            )
        }
    }

    private static func playbackFailureMessage(for error: Error?) -> String {
        guard let error else {
            return "The player failed without an error description."
        }

        var messages: [String] = []
        var currentError: NSError? = error as NSError
        while let unwrappedError = currentError {
            let message = unwrappedError.localizedDescription
            if !messages.contains(message) {
                messages.append(message)
            }
            currentError = unwrappedError.userInfo[NSUnderlyingErrorKey] as? NSError
        }
        return messages.joined(separator: " ")
    }

    private func prepareMediaSelections(
        for asset: AVAsset,
        item: AVPlayerItem
    ) async throws -> PreparedMediaSelections {
        async let loadedAudioGroup = asset.loadMediaSelectionGroup(for: .audible)
        async let loadedSubtitleGroup = asset.loadMediaSelectionGroup(for: .legible)
        let (audioGroup, subtitleGroup) = try await (loadedAudioGroup, loadedSubtitleGroup)

        var audioSelections: [String: AVMediaSelectionOption] = [:]
        let audioOptionIDs: [PlaybackMediaOption]
        if let audioGroup {
            audioOptionIDs = audioGroup.options.enumerated().map { index, option in
                let id = "audio-\(index)"
                audioSelections[id] = option
                return PlaybackMediaOption(id: id, displayName: option.displayName)
            }
        } else {
            audioOptionIDs = []
        }
        let labeledAudioOptions: [PlaybackMediaOption]
        let selectedAudioID: String
        if let audioGroup {
            let labelMetadata = audioGroup.options.enumerated().map { index, option in
                PlaybackAudioOptionLabelMetadata(
                    baseName: option.displayName,
                    role: Self.audioOptionRole(for: option),
                    index: index
                )
            }
            let displayNames = PlaybackAudioOptionLabelPolicy.labels(for: labelMetadata)
            labeledAudioOptions = audioOptionIDs.enumerated().map { index, option in
                PlaybackMediaOption(id: option.id, displayName: displayNames[index])
            }
            selectedAudioID = Self.audioSelectionID(
                in: item,
                group: audioGroup,
                options: labeledAudioOptions
            )
        } else {
            labeledAudioOptions = []
            selectedAudioID = ""
        }

        var subtitleSelections: [String: AVMediaSelectionOption] = [:]
        let subtitleOptions: [PlaybackMediaOption]
        let selectedSubtitleID: String
        if let subtitleGroup {
            subtitleOptions = [PlaybackMediaOption(id: "off", displayName: "Off")]
                + subtitleGroup.options.enumerated().map { index, option in
                    let id = "subtitle-\(index)"
                    subtitleSelections[id] = option
                    return PlaybackMediaOption(id: id, displayName: option.displayName)
                }
            selectedSubtitleID = subtitleOptions.first(where: { option in
                guard let selection = subtitleSelections[option.id] else {
                    return false
                }
                return item.currentMediaSelection.selectedMediaOption(in: subtitleGroup) === selection
            })?.id ?? "off"
        } else {
            subtitleOptions = []
            selectedSubtitleID = "off"
        }

        return PreparedMediaSelections(
            audioGroup: audioGroup,
            subtitleGroup: subtitleGroup,
            audioOptions: labeledAudioOptions,
            subtitleOptions: subtitleOptions,
            selectedAudioID: selectedAudioID,
            selectedSubtitleID: selectedSubtitleID,
            audioSelectionByID: audioSelections,
            subtitleSelectionByID: subtitleSelections
        )
    }

    private func configureMediaSelections(_ selections: PreparedMediaSelections) {
        audioGroup = selections.audioGroup
        subtitleGroup = selections.subtitleGroup
        audioOptions = selections.audioOptions
        subtitleOptions = selections.subtitleOptions
        selectedAudioID = selections.selectedAudioID
        selectedSubtitleID = selections.selectedSubtitleID
        audioSelectionByID = selections.audioSelectionByID
        subtitleSelectionByID = selections.subtitleSelectionByID
    }

    private func refreshSelectedMediaOptionIDs() {
        if let audioGroup {
            selectedAudioID = Self.audioSelectionID(
                in: playerItem,
                group: audioGroup,
                options: audioOptions
            )
        }

        if let subtitleGroup {
            selectedSubtitleID = subtitleOptions.first(where: { option in
                guard let selection = subtitleSelectionByID[option.id] else {
                    return false
                }
                return playerItem?.currentMediaSelection.selectedMediaOption(in: subtitleGroup) === selection
            })?.id ?? "off"
        }
    }

    private func currentMediaSelectionRestoration() -> MediaSelectionRestorationState {
        let audioPropertyList = audioGroup.flatMap {
            playerItem?.currentMediaSelection.selectedMediaOption(in: $0)?.propertyList()
        }
        let subtitlePropertyList = subtitleGroup.flatMap {
            playerItem?.currentMediaSelection.selectedMediaOption(in: $0)?.propertyList()
        }
        return MediaSelectionRestorationState(
            audioPropertyList: audioPropertyList,
            audioID: selectedAudioID.isEmpty ? nil : selectedAudioID,
            subtitlePropertyList: subtitlePropertyList
        )
    }

    private func restoreMediaSelections(_ restoration: MediaSelectionRestorationState, on item: AVPlayerItem) {
        var restoredAudio = false
        if let audioGroup,
           let propertyList = restoration.audioPropertyList,
           let selection = audioGroup.mediaSelectionOption(withPropertyList: propertyList)
        {
            item.select(selection, in: audioGroup)
            restoredAudio = true
        }

        if !restoredAudio,
           let audioGroup,
           let audioID = restoration.audioID,
           let selection = audioSelectionByID[audioID]
        {
            item.select(selection, in: audioGroup)
        }

        if let subtitleGroup {
            if let propertyList = restoration.subtitlePropertyList,
               let selection = subtitleGroup.mediaSelectionOption(withPropertyList: propertyList)
            {
                item.select(selection, in: subtitleGroup)
            } else {
                item.select(nil, in: subtitleGroup)
            }
        }
    }

    private static func audioSelectionID(
        in item: AVPlayerItem?,
        group: AVMediaSelectionGroup,
        options: [PlaybackMediaOption]
    ) -> String {
        guard let selectedOption = item?.currentMediaSelection.selectedMediaOption(in: group) else {
            return ""
        }
        guard let selectedIndex = group.options.firstIndex(where: {
            mediaSelectionOptionsMatch($0, selectedOption)
        }), options.indices.contains(selectedIndex) else {
            return ""
        }
        return options[selectedIndex].id
    }

    private func assertInitialPackedStereoAudioSelection(on item: AVPlayerItem) {
        guard mediaItem?.format == .sideBySide || mediaItem?.format == .overUnder,
              let audioGroup
        else {
            return
        }

        let currentSelection = item.currentMediaSelection.selectedMediaOption(in: audioGroup)
        let playableOptions = AVMediaSelectionGroup.playableMediaSelectionOptions(from: audioGroup.options)
        let currentIndex = currentSelection.flatMap { selectedOption in
            audioGroup.options.firstIndex { Self.mediaSelectionOptionsMatch($0, selectedOption) }
        }
        let defaultIndex = audioGroup.defaultOption.flatMap { defaultOption in
            audioGroup.options.firstIndex { Self.mediaSelectionOptionsMatch($0, defaultOption) }
        }
        let playableIndices = playableOptions.compactMap { playableOption in
            audioGroup.options.firstIndex { Self.mediaSelectionOptionsMatch($0, playableOption) }
        }
        guard let selectionIndex = PlaybackAudioSelectionPolicy.preferredIndex(
            currentIndex: currentIndex,
            defaultIndex: defaultIndex,
            playableIndices: playableIndices
        ), audioGroup.options.indices.contains(selectionIndex) else {
            return
        }
        item.select(audioGroup.options[selectionIndex], in: audioGroup)
    }

    private static func mediaSelectionOptionsMatch(
        _ first: AVMediaSelectionOption,
        _ second: AVMediaSelectionOption
    ) -> Bool {
        if first === second {
            return true
        }
        guard let firstPropertyList = first.propertyList() as? NSObject,
              let secondPropertyList = second.propertyList() as? NSObject
        else {
            return false
        }
        return firstPropertyList.isEqual(secondPropertyList)
    }

    private static func audioOptionRole(for option: AVMediaSelectionOption) -> String? {
        if option.hasMediaCharacteristic(.describesVideoForAccessibility) {
            return "Audio Description"
        }
        if option.hasMediaCharacteristic(.isAuxiliaryContent) {
            return "Commentary"
        }
        if option.hasMediaCharacteristic(.dubbedTranslation) {
            return "Dubbed"
        }
        if option.hasMediaCharacteristic(.voiceOverTranslation) {
            return "Voice-over"
        }
        if option.hasMediaCharacteristic(.isOriginalContent) {
            return "Original"
        }
        return nil
    }

    private func finishCurrentSession(persistResume shouldPersistResume: Bool) {
        if let installed = installedLiveContext {
            installedLiveContext = nil
            installed.ended()
        }
        eyeOrderChangeTask?.cancel()
        eyeOrderChangeTask = nil
        if shouldPersistResume {
            persistResume(isFinishing: true)
        }
#if BD_TO_AVP_QUALIFICATION
        qualificationRecorder?.recordSessionFinished(
            player: player,
            durationSeconds: duration
        )
        qualificationRecorder = nil
        pendingQualificationEyeOrderChange = false
        qualificationLastTimeControlStatus = nil
        qualificationNativePausePending = false
        qualificationNativePausePlayerTime = nil
        qualificationProgrammaticSeeks = []
        qualificationLastNativeSeekTime = nil
        qualificationLastNativeSeekUptime = nil
        qualificationNativeResumeSeekSuppressionUntilUptime = 0
        if let qualificationTimeJumpedObserver {
            NotificationCenter.default.removeObserver(qualificationTimeJumpedObserver)
            self.qualificationTimeJumpedObserver = nil
        }
#endif
        player.pause()
        player.replaceCurrentItem(with: nil)
        nativeStatusTask?.cancel()
        nativeStatusTask = nil
        if let nativePlayback {
            nativePlayback.stop()
            self.nativePlayback = nil
            playerEntity.components.remove(VideoPlayerComponent.self)
        }
        isBuffering = false
        subtitleListNote = nil
        itemStatusObservation = nil
        playerItem = nil
        if let timeObserver {
            player.removeTimeObserver(timeObserver)
            self.timeObserver = nil
        }
        if let playbackFinishedObserver {
            NotificationCenter.default.removeObserver(playbackFinishedObserver)
            self.playbackFinishedObserver = nil
        }
        resourceLease?.close()
        resourceLease = nil
        remotePlaybackSource?.cancelLoader()
        remotePlaybackSource = nil
        relayTransport = nil
        relayWindowRefreshTask?.cancel()
        relayWindowRefreshTask = nil
        consecutiveRelayRefreshFailures = 0
        seekNotice = nil
        liveNotice = nil
        resumeNotice = nil
        liveStatusRefreshCount = 0
        titleDuration = nil
        bufferedRange = nil
        audioGroup = nil
        subtitleGroup = nil
        audioSelectionByID = [:]
        subtitleSelectionByID = [:]
        packedStereoSource = nil
        pendingItemRestoration = nil
        playbackIntent.reset()
        eyeOrderChangeResumeTime = nil
        audioOptions = []
        subtitleOptions = []
        selectedAudioID = ""
        selectedSubtitleID = "off"
        isEyeSwapped = false
        isChangingEyeOrder = false
        isPlaying = false
        pendingResume.clear()
        hasEstablishedPlayback = false
        lastResumeSaveUptime = nil
        failurePresentation = nil
        preparationPhase = .openingSource
    }

    private func persistResume(isFinishing: Bool = false) {
        guard ResumeWritePolicy.allowsWrite(
            isChangingEyeOrder: isChangingEyeOrder,
            isFinishing: isFinishing,
            hasEstablishedPlayback: hasEstablishedPlayback
        ), let resumeID, let resumeStore else {
            return
        }
        lastResumeSaveUptime = ProcessInfo.processInfo.systemUptime

        let resumeTime = ResumeWritePolicy.position(
            currentTime: currentTime,
            eyeOrderChangeTime: eyeOrderChangeResumeTime,
            isChangingEyeOrder: isChangingEyeOrder,
            isFinishing: isFinishing
        )

        do {
            switch ResumeWritePolicy.decision(currentTime: resumeTime, duration: titleDuration ?? duration) {
            case let .write(position):
                try resumeStore.setResumeTime(position, for: resumeID)
            case .remove:
                try resumeStore.remove(id: resumeID)
            case .skip:
                break
            }
            if state == .ready {
                failureMessage = nil
            }
        } catch {
            failureMessage = "Playback position could not be saved: \(error.localizedDescription)"
        }
    }

    private func presentFailure(_ presentation: PlaybackFailurePresentation) {
        player.pause()
#if BD_TO_AVP_QUALIFICATION
        qualificationRecorder?.recordFailure(
            player: player,
            durationSeconds: duration
        )
#endif
        state = .failed
        failureMessage = presentation.message
        failurePresentation = presentation
        isPlaying = false
        isChangingEyeOrder = false
        pendingItemRestoration = nil
        playbackIntent.reset()
        eyeOrderChangeResumeTime = nil
        pendingResume.clear()
    }

#if BD_TO_AVP_QUALIFICATION
    private func handleNativeTimeControlTransition(
        to status: AVPlayer.TimeControlStatus,
        playerTimeSeconds: TimeInterval,
        capturedUptime: TimeInterval
    ) {
        let previousStatus = qualificationLastTimeControlStatus
        qualificationLastTimeControlStatus = status
        guard qualificationRecorder != nil else {
            qualificationNativePausePending = false
            qualificationNativePausePlayerTime = nil
            return
        }

        if PlaybackQualificationNativeControlPolicy.shouldRecordPause(
            previousStatus: previousStatus,
            currentStatus: status,
            shouldPlay: playbackIntent.shouldPlay,
            isChangingEyeOrder: isChangingEyeOrder,
            hasCurrentItem: playerItem != nil,
            playerTime: playerTimeSeconds,
            duration: duration
        ) {
            qualificationRecorder?.recordPauseRequested(player: player)
            qualificationNativePausePending = true
            qualificationNativePausePlayerTime = playerTimeSeconds
            playbackIntent.pause()
            return
        }

        if status == .playing, qualificationNativePausePending {
            let shouldRecordNativeResume = !playbackIntent.shouldPlay
            qualificationNativePausePending = false
            qualificationNativePausePlayerTime = nil
            qualificationNativeResumeSeekSuppressionUntilUptime = capturedUptime + 0.5
            if shouldRecordNativeResume {
                qualificationRecorder?.recordPlayRequested(player: player)
                playbackIntent.requestPlayback()
            }
        }
    }

    private func handleNativeTimeJump(item: AVPlayerItem, generation: Int) {
        guard generation == preparationGeneration, item === playerItem else {
            return
        }
        let playerTime = player.currentTime().seconds
        let currentUptime = ProcessInfo.processInfo.systemUptime
        if PlaybackQualificationNativeControlPolicy.isNaturalCompletionTimeJump(
            playerTime: playerTime,
            duration: duration
        ) {
            return
        }
        qualificationProgrammaticSeeks.removeAll {
            $0.expiresAtUptime < currentUptime
        }
        if PlaybackQualificationNativeControlPolicy.shouldSuppressSeekAfterNativeResume(
            playerTime: playerTime,
            nativePausePlayerTime: qualificationNativePausePlayerTime,
            nativePausePending: qualificationNativePausePending,
            currentUptime: currentUptime,
            suppressionUntilUptime: qualificationNativeResumeSeekSuppressionUntilUptime
        ) {
            return
        }
        if PlaybackQualificationNativeControlPolicy.programmaticSeekSuppressionIndex(
            playerTime: playerTime,
            currentUptime: currentUptime,
            suppressions: qualificationProgrammaticSeeks
        ) != nil {
            return
        }
        guard PlaybackQualificationNativeControlPolicy.shouldRecordSeek(
            isSceneActive: playbackIntent.isSceneActive,
            isChangingEyeOrder: isChangingEyeOrder,
            hasEstablishedPlayback: hasEstablishedPlayback,
            playerTime: playerTime
        ), !PlaybackQualificationNativeControlPolicy.isDuplicateNativeSeek(
            playerTime: playerTime,
            currentUptime: currentUptime,
            previousPlayerTime: qualificationLastNativeSeekTime,
            previousUptime: qualificationLastNativeSeekUptime
        ) else {
            return
        }
        qualificationLastNativeSeekTime = playerTime
        qualificationLastNativeSeekUptime = currentUptime
        qualificationRecorder?.recordSeekStarted(player: player)
        qualificationRecorder?.recordSeekCompleted(player: player)
    }

    private func registerQualificationProgrammaticSeek(targetTime: TimeInterval) -> UUID {
        let currentUptime = ProcessInfo.processInfo.systemUptime
        qualificationProgrammaticSeeks.removeAll {
            $0.expiresAtUptime < currentUptime
        }
        let id = UUID()
        qualificationProgrammaticSeeks.append(
            PlaybackQualificationProgrammaticSeek(
                id: id,
                targetTime: targetTime,
                isInFlight: true,
                expiresAtUptime: currentUptime + 60
            )
        )
        return id
    }

    private func completeQualificationProgrammaticSeek(id: UUID) {
        guard let index = qualificationProgrammaticSeeks.firstIndex(where: { $0.id == id }) else {
            return
        }
        qualificationProgrammaticSeeks[index].isInFlight = false
        qualificationProgrammaticSeeks[index].expiresAtUptime = ProcessInfo.processInfo.systemUptime + 5
    }

    private func configureQualificationRecorder(for mediaItem: MediaItem) {
        let environment = ProcessInfo.processInfo.environment
        guard let runID = environment["BD_TO_AVP_QUALIFICATION_RUN_ID"],
              let evidenceMediaID = environment["BD_TO_AVP_QUALIFICATION_MEDIA_ID"],
              let expectedItemID = environment["BD_TO_AVP_QUALIFICATION_ITEM_ID"],
              expectedItemID == mediaItem.id
        else {
            qualificationRecorder = nil
            return
        }
        qualificationRecorder = PlaybackQualificationRecorder(
            runID: runID,
            mediaID: evidenceMediaID
        )
        qualificationLastTimeControlStatus = player.timeControlStatus
        qualificationNativePausePending = false
        qualificationNativePausePlayerTime = nil
        qualificationProgrammaticSeeks = []
        qualificationLastNativeSeekTime = nil
        qualificationLastNativeSeekUptime = nil
        qualificationNativeResumeSeekSuppressionUntilUptime = 0
    }
#endif

    private func startRelayWindowRefresh(generation: Int) {
        relayWindowRefreshTask?.cancel()
        relayWindowRefreshTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard !Task.isCancelled, let self, generation == self.preparationGeneration else {
                    return
                }
                await self.refreshRelayWindow(generation: generation)
            }
        }
    }

    func refreshRelayWindowNow() async {
        await refreshRelayWindow(generation: preparationGeneration)
    }

    private func refreshRelayWindow(generation: Int) async {
        guard var source = remotePlaybackSource, let relayTransport else {
            return
        }
        do {
            try await source.refreshRetainedWindow(transport: relayTransport)
            guard generation == preparationGeneration else {
                return
            }
            remotePlaybackSource = source
            consecutiveRelayRefreshFailures = 0
            duration = max(duration, source.retainedSeekPolicy.latestAvailableTime)
            if liveContext != nil {
                reportLivePositionNow()
                refreshLiveNoticeIfDue(generation: generation)
            }
        } catch RelayTransportError.sessionExpired, RelayTransportError.unpaired, RelayTransportError.sessionRevoked {
            guard generation == preparationGeneration else {
                return
            }
            terminateRelayPlayback(
                presentation: .relayPreparationFailed(Self.sessionEndedMessage),
                reason: .sessionEnded,
                emitEvent: true
            )
        } catch let error as RelayTransportError where Self.isStreamEnd(error) {
            // Termination is decided here, by the verified snapshot request. The loopback
            // bridge flattens AVPlayer's media failures to 503, so they never decide it.
            guard generation == preparationGeneration else { return }
            let message = await relayFailureMessage(for: error, generation: generation)
            guard generation == preparationGeneration else { return }
            terminateRelayPlayback(
                presentation: .relayPreparationFailed(message),
                reason: .streamEnded,
                emitEvent: true
            )
        } catch is CancellationError {
            return
        } catch let error as URLError where error.code == .cancelled {
            return
        } catch {
            guard generation == preparationGeneration else { return }
            if Self.isTerminalRelayRejection(error) {
                terminateRelayPlayback(
                    presentation: .relayPreparationFailed(Self.playbackFailureMessage(for: error)),
                    reason: .rejected,
                    emitEvent: true
                )
                return
            }
            consecutiveRelayRefreshFailures += 1
            if consecutiveRelayRefreshFailures >= 3 {
                terminateRelayPlayback(
                    presentation: .relayPreparationFailed("The relay server could not be reached. Check the Mac and try again."),
                    reason: .serverUnreachable,
                    emitEvent: true
                )
            }
        }
    }

    func handleRelayCoordinatorState(_ coordinatorState: RelayCoordinatorState) {
        // A live ISO title belongs to the movie session, not the developer relay coordinator.
        guard isRelayPlayback, liveContext == nil else { return }
        switch coordinatorState {
        case .idle:
            finish()
        case .sessionExpired:
            terminateRelayPlayback(
                presentation: .relayPreparationFailed("The relay session ended. Pair with the Mac again to continue."),
                reason: .sessionEnded,
                emitEvent: false
            )
        case let .failed(message):
            terminateRelayPlayback(
                presentation: .relayPreparationFailed(message),
                reason: .serverUnreachable,
                emitEvent: false
            )
        case .networkUnavailable:
            terminateRelayPlayback(
                presentation: .relayPreparationFailed("The network became unavailable. Reconnect and try again."),
                reason: .serverUnreachable,
                emitEvent: false
            )
        case .discovery, .confirming, .connected, .reconnecting:
            break
        }
    }

    private func terminateRelayPlayback(
        presentation: PlaybackFailurePresentation,
        reason: RelayPlaybackTerminationReason,
        emitEvent: Bool
    ) {
        guard isRelayPlayback, state != .failed else { return }
        let relayItem = mediaItem
        let sessionID = remotePlaybackSource?.session.sessionID.rawValue
            ?? relayItem?.id.dropFirst("relay:".count).description
        preparationGeneration += 1
        finishCurrentSession(persistResume: false)
        mediaItem = relayItem
        presentFailure(presentation)
        if emitEvent, let sessionID {
            relayTerminationEvent = RelayPlaybackTerminationEvent(
                id: UUID(), sessionID: sessionID, reason: reason
            )
        }
    }

    private static func relayTerminationReason(for error: Error) -> RelayPlaybackTerminationReason {
        if let relayError = error as? RelayTransportError {
            switch relayError {
            case .sessionExpired, .unpaired, .sessionRevoked: return .sessionEnded
            case .streamEnded: return .streamEnded
            case let .unexpectedStatusCode(status) where (400 ..< 500).contains(status): return .rejected
            default: break
            }
        }
        if isTerminalRelayRejection(error) { return .rejected }
        return .serverUnreachable
    }

    private static func isStreamEnd(_ error: RelayTransportError) -> Bool {
        if case .streamEnded = error { return true }
        return false
    }

    /// Sends the playhead of a live stream, signed with the stream session. A lost report
    /// only delays pacing; the snapshot refresh decides whether the stream ended.
    private func reportLivePositionNow() {
        guard liveContext != nil, let source = remotePlaybackSource, let relayTransport else { return }
        let seconds = player.currentTime().seconds
        let milliseconds = Int64(max(0, seconds.isFinite ? seconds : currentTime) * 1_000)
        Task { try? await source.reportPosition(milliseconds: milliseconds, transport: relayTransport) }
    }

    /// Every fifth refresh, reads the Mac's live status for the busy notice.
    private func refreshLiveNoticeIfDue(generation: Int) {
        guard let liveContext else { return }
        liveStatusRefreshCount += 1
        guard liveStatusRefreshCount % 5 == 1 else { return }
        Task { @MainActor [weak self] in
            let status = await liveContext.status()
            guard let self, generation == self.preparationGeneration, let status else { return }
            self.liveNotice = status.macBusyConverting ? Self.liveBusyNotice : nil
        }
    }

    private static func isTerminalRelayRejection(_ error: Error) -> Bool {
        if error is RelaySessionError || error is DecodingError { return true }
        guard case let RelayTransportError.unexpectedStatusCode(status) = error else { return false }
        return (400 ..< 500).contains(status)
    }

    private static func sourceFailurePresentation(for error: Error) -> PlaybackFailurePresentation {
        guard let bookmarkError = error as? BookmarkStoreError else {
            return .sourceUnavailable
        }

        switch bookmarkError {
        case .missingBookmark, .invalidBookmark:
            return .sourceNeedsLocation
        case .staleBookmark, .missingResource:
            return .sourceUnavailable
        case .invalidIdentifier:
            return .sourceNeedsLocation
        }
    }
}

private enum PlaybackSeekOrigin {
    case user
    case resumeRestoration
    case eyeOrderRestoration

#if BD_TO_AVP_QUALIFICATION
    var qualificationDetail: String {
        switch self {
        case .user:
            return "seek"
        case .resumeRestoration:
            return "resume_restore"
        case .eyeOrderRestoration:
            return "eye_order_restore"
        }
    }
#endif
}

private struct PreparedMediaSelections {
    let audioGroup: AVMediaSelectionGroup?
    let subtitleGroup: AVMediaSelectionGroup?
    let audioOptions: [PlaybackMediaOption]
    let subtitleOptions: [PlaybackMediaOption]
    let selectedAudioID: String
    let selectedSubtitleID: String
    let audioSelectionByID: [String: AVMediaSelectionOption]
    let subtitleSelectionByID: [String: AVMediaSelectionOption]
}

private struct PackedStereoSource {
    let makeAsset: () -> AVURLAsset
    let format: StereoFormat
    let duration: CMTime
    let spatialMetadataFallback: PackedStereoSpatialMetadata?
}

private struct PlaybackItemRestorationState {
    let time: CMTime
    let wasPlaying: Bool
    let mediaSelection: MediaSelectionRestorationState
}

private struct MediaSelectionRestorationState {
    let audioPropertyList: Any?
    let audioID: String?
    let subtitlePropertyList: Any?
}

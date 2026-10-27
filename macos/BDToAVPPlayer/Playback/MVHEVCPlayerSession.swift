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
    /// The player's buffered range around the playhead.
    @Published private(set) var bufferedRange: ClosedRange<TimeInterval>?
    @Published private(set) var audioOptions: [PlaybackMediaOption] = []
    @Published private(set) var subtitleOptions: [PlaybackMediaOption] = []
    @Published private(set) var selectedAudioID = ""
    @Published private(set) var selectedSubtitleID = "off"
    @Published private(set) var isEyeSwapped = false
    @Published private(set) var isChangingEyeOrder = false
    /// Playback is waiting for more of the movie to load: a native Blu-ray 3D title's buffer ran
    /// dry, or `AVPlayer` stopped its clock to wait for data.
    @Published private(set) var isBuffering = false
    /// The share the movie plays from has stopped answering, or the headset has no network.
    @Published private(set) var isConnectionLost = false
    /// Replaces the subtitle list's note, for a title whose subtitles cannot show yet.
    @Published private(set) var subtitleListNote: String?
    /// Why the chosen Blu-ray 3D audio is not playing while the video plays on.
    @Published private(set) var audioWarning: String?

    /// How far ahead a movie on a share is read, so a short drop in the connection never shows.
    static let shareForwardBufferDuration: TimeInterval = 20

    private(set) var playerItem: AVPlayerItem?

    private var resourceLease: SecurityScopedResourceLease?
    /// The reads of a movie outside the app, for saying when its share stops answering.
    private var shareConnection: ShareConnectionMonitor?
    private var shareConnectionTask: Task<Void, Never>?
    /// Opens a Blu-ray 3D source; tests replace it to stand in for a share.
    var nativeSourceOpener: @Sendable (URL, ShareConnectionMonitor?) throws -> Bluray3DSource = { url, reads in
        try Bluray3DSourceOpener.open(url, reads: reads)
    }
    /// Blu-ray 3D playback decoded on the headset, used instead of `player`.
    private var nativePlayback: NativeBlurayPlayback?
    private var nativeStatusTask: Task<Void, Never>?
    private var resumeStore: ResumeStore?
    /// The key the resume position is saved under, or nil when this playback keeps none.
    private var resumeID: String?
    private var lastResumeSaveUptime: TimeInterval?
    private var itemStatusObservation: NSKeyValueObservation?
    private var timeControlStatusObservation: NSKeyValueObservation?
    private var reasonForWaitingObservation: NSKeyValueObservation?
    private var timeObserver: Any?
    private var playbackFinishedObserver: NSObjectProtocol?
    private var audioGroup: AVMediaSelectionGroup?
    private var subtitleGroup: AVMediaSelectionGroup?
    private var audioSelectionByID: [String: AVMediaSelectionOption] = [:]
    private var subtitleSelectionByID: [String: AVMediaSelectionOption] = [:]
    private var packedStereoSource: PackedStereoSource?
    /// Makes a fresh asset for the open movie, so its player item can be replaced.
    private var makePlayerAsset: (() -> AVURLAsset)?
    /// The system interrupted playback while the app was in the background; it is rebuilt when
    /// the app returns (#1165).
    private var awaitsInterruptionRecovery = false
    private var recoveryBudget = PlaybackRecoveryBudget()
    private var recoveryTask: Task<Void, Never>?
    /// Identifies `recoveryTask`, so a finished task clears only itself.
    private var recoveryTaskID = 0
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
        reasonForWaitingObservation = player.observe(\.reasonForWaitingToPlay, options: [.new]) { [weak self] observedPlayer, _ in
            let timeControl = PlayerTimeControl(observedPlayer)
            Task { @MainActor in
                self?.apply(timeControl)
            }
        }
        timeControlStatusObservation = player.observe(\.timeControlStatus, options: [.initial, .new]) { [weak self] observedPlayer, change in
            let status = change.newValue ?? observedPlayer.timeControlStatus
            let timeControl = PlayerTimeControl(observedPlayer, status: status)
#if BD_TO_AVP_QUALIFICATION
            let capturedAt = Date()
            let capturedUptime = ProcessInfo.processInfo.systemUptime
            let playerTimeSeconds = observedPlayer.currentTime().seconds
#endif
            Task { @MainActor in
                self?.apply(timeControl)
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
        if nativePlayback != nil {
            return bufferedRange.map { $0.upperBound > $0.lowerBound } ?? false
        }
        return duration.isFinite && duration > 0
    }

    /// The scrub bar's full span and the range that can be played right now.
    var timeline: PlaybackTimeline {
        PlaybackTimeline(
            playerDuration: duration,
            loadedRange: bufferedRange ?? currentTime ... currentTime
        )
    }

    var supportsEyeSwap: Bool {
        packedStereoSource != nil
    }

    func prepare(
        mediaItem: MediaItem,
        bookmarkStore: BookmarkStore,
        resumeStore: ResumeStore
    ) async {
        preparationGeneration += 1
        let generation = preparationGeneration
        finishCurrentSession(persistResume: true)

        self.mediaItem = mediaItem
        if mediaItem.format != .mvHEVC {
            playerEntity.components.remove(VideoPlayerComponent.self)
        }
        self.resumeStore = resumeStore
        resumeID = nil
        state = .loading
        failureMessage = nil
        failurePresentation = nil
        preparationPhase = .openingSource
        currentTime = 0
        duration = 0
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

        guard mediaItem.format != .unsupported else {
            presentFailure(.unsupported)
            return
        }
        if mediaItem.format == .bluray3D {
            await prepareNativeBluray(mediaItem, bookmarkStore: bookmarkStore, resumeStore: resumeStore, generation: generation)
            return
        }

        let openedLease: SecurityScopedResourceLease
        let asset: AVURLAsset
        let makeAsset: () -> AVURLAsset
        var openedConnection: ShareConnectionMonitor?
        do {
            let bookmarkLease = try await bookmarkStore.open(id: mediaItem.id)
            let url = bookmarkLease.url
            // Held before anything touches the share, so leaving the player can stop it.
            let connection = ShareConnectionMonitor.forMovie(at: url)
            watchShareConnection(connection, generation: generation)
            let shareLoader: ShareFileResourceLoader?
            do {
                shareLoader = try await Task.detached(priority: .userInitiated) {
                    try connection.map { connection in
                        connection.watchNetworkIfRemote(url)
                        return try ShareFileResourceLoader(url: url, connection: connection)
                    }
                }.value
            } catch {
                bookmarkLease.close()
                throw error
            }
            if let shareLoader {
                makeAsset = { shareLoader.makeAsset() }
                openedLease = SecurityScopedResourceLease(url: url, startAccessing: { true }, stopAccessing: {
                    shareLoader.cancel()
                    bookmarkLease.close()
                })
                openedConnection = shareLoader.connection
            } else {
                makeAsset = { AVURLAsset(url: url) }
                openedLease = bookmarkLease
            }
            asset = makeAsset()
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
            resumeID = mediaItem.id
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
            if openedConnection != nil {
                item.preferredForwardBufferDuration = Self.shareForwardBufferDuration
            }
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
            makePlayerAsset = makeAsset
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
            presentFailure(.preparationFailed(
                PlaybackErrorText.message(for: error, fallback: "The movie could not be prepared. Try again.")
            ))
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
        let targetTime = PlaybackSeekPolicy.clampedTime(requestedTime, duration: duration)
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
        if let nativePlayback {
            if id == "off" {
                nativePlayback.selectSubtitle(trackID: nil)
            } else {
                guard let track = nativePlayback.subtitleTracks.first(where: { $0.mediaOption.id == id }) else { return }
                nativePlayback.selectSubtitle(trackID: track.id)
            }
            selectedSubtitleID = id
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
        guard packedStereoSource != nil,
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
            mediaSelection: currentMediaSelectionRestoration(),
            reason: .eyeOrder
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
                let (replacementItem, preparedSelections) = try await makeReplacementItem(eyeOrder: targetEyeOrder)

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
                PlaybackErrorText.log(error, context: "eye order")
                failureMessage = "Eye order could not be changed."
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
        if awaitsInterruptionRecovery {
            recoverFromInterruption()
        }
#if BD_TO_AVP_QUALIFICATION
        qualificationRecorder?.recordSceneActive(player: player)
#endif
    }

    func finish() {
        preparationGeneration += 1
        finishCurrentSession(persistResume: true)
        mediaItem = nil
        resumeStore = nil
        resumeID = nil
        state = .idle
        failureMessage = nil
        failurePresentation = nil
        preparationPhase = .openingSource
    }

    // MARK: Interruptions

    /// Rebuilds playback at the same position after the system interrupted it, as it does when
    /// the app leaves the foreground (#1165). It waits for the app to be in the foreground, and
    /// after a few attempts in a short time says playback stopped and offers Try Again.
    private func recoverFromInterruption() {
        guard recoveryTask == nil else {
            return
        }
        guard playbackIntent.isSceneActive else {
            awaitsInterruptionRecovery = true
            return
        }
        awaitsInterruptionRecovery = false
        switch recoveryBudget.decision(now: ProcessInfo.processInfo.systemUptime) {
        case .recover:
            break
        case let .wait(seconds):
            let generation = preparationGeneration
            startRecoveryTask { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
                guard let self, !Task.isCancelled, generation == self.preparationGeneration else {
                    return
                }
                self.recoveryTask = nil
                self.recoverFromInterruption()
            }
            return
        case .giveUp:
            persistResume()
            presentFailure(.interrupted)
            return
        }
        if let nativePlayback {
            nativePlayback.recoverFromInterruption()
            return
        }
        rebuildPlayerItem()
    }

    /// Replaces a player item the system interrupted with a fresh one at the same position, audio
    /// and subtitles. Playback continues only if it was playing and the app is in the foreground.
    private func rebuildPlayerItem() {
        guard let failedItem = playerItem, makePlayerAsset != nil else {
            return
        }
        let restoration = PlaybackItemRestorationState(
            time: CMTime(seconds: currentTime, preferredTimescale: 600),
            wasPlaying: playbackIntent.wantsPlayback,
            mediaSelection: currentMediaSelectionRestoration(),
            reason: .interruption
        )
        let eyeOrder: PackedStereoEyeOrder = isEyeSwapped ? .reversed : .normal
        let generation = preparationGeneration
        startRecoveryTask { [weak self, weak failedItem] in
            guard let self else {
                return
            }
            do {
                let (replacementItem, preparedSelections) = try await makeReplacementItem(eyeOrder: eyeOrder)
                // A newer movie, or leaving the player, already let go of this task.
                guard generation == preparationGeneration,
                      failedItem === self.playerItem,
                      !Task.isCancelled
                else {
                    return
                }
                preparationGeneration += 1
                let replacementGeneration = preparationGeneration
                self.playerItem = replacementItem
                configureMediaSelections(preparedSelections)
                // Before the first item was ready, the saved resume position still applies.
                pendingItemRestoration = hasEstablishedPlayback ? restoration : nil
                observe(replacementItem, generation: replacementGeneration)
                player.replaceCurrentItem(with: replacementItem)
            } catch {
                guard generation == preparationGeneration, !Task.isCancelled else {
                    return
                }
                persistResume()
                presentFailure(.preparationFailed(PlaybackErrorText.message(for: error)))
            }
        }
    }

    /// Runs `body` as `recoveryTask`, which is cleared when it ends unless replaced first.
    private func startRecoveryTask(_ body: @escaping @MainActor () async -> Void) {
        recoveryTaskID += 1
        let id = recoveryTaskID
        recoveryTask = Task { [weak self] in
            await body()
            if let self, self.recoveryTaskID == id {
                self.recoveryTask = nil
            }
        }
    }

    /// Handles the current player item failing with `error`: a system interruption rebuilds the
    /// item at the same position, and anything else is reported in plain words. Tests call it to
    /// fail an item the way the system does.
    func handlePlayerItemFailure(_ error: Error?) {
        guard playerItem != nil else {
            return
        }
        if PlaybackInterruptionPolicy.isInterruption(error), makePlayerAsset != nil {
            isBuffering = true
            recoverFromInterruption()
            return
        }
        let message = PlaybackErrorText.message(for: error)
        if BuiltInStereoChecks.contains(mediaItem) {
            presentFailure(.builtInStereoCheckUnavailable(message))
        } else {
            presentFailure(.preparationFailed(message))
        }
    }

    /// A new player item for the open movie, with its packed-stereo composition in `eyeOrder`.
    private func makeReplacementItem(
        eyeOrder: PackedStereoEyeOrder
    ) async throws -> (AVPlayerItem, PreparedMediaSelections) {
        guard let makePlayerAsset else {
            throw CancellationError()
        }
        let asset = makePlayerAsset()
        let item = AVPlayerItem(asset: asset)
        if shareConnection != nil {
            item.preferredForwardBufferDuration = Self.shareForwardBufferDuration
        }
        guard let packedStereoSource else {
            return (item, try await prepareMediaSelections(for: asset, item: item))
        }
        async let composition = PackedStereoComposition.make(
            asset: asset,
            format: packedStereoSource.format,
            duration: packedStereoSource.duration,
            eyeOrder: eyeOrder,
            spatialMetadataFallback: packedStereoSource.spatialMetadataFallback
        )
        async let selections = prepareMediaSelections(for: asset, item: item)
        item.videoComposition = try await composition
        item.seekingWaitsForVideoCompositionRendering = true
        return (item, try await selections)
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
        // Held before the source opens, since opening an MKV already reads the share, so leaving
        // the player stops those reads too.
        let reads = ShareConnectionMonitor.forMovie(at: url)
        watchShareConnection(reads, generation: generation)
        let openSource = nativeSourceOpener
        let source: Bluray3DSource
        do {
            source = try await Task.detached(priority: .userInitiated) {
                reads?.watchNetworkIfRemote(url)
                return try openSource(url, reads)
            }.value
        } catch {
            guard generation == preparationGeneration, !Task.isCancelled else { return }
            presentFailure(.preparationFailed(
                PlaybackErrorText.message(for: error, fallback: Bluray3DSourceError.openFailed.localizedDescription)
            ))
            return
        }
        guard generation == preparationGeneration, !Task.isCancelled else {
            reads?.cancel()
            return
        }
        let title = source.info.titles.first { $0.id == mediaItem.bluray3DTitle?.id } ?? source.info.mainTitle
        guard let title, title.isPlayable else {
            presentFailure(.preparationFailed(
                title?.unsupportedReason?.message ?? Bluray3DSourceError.titleUnavailable.localizedDescription
            ))
            return
        }

        let startSeconds = ResumeWritePolicy.blurayStartSeconds(
            savedPosition: resumeStore.resumeTime(for: mediaItem.id), duration: title.durationSeconds
        )
        let playback = NativeBlurayPlayback(source: source, title: title, startSeconds: startSeconds, reads: reads)
        nativePlayback = playback
        resumeID = mediaItem.id
        currentTime = startSeconds
        duration = title.durationSeconds
        audioOptions = playback.audioTracks.map(\.mediaOption)
        selectedAudioID = playback.audioTracks.first { $0.id == playback.selectedAudioTrackID }?.mediaOption.id ?? ""
        subtitleOptions = [PlaybackMediaOption(id: "off", displayName: "Off")] + playback.subtitleTracks.map(\.mediaOption)
        selectedSubtitleID = "off"
        subtitleListNote = playback.subtitleTracks.isEmpty && !title.subtitleTracks.isEmpty
            ? PlaybackTrackListNote.unsupportedBluraySubtitles : nil
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
        if status.isInterrupted, status.failure == nil {
            if !isBuffering {
                isBuffering = true
            }
            recoverFromInterruption()
            return
        }
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
        if audioWarning != status.audioFailure {
            audioWarning = status.audioFailure
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
        nativePlayback.seek(to: target)
        currentTime = target
    }

    // MARK: Share connection

    /// Checks the share's reads four times a second, separately from the playback clock, which
    /// stops while the player waits for them.
    private func watchShareConnection(_ connection: ShareConnectionMonitor?, generation: Int) {
        shareConnectionTask?.cancel()
        shareConnection = connection
        guard connection != nil else {
            shareConnectionTask = nil
            return
        }
        shareConnectionTask = Task { [weak self] in
            while !Task.isCancelled {
                self?.refreshShareConnection(generation: generation)
                try? await Task.sleep(nanoseconds: 250_000_000)
            }
        }
    }

    private func refreshShareConnection(generation: Int) {
        guard generation == preparationGeneration, let shareConnection else { return }
        let isLost = shareConnection.isConnectionLost
        if isConnectionLost != isLost {
            isConnectionLost = isLost
        }
    }

    /// `AVPlayer`'s play state. Waiting for data still counts as playing, so the play button
    /// keeps offering Pause while the player buffers.
    private func apply(_ timeControl: PlayerTimeControl) {
        guard nativePlayback == nil else { return }
        isPlaying = timeControl.wantsPlayback
        if isBuffering != timeControl.isWaitingForData {
            isBuffering = timeControl.isWaitingForData
        }
    }

    /// The subtitle track the native Blu-ray 3D engine draws, nil while subtitles are off.
    var nativeSubtitleTrackID: UInt16? { nativePlayback?.selectedSubtitleTrackID }

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
                let loadedRanges = item.loadedTimeRanges.compactMap { value -> ClosedRange<TimeInterval>? in
                    let range = value.timeRangeValue
                    let start = range.start.seconds
                    let end = range.end.seconds
                    guard start.isFinite, end.isFinite, end >= start else { return nil }
                    return start ... end
                }
                self.bufferedRange = PlaybackTimeline.bufferedRange(containing: self.currentTime, in: loadedRanges)
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
            apply(PlayerTimeControl(player))
            if let restoration = pendingItemRestoration, restoration.reason == .interruption {
                pendingItemRestoration = nil
                restoreMediaSelections(restoration.mediaSelection, on: item)
                refreshSelectedMediaOptionIDs()
                seek(to: restoration.time.seconds, origin: .resumeRestoration) { [weak self] _ in
                    guard let self, self.playbackIntent.shouldPlay else {
                        return
                    }
                    self.player.play()
                }
                return
            }
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
            handlePlayerItemFailure(item.error)
        @unknown default:
            presentFailure(
                .preparationFailed("The player returned an unknown playback status.")
            )
        }
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
        shareConnectionTask?.cancel()
        shareConnectionTask = nil
        shareConnection?.cancel()
        shareConnection = nil
        isConnectionLost = false
        subtitleListNote = nil
        audioWarning = nil
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
        bufferedRange = nil
        audioGroup = nil
        subtitleGroup = nil
        audioSelectionByID = [:]
        subtitleSelectionByID = [:]
        packedStereoSource = nil
        makePlayerAsset = nil
        recoveryTask?.cancel()
        recoveryTask = nil
        awaitsInterruptionRecovery = false
        recoveryBudget.reset()
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
            switch ResumeWritePolicy.decision(currentTime: resumeTime, duration: duration) {
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
            PlaybackErrorText.log(error, context: "resume position")
            failureMessage = "Playback position could not be saved."
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
        isBuffering = false
        pendingItemRestoration = nil
        awaitsInterruptionRecovery = false
        recoveryTask?.cancel()
        recoveryTask = nil
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
    let format: StereoFormat
    let duration: CMTime
    let spatialMetadataFallback: PackedStereoSpatialMetadata?
}

private struct PlaybackItemRestorationState {
    enum Reason {
        case eyeOrder
        /// The previous item failed because the system interrupted playback (#1165).
        case interruption
    }

    let time: CMTime
    let wasPlaying: Bool
    let mediaSelection: MediaSelectionRestorationState
    let reason: Reason
}

private struct MediaSelectionRestorationState {
    let audioPropertyList: Any?
    let audioID: String?
    let subtitlePropertyList: Any?
}

/// What `AVPlayer`'s time control says about playback, read on the observing thread.
struct PlayerTimeControl: Equatable, Sendable {
    /// Playing, or about to play once enough data has loaded.
    let wantsPlayback: Bool
    /// The clock is stopped because data has not arrived in time, as from a slow or lost share.
    let isWaitingForData: Bool

    init(status: AVPlayer.TimeControlStatus, reasonForWaiting: AVPlayer.WaitingReason?) {
        let isWaiting = status == .waitingToPlayAtSpecifiedRate
        isWaitingForData = isWaiting && reasonForWaiting == .toMinimizeStalls
        wantsPlayback = status == .playing
            || isWaitingForData
            || isWaiting && reasonForWaiting == .evaluatingBufferingRate
    }

    init(_ player: AVPlayer, status: AVPlayer.TimeControlStatus? = nil) {
        self.init(status: status ?? player.timeControlStatus, reasonForWaiting: player.reasonForWaitingToPlay)
    }
}

import AVFoundation
import Combine
import Foundation
import RealityKit

struct PlaybackMediaOption: Identifiable, Equatable, Hashable {
    let id: String
    let displayName: String
}

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
    @Published private(set) var audioOptions: [PlaybackMediaOption] = []
    @Published private(set) var subtitleOptions: [PlaybackMediaOption] = []
    @Published private(set) var selectedAudioID = ""
    @Published private(set) var selectedSubtitleID = "off"
    @Published private(set) var isEyeSwapped = false
    @Published private(set) var isChangingEyeOrder = false

    private(set) var playerItem: AVPlayerItem?

    private var resourceLease: SecurityScopedResourceLease?
    private var resumeStore: ResumeStore?
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
            && playerItem?.status == .readyToPlay
    }

    var canSeek: Bool {
        canControlPlayback && duration.isFinite && duration > 0
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
        state = .loading
        failureMessage = nil
        failurePresentation = nil
        preparationPhase = .openingSource
        currentTime = 0
        duration = 0
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

        let openedLease: SecurityScopedResourceLease
        do {
            openedLease = try await bookmarkStore.open(id: mediaItem.id)
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
            let detectedFormat = try await MediaFormatInspector.inspect(url: openedLease.url)
            guard generation == preparationGeneration, !Task.isCancelled else {
                if resourceLease === openedLease {
                    resourceLease = nil
                }
                openedLease.close()
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

            let asset = AVURLAsset(url: openedLease.url)
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
                    url: openedLease.url,
                    format: mediaItem.format,
                    duration: preparedDuration,
                    spatialMetadataFallback: spatialMetadataFallback
                )
            case .mvHEVC, .unsupported:
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

    func installPlayerComponent() {
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
        guard let audioGroup,
              let selection = audioSelectionByID[id],
              let playerItem
        else {
            return
        }
        playerItem.select(selection, in: audioGroup)
        selectedAudioID = id
    }

    func selectSubtitle(id: String) {
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
                let replacementAsset = AVURLAsset(url: packedStereoSource.url)
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

    func applicationBecameInactive() {
        playbackIntent.sceneBecameInactive()
#if BD_TO_AVP_QUALIFICATION
        qualificationRecorder?.recordSceneInactive(player: player)
#endif
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
        mediaItem = nil
        resumeStore = nil
        state = .idle
        failureMessage = nil
        failurePresentation = nil
        preparationPhase = .openingSource
    }

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
            if BuiltInStereoChecks.contains(mediaItem) {
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
        let audioOptions: [PlaybackMediaOption]
        if let audioGroup {
            audioOptions = audioGroup.options.enumerated().map { index, option in
                let id = "audio-\(index)"
                audioSelections[id] = option
                return PlaybackMediaOption(id: id, displayName: option.displayName)
            }
        } else {
            audioOptions = []
        }
        let selectedAudioID = audioOptions.first(where: { option in
            guard let audioGroup, let selection = audioSelections[option.id] else {
                return false
            }
            return item.currentMediaSelection.selectedMediaOption(in: audioGroup) === selection
        })?.id ?? audioOptions.first?.id ?? ""

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
            audioOptions: audioOptions,
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
            selectedAudioID = audioOptions.first(where: { option in
                guard let selection = audioSelectionByID[option.id] else {
                    return false
                }
                return playerItem?.currentMediaSelection.selectedMediaOption(in: audioGroup) === selection
            })?.id ?? audioOptions.first?.id ?? ""
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
            subtitlePropertyList: subtitlePropertyList
        )
    }

    private func restoreMediaSelections(_ restoration: MediaSelectionRestorationState, on item: AVPlayerItem) {
        if let audioGroup,
           let propertyList = restoration.audioPropertyList,
           let selection = audioGroup.mediaSelectionOption(withPropertyList: propertyList)
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
#endif
        player.pause()
        player.replaceCurrentItem(with: nil)
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
        failurePresentation = nil
        preparationPhase = .openingSource
    }

    private func persistResume(isFinishing: Bool = false) {
        guard ResumeWritePolicy.allowsWrite(
            isChangingEyeOrder: isChangingEyeOrder,
            isFinishing: isFinishing,
            hasEstablishedPlayback: hasEstablishedPlayback
        ), let mediaItem, let resumeStore else {
            return
        }

        let resumeTime = ResumeWritePolicy.position(
            currentTime: currentTime,
            eyeOrderChangeTime: eyeOrderChangeResumeTime,
            isChangingEyeOrder: isChangingEyeOrder,
            isFinishing: isFinishing
        )

        do {
            switch ResumeWritePolicy.decision(currentTime: resumeTime, duration: duration) {
            case let .write(position):
                try resumeStore.setResumeTime(position, for: mediaItem.id)
            case .remove:
                try resumeStore.remove(id: mediaItem.id)
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
    let url: URL
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
    let subtitlePropertyList: Any?
}

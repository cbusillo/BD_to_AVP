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
    @Published private(set) var isPlaying = false
    @Published private(set) var currentTime: TimeInterval = 0
    @Published private(set) var duration: TimeInterval = 0
    @Published private(set) var isRenderingReady = false
    @Published private(set) var audioOptions: [PlaybackMediaOption] = []
    @Published private(set) var subtitleOptions: [PlaybackMediaOption] = []
    @Published private(set) var selectedAudioID = ""
    @Published private(set) var selectedSubtitleID = "off"

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
    private var preparationGeneration = 0
    private var playerComponentInstalled = false

    init() {
        timeControlStatusObservation = player.observe(\.timeControlStatus, options: [.initial, .new]) { [weak self] observedPlayer, _ in
            Task { @MainActor in
                self?.isPlaying = observedPlayer.timeControlStatus == .playing
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
        isReady && playerItem?.status == .readyToPlay
    }

    var canSeek: Bool {
        canControlPlayback && duration.isFinite && duration > 0
    }

    var timeSummary: String {
        "\(PlaybackTimeFormatter.string(for: currentTime)) / \(PlaybackTimeFormatter.string(for: duration))"
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
        self.resumeStore = resumeStore
        state = .loading
        failureMessage = nil
        currentTime = 0
        duration = 0
        isRenderingReady = false
        audioOptions = []
        subtitleOptions = []
        selectedAudioID = ""
        selectedSubtitleID = "off"

        guard mediaItem.format == .mvHEVC else {
            presentFailure(
                "\(mediaItem.format.displayName) playback is not supported here. Choose an MV-HEVC spatial video."
            )
            return
        }

        let openedLease: SecurityScopedResourceLease
        do {
            openedLease = try bookmarkStore.open(id: mediaItem.id)
        } catch {
            presentFailure("The movie could not be opened: \(error.localizedDescription)")
            return
        }

        do {
            let detectedFormat = try await MediaFormatInspector.inspect(url: openedLease.url)
            guard generation == preparationGeneration, !Task.isCancelled else {
                openedLease.close()
                return
            }
            guard detectedFormat == .mvHEVC else {
                openedLease.close()
                presentFailure(
                    "This movie is \(detectedFormat.displayName), not MV-HEVC. It cannot be played by the spatial player."
                )
                return
            }

            let asset = AVURLAsset(url: openedLease.url)
            let item = AVPlayerItem(asset: asset)
            async let loadedDuration = asset.load(.duration)
            async let mediaSelections = prepareMediaSelections(for: asset, item: item)
            let preparedDuration = try await loadedDuration
            let preparedSelections = try await mediaSelections

            guard generation == preparationGeneration, !Task.isCancelled else {
                openedLease.close()
                return
            }

            resourceLease = openedLease
            playerItem = item
            duration = preparedDuration.seconds.isFinite ? max(0, preparedDuration.seconds) : 0
            configureMediaSelections(preparedSelections)
            observe(item, generation: generation)
            player.replaceCurrentItem(with: item)

            if let resumeTime = resumeStore.resumeTime(for: mediaItem.id) {
                seek(to: resumeTime)
            }
        } catch is CancellationError {
            openedLease.close()
        } catch {
            openedLease.close()
            guard generation == preparationGeneration else {
                return
            }
            presentFailure("The movie could not be prepared: \(error.localizedDescription)")
        }
    }

    func installPlayerComponent() {
        var component = playerEntity.components[VideoPlayerComponent.self] ?? VideoPlayerComponent(avPlayer: player)
        component.desiredViewingMode = .stereo
        component.desiredSpatialVideoMode = .screen
        component.desiredImmersiveViewingMode = .portal
        playerEntity.components.set(component)
        playerComponentInstalled = true
        refreshRenderingReadiness()
    }

    func refreshRenderingReadiness() {
        guard playerComponentInstalled,
              let component = playerEntity.components[VideoPlayerComponent.self]
        else {
            isRenderingReady = false
            return
        }
        let renderingReady = component.currentRenderingStatus == .ready
        if isRenderingReady != renderingReady {
            isRenderingReady = renderingReady
        }
    }

    func play() {
        guard canControlPlayback else {
            return
        }
        player.play()
    }

    func pause() {
        guard playerItem != nil else {
            return
        }
        player.pause()
        persistResume()
    }

    func togglePlayback() {
        isPlaying ? pause() : play()
    }

    func seek(to requestedTime: TimeInterval) {
        guard let playerItem else {
            return
        }

        let generation = preparationGeneration
        let targetTime = PlaybackSeekPolicy.clampedTime(requestedTime, duration: duration)
        player.seek(
            to: CMTime(seconds: targetTime, preferredTimescale: 600),
            toleranceBefore: .zero,
            toleranceAfter: .zero
        ) { [weak self, weak playerItem] completed in
            Task { @MainActor in
                guard let self,
                      completed,
                      generation == self.preparationGeneration,
                      playerItem === self.playerItem
                else {
                    return
                }
                self.currentTime = targetTime
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

    func applicationDidEnterBackground() {
        pause()
    }

    func finish() {
        preparationGeneration += 1
        finishCurrentSession(persistResume: true)
        mediaItem = nil
        resumeStore = nil
        state = .idle
        failureMessage = nil
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
            player.play()
        case .failed:
            presentFailure(item.error?.localizedDescription ?? "The player failed without an error description.")
        @unknown default:
            presentFailure("The player returned an unknown playback status.")
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

    private func finishCurrentSession(persistResume shouldPersistResume: Bool) {
        if shouldPersistResume {
            persistResume()
        }
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
        audioOptions = []
        subtitleOptions = []
        selectedAudioID = ""
        selectedSubtitleID = "off"
        isPlaying = false
        isRenderingReady = false
    }

    private func persistResume() {
        guard let mediaItem, let resumeStore else {
            return
        }

        do {
            switch ResumeWritePolicy.decision(currentTime: currentTime, duration: duration) {
            case let .write(position):
                try resumeStore.setResumeTime(position, for: mediaItem.id)
            case .remove:
                try resumeStore.remove(id: mediaItem.id)
            case .skip:
                break
            }
        } catch {
            failureMessage = "Playback position could not be saved: \(error.localizedDescription)"
        }
    }

    private func presentFailure(_ message: String) {
        player.pause()
        state = .failed
        failureMessage = message
        isPlaying = false
        isRenderingReady = false
    }
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

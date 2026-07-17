import AVFoundation
import Combine
import Foundation
import RealityKit
import VideoToolbox

enum ProbeFailureCategory: String, Codable {
    case unsupportedDecode = "unsupported_decode"
    case malformedMetadata = "malformed_metadata"
    case missingFile = "missing_file"
    case transferFailure = "transfer_failure"
    case playbackFailure = "playback_failure"
}

struct ProbeFailure: Identifiable, Equatable {
    let id = UUID()
    let category: ProbeFailureCategory
    let title: String
    let message: String
}

struct ProbeMediaOption: Identifiable, Equatable {
    let id: String
    let name: String
}

enum ProbeSeekPosition: String, CaseIterable {
    case beginning
    case middle
    case end

    var checkID: PlaybackCheckID {
        switch self {
        case .beginning:
            return .beginningSeek
        case .middle:
            return .middleSeek
        case .end:
            return .endSeek
        }
    }

    var plainLanguageName: String {
        switch self {
        case .beginning:
            return "beginning"
        case .middle:
            return "middle"
        case .end:
            return "end"
        }
    }
}

private struct ProbeLogEvent: Encodable {
    let sequence: Int
    let timestamp: String
    let name: String
    let values: [String: String]
}

private struct PreparedMediaSelections {
    let audioGroup: AVMediaSelectionGroup?
    let subtitleGroup: AVMediaSelectionGroup?
    let audioOptions: [ProbeMediaOption]
    let subtitleOptions: [ProbeMediaOption]
    let selectedAudioID: String
    let selectedSubtitleID: String
    let audioSelectionByID: [String: AVMediaSelectionOption]
    let subtitleSelectionByID: [String: AVMediaSelectionOption]
}

private final class ProbeSeekCompletion: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Bool, Never>?

    init(_ continuation: CheckedContinuation<Bool, Never>) {
        self.continuation = continuation
    }

    func resolve(_ result: Bool) {
        lock.lock()
        guard let continuation else {
            lock.unlock()
            return
        }
        self.continuation = nil
        lock.unlock()
        continuation.resume(returning: result)
    }
}

@MainActor
final class PlaybackProbeModel: ObservableObject {
    static let defaultTransferredAssetName = "Probe.mov"

    let player = AVPlayer()
    let playerEntity = Entity()

    @Published private(set) var assetName = "No movie selected"
    @Published private(set) var hasLoadedAsset = false
    @Published private(set) var isLoading = false
    @Published private(set) var isPlaying = false
    @Published private(set) var playerItemStatusText = "Not loaded"
    @Published private(set) var renderingStatusText = "Not loaded"
    @Published private(set) var actualPresentationText = "Not available"
    @Published private(set) var isActuallySpatial = false
    @Published private(set) var isSpatialPresentationRequested = false
    @Published private(set) var currentSeconds = 0.0
    @Published private(set) var durationSeconds = 0.0
    @Published private(set) var sourceFileSizeBytes: Int64?
    @Published private(set) var sourceSHA256 = ""
    @Published private(set) var failure: ProbeFailure?
    @Published private(set) var audioOptions: [ProbeMediaOption] = []
    @Published private(set) var subtitleOptions: [ProbeMediaOption] = []
    @Published var selectedAudioID = ""
    @Published var selectedSubtitleID = "off"
    @Published private(set) var validationPhase: PlaybackValidationPhase = .selectMovie
    @Published private(set) var validationChecks = PlaybackCheckID.allCases.map { PlaybackCheck(id: $0) }
    @Published private(set) var observations = PlaybackObservations()
    @Published private(set) var validationResult: PlaybackValidationResult?
    @Published private(set) var validationReportText = ""
    @Published private(set) var currentValidationStepText = ""
    @Published private(set) var playerComponentInstalled = false

    let stereoDecodeSupported = VTIsStereoMVHEVCDecodeSupported()

    private var itemStatusObservation: NSKeyValueObservation?
    private var timeObserver: Any?
    private var statusTask: Task<Void, Never>?
    private var loadTask: Task<Void, Never>?
    private var validationTask: Task<Void, Never>?
    private var logSequence = 0
    private var audioGroup: AVMediaSelectionGroup?
    private var subtitleGroup: AVMediaSelectionGroup?
    private var audioSelectionByID: [String: AVMediaSelectionOption] = [:]
    private var subtitleSelectionByID: [String: AVMediaSelectionOption] = [:]
    private var automatedValidationStarted = false
    private var loadGeneration = 0
    private var validationGeneration = 0
    private var componentStatusSampleSequence = 0
    private var currentImportedURL: URL?
    private var hasBootstrapped = false

    private var environment: [String: String] {
        ProcessInfo.processInfo.environment
    }

    private var shouldRunAutomatedProbe: Bool {
        environment["BD_TO_AVP_PROBE_AUTORUN"] == "1"
    }

    var decodeSupportText: String {
        stereoDecodeSupported ? "Supported" : "Unavailable"
    }

    var canControlPlayback: Bool {
        player.currentItem?.status == .readyToPlay && validationPhase != .running
    }

    var canSeek: Bool {
        canControlPlayback && durationSeconds.isFinite && durationSeconds > 0
    }

    var canStartValidation: Bool {
        !isLoading
            && hasLoadedAsset
            && playerComponentInstalled
            && player.currentItem?.status == .readyToPlay
            && durationSeconds.isFinite
            && durationSeconds > 0
            && validationPhase != .preparing
            && validationPhase != .running
            && validationPhase != .observations
    }

    var canFinishObservations: Bool {
        !isLoading && validationPhase == .observations && observations.isComplete
    }

    var timeSummary: String {
        "\(formatTime(currentSeconds)) / \(formatTime(durationSeconds))"
    }

    var completedCheckCount: Int {
        validationChecks.count(where: { $0.status == .passed || $0.status == .failed })
    }

    var automaticChecksPassed: Bool {
        validationChecks.allSatisfy { $0.status == .passed }
    }

    var automaticStatusText: String {
        let failedCount = validationChecks.count(where: { $0.status == .failed })
        if failedCount > 0 {
            return "Automatic checks found \(failedCount) problem\(failedCount == 1 ? "" : "s")"
        }
        if completedCheckCount == validationChecks.count {
            return "Automatic checks passed"
        }
        return "\(completedCheckCount) of \(validationChecks.count) checks complete"
    }

    deinit {
        statusTask?.cancel()
        loadTask?.cancel()
        validationTask?.cancel()
        if let timeObserver {
            player.removeTimeObserver(timeObserver)
        }
    }

    func bootstrap() async {
        guard !hasBootstrapped else {
            return
        }
        hasBootstrapped = true
        let cleanupSucceeded = await Self.cleanupImportedAssets()
        emit(
            "capability",
            values: [
                "stereo_mv_hevc_decode": String(stereoDecodeSupported),
                "visionos_version": ProcessInfo.processInfo.operatingSystemVersionString,
            ]
        )
        if !cleanupSucceeded {
            emit(
                "warning",
                values: [
                    "category": "cache_cleanup_failed",
                    "message": "A previous temporary movie could not be removed.",
                ]
            )
        }

        if !stereoDecodeSupported {
            emit(
                "warning",
                values: [
                    "category": ProbeFailureCategory.unsupportedDecode.rawValue,
                    "message": "This device does not currently report stereo MV-HEVC decode support.",
                ]
            )
        }

        let requestedAssetPath = environment["BD_TO_AVP_PROBE_ASSET"]?.trimmingCharacters(in: .whitespacesAndNewlines)
        let shouldLoadTransferredAsset = shouldRunAutomatedProbe || requestedAssetPath?.isEmpty == false
        if shouldLoadTransferredAsset {
            if let automaticAssetURL = automaticAssetURL() {
                loadGeneration += 1
                let generation = loadGeneration
                let requestedDisplayName = environment["BD_TO_AVP_PROBE_SOURCE_NAME"]?
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                let displayName = requestedDisplayName?.isEmpty == false
                    ? requestedDisplayName ?? automaticAssetURL.lastPathComponent
                    : automaticAssetURL.lastPathComponent
                do {
                    let localURL = try await Self.copyImportedAsset(automaticAssetURL)
                    guard generation == loadGeneration else {
                        removeImportedAsset(localURL, reason: "superseded_automatic_import")
                        return
                    }
                    let loaded = await loadAsset(
                        at: localURL,
                        displayName: displayName,
                        generation: generation
                    )
                    if loaded {
                        currentImportedURL = localURL
                    } else {
                        removeImportedAsset(localURL, reason: "automatic_load_failed")
                    }
                } catch {
                    guard generation == loadGeneration else {
                        return
                    }
                    reportLoadFailure(
                        category: .transferFailure,
                        title: "Transferred movie could not be prepared",
                        message: error.localizedDescription
                    )
                }
            } else {
                setFailure(
                    category: .missingFile,
                    title: "Transferred movie is missing",
                    message: "Copy a finalized movie to Documents/\(Self.defaultTransferredAssetName) or set BD_TO_AVP_PROBE_ASSET."
                )
            }
        }
    }

    func installPlayerComponent() {
        guard !playerComponentInstalled else {
            return
        }

        var component = VideoPlayerComponent(avPlayer: player)
        component.desiredViewingMode = .stereo
        component.desiredSpatialVideoMode = .screen
        component.desiredImmersiveViewingMode = .portal
        playerEntity.components.set(component)
        playerEntity.position = .zero
        playerEntity.scale = SIMD3<Float>(repeating: 0.20)
        playerComponentInstalled = true
        startStatusPolling()

        emit(
            "component_installed",
            values: [
                "desired_viewing_mode": "stereo",
                "desired_spatial_video_mode": "screen",
                "desired_immersive_viewing_mode": "portal",
            ]
        )

        scheduleAutomatedValidationIfNeeded()
    }

    func importAsset(from sourceURL: URL) {
        loadGeneration += 1
        let requestedGeneration = loadGeneration
        let displayName = sourceURL.lastPathComponent
        let previousImportedURL = currentImportedURL
        isLoading = true
        failure = nil
        requestSpatialPresentation(false)
        resetValidationState(phase: .preparing)
        let didAccess = sourceURL.startAccessingSecurityScopedResource()

        loadTask?.cancel()
        loadTask = Task {
            defer {
                if didAccess {
                    sourceURL.stopAccessingSecurityScopedResource()
                }
            }

            do {
                let localURL = try await Self.copyImportedAsset(sourceURL)
                guard requestedGeneration == loadGeneration else {
                    removeImportedAsset(localURL, reason: "superseded_import")
                    return
                }

                let loaded = await loadAsset(
                    at: localURL,
                    displayName: displayName,
                    generation: requestedGeneration
                )
                guard requestedGeneration == loadGeneration else {
                    removeImportedAsset(localURL, reason: "superseded_load")
                    return
                }

                if loaded {
                    currentImportedURL = localURL
                    if let previousImportedURL, previousImportedURL != localURL {
                        removeImportedAsset(previousImportedURL, reason: "replaced_movie")
                    }
                } else {
                    removeImportedAsset(localURL, reason: "import_load_failed")
                }
            } catch {
                guard requestedGeneration == loadGeneration else {
                    return
                }
                reportLoadFailure(
                    category: .transferFailure,
                    title: "Movie import failed",
                    message: error.localizedDescription
                )
            }
        }
    }

    func reportImportFailure(_ error: Error) {
        if error is CancellationError {
            return
        }
        let errorValue = error as NSError
        if errorValue.domain == NSCocoaErrorDomain && errorValue.code == NSUserCancelledError {
            return
        }

        reportLoadFailure(
            category: .transferFailure,
            title: "Movie selection failed",
            message: error.localizedDescription
        )
    }

    func startGuidedValidation() {
        guard canStartValidation else {
            return
        }

        validationTask?.cancel()
        validationGeneration += 1
        let generation = validationGeneration
        validationChecks = PlaybackCheckID.allCases.map { PlaybackCheck(id: $0) }
        observations = PlaybackObservations()
        validationResult = nil
        validationReportText = ""
        validationPhase = .running
        currentValidationStepText = "Preparing the playback check…"
        requestSpatialPresentation(true)

        validationTask = Task { [weak self] in
            await self?.runGuidedValidation(generation: generation)
        }
    }

    func setObservation(
        _ answer: PlaybackObservationAnswer,
        for keyPath: WritableKeyPath<PlaybackObservations, PlaybackObservationAnswer>
    ) {
        observations[keyPath: keyPath] = answer
    }

    func finishGuidedValidation() {
        guard canFinishObservations else {
            return
        }

        let result = PlaybackValidationRules.result(
            checks: validationChecks,
            observations: observations
        )
        validationResult = result
        validationPhase = .complete
        currentValidationStepText = ""

        let report = PlaybackValidationReport(
            schemaVersion: 1,
            validatorVersion: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "development",
            validatorBuild: Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "development",
            generatedAt: ISO8601DateFormatter().string(from: Date()),
            operatingSystem: ProcessInfo.processInfo.operatingSystemVersionString,
            source: PlaybackSourceSummary(
                fileName: assetName,
                sha256: sourceSHA256,
                sizeBytes: sourceFileSizeBytes,
                durationSeconds: durationSeconds,
                audioOptionCount: audioOptions.count,
                subtitleOptionCount: max(0, subtitleOptions.count - 1)
            ),
            automaticChecks: validationChecks,
            observations: observations,
            result: result
        )

        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        validationReportText = (try? encoder.encode(report)).flatMap { String(data: $0, encoding: .utf8) } ?? ""

        emit(
            "guided_validation_complete",
            values: [
                "result": result.rawValue,
                "video_remained_visible": observations.videoRemainedVisible.rawValue,
                "appeared_three_dimensional": observations.appearedThreeDimensional.rawValue,
            ]
        )
    }

    func prepareForAnotherRun() {
        guard hasLoadedAsset else {
            return
        }
        requestSpatialPresentation(false)
        resetValidationState(phase: .ready)
    }

    func togglePlayback() {
        guard canControlPlayback else {
            return
        }

        if isPlaying {
            player.pause()
        } else {
            player.play()
        }
    }

    func seek(to position: ProbeSeekPosition) {
        guard canSeek else {
            return
        }

        Task {
            let target = seekTarget(for: position)
            let finished = await seek(to: target)
            emit(
                "manual_seek",
                values: [
                    "position": position.rawValue,
                    "target_seconds": formatNumber(target),
                    "finished": String(finished),
                    "actual_spatial_video_mode": isActuallySpatial ? "spatial" : "screen",
                ]
            )
        }
    }

    func selectAudio(_ identifier: String) {
        guard
            let currentItem = player.currentItem,
            let audioGroup,
            let option = audioSelectionByID[identifier]
        else {
            return
        }

        currentItem.select(option, in: audioGroup)
        emit("audio_selected", values: ["name": option.displayName])
    }

    func selectSubtitle(_ identifier: String) {
        guard let currentItem = player.currentItem, let subtitleGroup else {
            return
        }

        let option = subtitleSelectionByID[identifier]
        currentItem.select(option, in: subtitleGroup)
        emit("subtitle_selected", values: ["name": option?.displayName ?? "Off"])
    }

    private func loadAsset(at url: URL, displayName: String, generation: Int) async -> Bool {
        guard FileManager.default.fileExists(atPath: url.path) else {
            if generation == loadGeneration {
                reportLoadFailure(
                    category: .missingFile,
                    title: "Movie is missing",
                    message: "The selected movie is no longer available at \(displayName)."
                )
            }
            return false
        }

        isLoading = true
        let asset = AVURLAsset(url: url)
        let item = AVPlayerItem(asset: asset)

        do {
            let duration = try await asset.load(.duration)
            guard generation == loadGeneration, !Task.isCancelled else {
                return false
            }
            let preparedMediaSelections = try await prepareMediaSelections(
                for: asset,
                item: item,
                generation: generation
            )
            guard generation == loadGeneration, !Task.isCancelled else {
                return false
            }
            let sourceHash = try await PlaybackArtifactHasher.sha256Hex(at: url)
            guard generation == loadGeneration, !Task.isCancelled else {
                return false
            }

            validationTask?.cancel()
            failure = nil
            hasLoadedAsset = true
            assetName = displayName
            playerItemStatusText = "Loading"
            renderingStatusText = "Loading"
            actualPresentationText = "Waiting for playback"
            isActuallySpatial = false
            requestSpatialPresentation(false)
            currentSeconds = 0
            durationSeconds = duration.seconds.isFinite ? max(0, duration.seconds) : 0
            sourceFileSizeBytes = fileSize(at: url)
            sourceSHA256 = sourceHash
            audioGroup = preparedMediaSelections.audioGroup
            subtitleGroup = preparedMediaSelections.subtitleGroup
            audioOptions = preparedMediaSelections.audioOptions
            subtitleOptions = preparedMediaSelections.subtitleOptions
            selectedAudioID = preparedMediaSelections.selectedAudioID
            selectedSubtitleID = preparedMediaSelections.selectedSubtitleID
            audioSelectionByID = preparedMediaSelections.audioSelectionByID
            subtitleSelectionByID = preparedMediaSelections.subtitleSelectionByID
            automatedValidationStarted = false
            resetValidationState(phase: .preparing)

            observe(item, generation: generation)
            player.replaceCurrentItem(with: item)
            emit(
                "asset_loaded",
                values: [
                    "file": displayName,
                    "sha256": sourceHash,
                    "duration_seconds": formatNumber(durationSeconds),
                    "file_size_bytes": sourceFileSizeBytes.map(String.init) ?? "unknown",
                    "audio_options": String(audioOptions.count),
                    "subtitle_options": String(max(0, subtitleOptions.count - 1)),
                ]
            )
        } catch is CancellationError {
            return false
        } catch {
            guard generation == loadGeneration, !Task.isCancelled else {
                return false
            }
            reportLoadFailure(
                category: .malformedMetadata,
                title: "Movie metadata is unreadable",
                message: error.localizedDescription
            )
            return false
        }

        player.play()
        return true
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
                guard let self else {
                    return
                }
                self.currentSeconds = max(0, time.seconds.isFinite ? time.seconds : 0)
                self.isPlaying = self.player.rate != 0
            }
        }
    }

    private func handleItemStatus(_ item: AVPlayerItem, generation: Int) {
        guard generation == loadGeneration, item === player.currentItem else {
            return
        }

        switch item.status {
        case .unknown:
            playerItemStatusText = "Loading"
        case .readyToPlay:
            playerItemStatusText = "Ready to play"
            isLoading = false
            if validationPhase == .preparing {
                validationPhase = .ready
            }
            emit("player_item", values: ["status": "ready_to_play"])
            scheduleAutomatedValidationIfNeeded()
        case .failed:
            setFailure(
                category: .playbackFailure,
                title: "The movie could not be opened",
                message: item.error?.localizedDescription ?? "The player failed without an error description."
            )
        @unknown default:
            playerItemStatusText = "Unknown"
        }
    }

    private func startStatusPolling() {
        statusTask?.cancel()
        statusTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 250_000_000)
                self?.refreshComponentStatus()
            }
        }
    }

    private func refreshComponentStatus() {
        guard hasLoadedAsset, let component = playerEntity.components[VideoPlayerComponent.self] else {
            return
        }
        componentStatusSampleSequence += 1

        let newRenderingStatus = component.currentRenderingStatus == .ready ? "Ready" : "Loading"
        let spatialMode = component.spatialVideoMode == .spatial ? "Spatial" : "Screen fallback"
        let viewingMode: String
        switch component.viewingMode {
        case .stereo:
            viewingMode = "Stereo"
        case .mono:
            viewingMode = "Mono"
        case nil:
            viewingMode = "Unknown"
        @unknown default:
            viewingMode = "Unknown"
        }

        let immersiveMode: String
        switch component.immersiveViewingMode {
        case .portal:
            immersiveMode = "Portal"
        case .full:
            immersiveMode = "Full"
        case .progressive:
            immersiveMode = "Progressive"
        case nil:
            immersiveMode = "None"
        @unknown default:
            immersiveMode = "Unknown"
        }

        let newPresentation = "\(viewingMode) · \(spatialMode) · \(immersiveMode)"
        let didChange = newRenderingStatus != renderingStatusText || newPresentation != actualPresentationText

        renderingStatusText = newRenderingStatus
        actualPresentationText = newPresentation
        isActuallySpatial = component.spatialVideoMode == .spatial
            && component.viewingMode == .stereo
            && component.immersiveViewingMode == .portal

        if didChange {
            emit(
                "component_status",
                values: [
                    "rendering_status": newRenderingStatus.lowercased(),
                    "viewing_mode": viewingMode.lowercased(),
                    "spatial_video_mode": component.spatialVideoMode == .spatial ? "spatial" : "screen",
                    "immersive_viewing_mode": immersiveMode.lowercased(),
                ]
            )
        }

        scheduleAutomatedValidationIfNeeded()
    }

    private func scheduleAutomatedValidationIfNeeded() {
        guard
            shouldRunAutomatedProbe,
            !automatedValidationStarted,
            playerComponentInstalled,
            canStartValidation
        else {
            return
        }

        automatedValidationStarted = true
        startGuidedValidation()
    }

    private func runGuidedValidation(generation: Int) async {
        guard let validationItem = player.currentItem, isValidationCurrent(generation, item: validationItem) else {
            return
        }

        updateCheck(
            .stereoDecode,
            status: stereoDecodeSupported ? .passed : .failed,
            detail: stereoDecodeSupported
                ? "This Vision Pro reports support for stereo MV-HEVC playback."
                : "This device does not report support for stereo MV-HEVC playback."
        )

        let playerReady = player.currentItem?.status == .readyToPlay
        updateCheck(
            .playerReady,
            status: playerReady ? .passed : .failed,
            detail: playerReady ? "The movie is ready to play." : "The movie did not become ready to play."
        )

        currentValidationStepText = "Waiting for the picture…"
        updateCheck(.renderingReady, status: .running, detail: "Waiting for the picture to become ready.")
        let renderingReady = await waitForCondition(maxAttempts: 40) { [weak self] in
            self?.renderingStatusText == "Ready"
        }
        guard isValidationCurrent(generation, item: validationItem) else {
            return
        }
        updateCheck(
            .renderingReady,
            status: renderingReady ? .passed : .failed,
            detail: renderingReady ? "RealityKit reports that the picture is ready." : "The picture did not become ready within 10 seconds."
        )

        currentValidationStepText = "Checking 3D presentation…"
        updateCheck(.spatialPresentation, status: .running, detail: "Waiting for stereoscopic spatial presentation.")
        let spatialPresentationReady = await waitForCondition(maxAttempts: 40) { [weak self] in
            self?.isActuallySpatial == true
        }
        guard isValidationCurrent(generation, item: validationItem) else {
            return
        }
        updateCheck(
            .spatialPresentation,
            status: spatialPresentationReady ? .passed : .failed,
            detail: spatialPresentationReady
                ? "The movie is rendering as stereoscopic spatial video."
                : "The movie remained in flat-screen fallback instead of spatial presentation."
        )

        for position in ProbeSeekPosition.allCases {
            guard isValidationCurrent(generation, item: validationItem) else {
                return
            }

            currentValidationStepText = "Checking the \(position.plainLanguageName) of the movie…"
            updateCheck(
                position.checkID,
                status: .running,
                detail: "Moving playback to the \(position.plainLanguageName)."
            )

            let target = seekTarget(for: position)
            let finished = await seek(to: target)
            guard isValidationCurrent(generation, item: validationItem) else {
                return
            }

            let landedSeconds = normalizedTime(player.currentTime().seconds)
            let statusSampleAfterSeek = componentStatusSampleSequence
            let allowedTargetError = min(1, max(0.25, durationSeconds * 0.0005))
            let targetError = abs(landedSeconds - target)
            let availablePlaybackSeconds = max(0, durationSeconds - landedSeconds)
            let requiredPlaybackAdvance = min(0.4, max(0.05, availablePlaybackSeconds * 0.5))
            player.play()
            _ = await waitForCondition(maxAttempts: 12) { [weak self] in
                guard let self else {
                    return false
                }
                return self.normalizedTime(self.player.currentTime().seconds) - landedSeconds >= requiredPlaybackAdvance
            }
            guard isValidationCurrent(generation, item: validationItem) else {
                return
            }
            let receivedFreshStatus = await waitForCondition(maxAttempts: 8) { [weak self] in
                guard let self else {
                    return false
                }
                return self.componentStatusSampleSequence >= statusSampleAfterSeek + 2
            }
            guard isValidationCurrent(generation, item: validationItem) else {
                return
            }

            let normalizedSeconds = normalizedTime(player.currentTime().seconds)
            currentSeconds = normalizedSeconds
            let evidence = PlaybackSeekEvidence(
                seekFinished: finished,
                targetErrorSeconds: targetError,
                allowedTargetErrorSeconds: allowedTargetError,
                playbackAdvanceSeconds: max(0, normalizedSeconds - landedSeconds),
                requiredPlaybackAdvanceSeconds: requiredPlaybackAdvance,
                renderingReady: receivedFreshStatus && renderingStatusText == "Ready",
                spatialPresentation: receivedFreshStatus && isActuallySpatial
            )
            let passed = PlaybackValidationRules.seekPassed(evidence)

            updateCheck(
                position.checkID,
                status: passed ? .passed : .failed,
                detail: seekDetail(
                    position: position,
                    evidence: evidence
                )
            )

            emit(
                "guided_seek",
                values: [
                    "position": position.rawValue,
                    "target_seconds": formatNumber(target),
                    "current_seconds": formatNumber(normalizedSeconds),
                    "finished": String(finished),
                    "target_error_seconds": formatNumber(targetError),
                    "playback_advance_seconds": formatNumber(evidence.playbackAdvanceSeconds),
                    "fresh_rendering_status": String(evidence.renderingReady),
                    "spatial_presentation": String(evidence.spatialPresentation),
                ]
            )
        }

        player.pause()
        isPlaying = false
        currentValidationStepText = "Automatic checks finished."

        emit(
            "automated_probe_complete",
            values: [
                "result": automaticChecksPassed ? "pass" : "fail",
                "rendering_status": renderingStatusText.lowercased(),
                "actual_presentation": actualPresentationText.lowercased(),
                "checks_passed": String(automaticChecksPassed),
                "audio_options": String(audioOptions.count),
                "subtitle_options": String(max(0, subtitleOptions.count - 1)),
            ]
        )

        requestSpatialPresentation(false)
        _ = await waitForCondition(maxAttempts: 20) { [weak self] in
            self?.isActuallySpatial == false
        }
        guard isValidationCurrent(generation, item: validationItem) else {
            return
        }
        validationPhase = .observations
    }

    private func waitForCondition(
        maxAttempts: Int,
        condition: @MainActor () -> Bool
    ) async -> Bool {
        for _ in 0 ..< maxAttempts {
            if condition() {
                return true
            }
            if Task.isCancelled {
                return false
            }
            try? await Task.sleep(nanoseconds: 250_000_000)
        }
        return condition()
    }

    private func seekTarget(for position: ProbeSeekPosition) -> Double {
        switch position {
        case .beginning:
            return 0
        case .middle:
            return durationSeconds / 2
        case .end:
            return max(0, durationSeconds - min(1, durationSeconds / 10))
        }
    }

    private func seek(to seconds: Double) async -> Bool {
        await withCheckedContinuation { continuation in
            let completion = ProbeSeekCompletion(continuation)
            player.seek(
                to: CMTime(seconds: seconds, preferredTimescale: 600),
                toleranceBefore: .zero,
                toleranceAfter: .zero
            ) { finished in
                completion.resolve(finished)
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
                completion.resolve(false)
            }
        }
    }

    private func seekDetail(
        position: ProbeSeekPosition,
        evidence: PlaybackSeekEvidence
    ) -> String {
        if !evidence.seekFinished || evidence.targetErrorSeconds > evidence.allowedTargetErrorSeconds {
            return "Playback did not reliably reach the \(position.plainLanguageName)."
        }
        if evidence.playbackAdvanceSeconds < evidence.requiredPlaybackAdvanceSeconds {
            return "Playback reached the \(position.plainLanguageName), but the picture did not resume."
        }
        if !evidence.renderingReady || !evidence.spatialPresentation {
            return "Playback reached the \(position.plainLanguageName), but 3D presentation was lost."
        }
        return "The \(position.plainLanguageName) played and remained spatial."
    }

    private func isValidationCurrent(_ generation: Int, item: AVPlayerItem) -> Bool {
        !Task.isCancelled && generation == validationGeneration && item === player.currentItem
    }

    private func normalizedTime(_ seconds: Double) -> Double {
        seconds.isFinite ? max(0, seconds) : 0
    }

    private func updateCheck(_ identifier: PlaybackCheckID, status: PlaybackCheckStatus, detail: String) {
        guard let index = validationChecks.firstIndex(where: { $0.id == identifier }) else {
            return
        }
        validationChecks[index].status = status
        validationChecks[index].detail = detail
    }

    private func requestSpatialPresentation(_ requested: Bool) {
        guard var component = playerEntity.components[VideoPlayerComponent.self] else {
            return
        }

        component.desiredViewingMode = .stereo
        component.desiredSpatialVideoMode = requested ? .spatial : .screen
        component.desiredImmersiveViewingMode = .portal
        playerEntity.components.set(component)
        isSpatialPresentationRequested = requested

        emit(
            "presentation_requested",
            values: [
                "spatial": String(requested),
                "viewing_mode": "stereo",
                "immersive_viewing_mode": "portal",
            ]
        )
    }

    private func resetValidationState(phase: PlaybackValidationPhase) {
        validationGeneration += 1
        validationTask?.cancel()
        validationChecks = PlaybackCheckID.allCases.map { PlaybackCheck(id: $0) }
        observations = PlaybackObservations()
        validationResult = nil
        validationReportText = ""
        currentValidationStepText = ""
        validationPhase = phase
    }

    private func prepareMediaSelections(
        for asset: AVAsset,
        item: AVPlayerItem,
        generation: Int
    ) async throws -> PreparedMediaSelections {
        let loadedAudioGroup = try await asset.loadMediaSelectionGroup(for: .audible)
        guard generation == loadGeneration, !Task.isCancelled else {
            throw CancellationError()
        }
        let loadedSubtitleGroup = try await asset.loadMediaSelectionGroup(for: .legible)
        guard generation == loadGeneration, !Task.isCancelled else {
            throw CancellationError()
        }

        var preparedAudioSelectionByID: [String: AVMediaSelectionOption] = [:]
        var preparedSubtitleSelectionByID: [String: AVMediaSelectionOption] = [:]
        let preparedAudioOptions: [ProbeMediaOption]
        let preparedSubtitleOptions: [ProbeMediaOption]
        let preparedSelectedAudioID: String
        let preparedSelectedSubtitleID: String

        if let loadedAudioGroup {
            preparedAudioOptions = loadedAudioGroup.options.enumerated().map { index, option in
                let identifier = "audio-\(index)"
                preparedAudioSelectionByID[identifier] = option
                return ProbeMediaOption(id: identifier, name: option.displayName)
            }
            preparedSelectedAudioID = preparedAudioOptions.first(where: { option in
                guard let selection = preparedAudioSelectionByID[option.id] else {
                    return false
                }
                return item.currentMediaSelection.selectedMediaOption(in: loadedAudioGroup) === selection
            })?.id ?? preparedAudioOptions.first?.id ?? ""
        } else {
            preparedAudioOptions = []
            preparedSelectedAudioID = ""
        }

        if let loadedSubtitleGroup {
            var options = [ProbeMediaOption(id: "off", name: "Off")]
            options += loadedSubtitleGroup.options.enumerated().map { index, option in
                let identifier = "subtitle-\(index)"
                preparedSubtitleSelectionByID[identifier] = option
                return ProbeMediaOption(id: identifier, name: option.displayName)
            }
            preparedSubtitleOptions = options
            preparedSelectedSubtitleID = options.first(where: { option in
                guard let selection = preparedSubtitleSelectionByID[option.id] else {
                    return false
                }
                return item.currentMediaSelection.selectedMediaOption(in: loadedSubtitleGroup) === selection
            })?.id ?? "off"
        } else {
            preparedSubtitleOptions = []
            preparedSelectedSubtitleID = "off"
        }

        return PreparedMediaSelections(
            audioGroup: loadedAudioGroup,
            subtitleGroup: loadedSubtitleGroup,
            audioOptions: preparedAudioOptions,
            subtitleOptions: preparedSubtitleOptions,
            selectedAudioID: preparedSelectedAudioID,
            selectedSubtitleID: preparedSelectedSubtitleID,
            audioSelectionByID: preparedAudioSelectionByID,
            subtitleSelectionByID: preparedSubtitleSelectionByID
        )
    }

    private func automaticAssetURL() -> URL? {
        let fileManager = FileManager.default
        let documentsURL = fileManager.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let requestedPath = environment["BD_TO_AVP_PROBE_ASSET"]?.trimmingCharacters(in: .whitespacesAndNewlines)

        if let requestedPath, !requestedPath.isEmpty {
            let candidate = requestedPath.hasPrefix("/")
                ? URL(fileURLWithPath: requestedPath)
                : documentsURL.appendingPathComponent(requestedPath)
            return fileManager.fileExists(atPath: candidate.path) ? candidate : nil
        }

        let defaultURL = documentsURL.appendingPathComponent(Self.defaultTransferredAssetName)
        return fileManager.fileExists(atPath: defaultURL.path) ? defaultURL : nil
    }

    private func fileSize(at url: URL) -> Int64? {
        guard let fileSize = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize else {
            return nil
        }
        return Int64(fileSize)
    }

    private func reportLoadFailure(category: ProbeFailureCategory, title: String, message: String) {
        isLoading = false
        if hasLoadedAsset {
            failure = ProbeFailure(category: category, title: title, message: message)
            if validationPhase == .preparing {
                validationPhase = .ready
            }
            emit(
                "failure",
                values: [
                    "category": category.rawValue,
                    "message": message,
                    "active_movie_preserved": "true",
                ]
            )
        } else {
            setFailure(category: category, title: title, message: message)
        }
    }

    private func setFailure(category: ProbeFailureCategory, title: String, message: String) {
        validationTask?.cancel()
        isLoading = false
        hasLoadedAsset = false
        player.pause()
        player.replaceCurrentItem(with: nil)
        playerItemStatusText = "Failed"
        renderingStatusText = "Not loaded"
        actualPresentationText = "Not available"
        isPlaying = false
        currentSeconds = 0
        durationSeconds = 0
        requestSpatialPresentation(false)
        sourceFileSizeBytes = nil
        sourceSHA256 = ""
        audioOptions = []
        subtitleOptions = []
        audioGroup = nil
        subtitleGroup = nil
        audioSelectionByID.removeAll()
        subtitleSelectionByID.removeAll()
        if let currentImportedURL {
            removeImportedAsset(currentImportedURL, reason: "failed_movie")
            self.currentImportedURL = nil
        }
        failure = ProbeFailure(category: category, title: title, message: message)
        resetValidationState(phase: .selectMovie)
        emit(
            "failure",
            values: [
                "category": category.rawValue,
                "message": message,
            ]
        )
    }

    private func emit(_ name: String, values: [String: String]) {
        logSequence += 1
        let event = ProbeLogEvent(
            sequence: logSequence,
            timestamp: ISO8601DateFormatter().string(from: Date()),
            name: name,
            values: values
        )
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        guard let data = try? encoder.encode(event), let json = String(data: data, encoding: .utf8) else {
            return
        }
        print("BD_TO_AVP_PLAYBACK_PROBE \(json)")
    }

    private func removeImportedAsset(_ url: URL, reason: String) {
        guard FileManager.default.fileExists(atPath: url.path) else {
            return
        }

        do {
            try FileManager.default.removeItem(at: url)
        } catch {
            emit(
                "warning",
                values: [
                    "category": "cache_cleanup_failed",
                    "message": error.localizedDescription,
                    "reason": reason,
                ]
            )
        }
    }

    private func formatTime(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds > 0 else {
            return "0:00"
        }
        let wholeSeconds = Int(seconds.rounded(.down))
        return String(format: "%d:%02d", wholeSeconds / 60, wholeSeconds % 60)
    }

    private func formatNumber(_ value: Double) -> String {
        String(format: "%.3f", value)
    }

    private nonisolated static func cleanupImportedAssets() async -> Bool {
        await Task.detached(priority: .utility) {
            let fileManager = FileManager.default
            do {
                let cachesURL = try fileManager.url(
                    for: .cachesDirectory,
                    in: .userDomainMask,
                    appropriateFor: nil,
                    create: true
                )
                let destinationDirectory = cachesURL.appendingPathComponent("PlaybackValidator", isDirectory: true)
                guard fileManager.fileExists(atPath: destinationDirectory.path) else {
                    return true
                }
                try fileManager.removeItem(at: destinationDirectory)
                return !fileManager.fileExists(atPath: destinationDirectory.path)
            } catch {
                return false
            }
        }.value
    }

    private nonisolated static func copyImportedAsset(_ sourceURL: URL) async throws -> URL {
        try await Task.detached(priority: .userInitiated) {
            let fileManager = FileManager.default
            let cachesURL = try fileManager.url(
                for: .cachesDirectory,
                in: .userDomainMask,
                appropriateFor: nil,
                create: true
            )
            let destinationDirectory = cachesURL.appendingPathComponent("PlaybackValidator", isDirectory: true)
            try fileManager.createDirectory(at: destinationDirectory, withIntermediateDirectories: true)

            let extensionName = sourceURL.pathExtension.isEmpty ? "mov" : sourceURL.pathExtension
            let destinationURL = destinationDirectory.appendingPathComponent("Imported-\(UUID().uuidString).\(extensionName)")
            try fileManager.copyItem(at: sourceURL, to: destinationURL)
            return destinationURL
        }.value
    }
}

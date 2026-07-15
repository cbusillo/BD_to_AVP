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
}

private struct ProbeLogEvent: Encodable {
    let sequence: Int
    let timestamp: String
    let name: String
    let values: [String: String]
}

@MainActor
final class PlaybackProbeModel: ObservableObject {
    static let defaultTransferredAssetName = "Probe.mov"

    let player = AVPlayer()
    let playerEntity = Entity()

    @Published private(set) var assetName = "No preview selected"
    @Published private(set) var hasLoadedAsset = false
    @Published private(set) var isLoading = false
    @Published private(set) var isPlaying = false
    @Published private(set) var playerItemStatusText = "Not loaded"
    @Published private(set) var renderingStatusText = "Not loaded"
    @Published private(set) var immersiveSpaceStatusText = "Not open"
    @Published private(set) var immersiveSpaceIsOpen = false
    @Published private(set) var actualPresentationText = "Not available"
    @Published private(set) var isActuallySpatial = false
    @Published private(set) var currentSeconds = 0.0
    @Published private(set) var durationSeconds = 0.0
    @Published private(set) var failure: ProbeFailure?
    @Published private(set) var audioOptions: [ProbeMediaOption] = []
    @Published private(set) var subtitleOptions: [ProbeMediaOption] = []
    @Published var selectedAudioID = ""
    @Published var selectedSubtitleID = "off"

    let stereoDecodeSupported = VTIsStereoMVHEVCDecodeSupported()

    private var itemStatusObservation: NSKeyValueObservation?
    private var timeObserver: Any?
    private var statusTask: Task<Void, Never>?
    private var automatedProbeTask: Task<Void, Never>?
    private var playerComponentInstalled = false
    private var logSequence = 0
    private var audioGroup: AVMediaSelectionGroup?
    private var subtitleGroup: AVMediaSelectionGroup?
    private var audioSelectionByID: [String: AVMediaSelectionOption] = [:]
    private var subtitleSelectionByID: [String: AVMediaSelectionOption] = [:]
    private var automatedProbeStarted = false

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
        player.currentItem?.status == .readyToPlay
    }

    var canSeek: Bool {
        canControlPlayback && durationSeconds.isFinite && durationSeconds > 0
    }

    var timeSummary: String {
        "\(formatTime(currentSeconds)) / \(formatTime(durationSeconds))"
    }

    deinit {
        statusTask?.cancel()
        automatedProbeTask?.cancel()
        if let timeObserver {
            player.removeTimeObserver(timeObserver)
        }
    }

    func bootstrap() async {
        emit(
            "capability",
            values: [
                "stereo_mv_hevc_decode": String(stereoDecodeSupported),
                "visionos_version": ProcessInfo.processInfo.operatingSystemVersionString,
            ]
        )

        if !stereoDecodeSupported {
            emit(
                "warning",
                values: [
                    "category": ProbeFailureCategory.unsupportedDecode.rawValue,
                    "message": "This device does not currently report stereo MV-HEVC decode support.",
                ]
            )
        }

        if let automaticAssetURL = automaticAssetURL() {
            await loadAsset(at: automaticAssetURL)
        } else if shouldRunAutomatedProbe {
            setFailure(
                category: .missingFile,
                title: "Transferred preview is missing",
                message: "Copy a finalized movie to Documents/\(Self.defaultTransferredAssetName) or set BD_TO_AVP_PROBE_ASSET."
            )
        }
    }

    func installPlayerComponent() {
        guard !playerComponentInstalled else {
            return
        }

        var component = VideoPlayerComponent(avPlayer: player)
        component.desiredViewingMode = .stereo
        component.desiredSpatialVideoMode = .spatial
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
                "desired_spatial_video_mode": "spatial",
                "desired_immersive_viewing_mode": "portal",
            ]
        )
    }

    func importAsset(from sourceURL: URL) {
        isLoading = true
        failure = nil
        let didAccess = sourceURL.startAccessingSecurityScopedResource()

        Task {
            defer {
                if didAccess {
                    sourceURL.stopAccessingSecurityScopedResource()
                }
            }
            do {
                let localURL = try await Self.copyImportedAsset(sourceURL)
                await loadAsset(at: localURL)
            } catch {
                setFailure(
                    category: .transferFailure,
                    title: "Preview import failed",
                    message: error.localizedDescription
                )
            }
        }
    }

    func reportImportFailure(_ error: Error) {
        setFailure(
            category: .transferFailure,
            title: "Preview selection failed",
            message: error.localizedDescription
        )
    }

    func recordImmersiveSpaceStatus(_ status: String, isOpen: Bool) {
        immersiveSpaceStatusText = status
        immersiveSpaceIsOpen = isOpen
        emit(
            "immersive_space",
            values: [
                "status": status.lowercased().replacingOccurrences(of: " ", with: "_"),
                "is_open": String(isOpen),
            ]
        )
        scheduleAutomatedProbeIfNeeded()
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
                "seek",
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

    private func loadAsset(at url: URL) async {
        guard FileManager.default.fileExists(atPath: url.path) else {
            setFailure(
                category: .missingFile,
                title: "Preview is missing",
                message: "The selected movie is no longer available at \(url.lastPathComponent)."
            )
            return
        }

        failure = nil
        isLoading = true
        hasLoadedAsset = true
        assetName = url.lastPathComponent
        playerItemStatusText = "Loading"
        renderingStatusText = "Loading"
        actualPresentationText = "Waiting for RealityKit"
        isActuallySpatial = false
        automatedProbeStarted = false

        let asset = AVURLAsset(url: url)
        let item = AVPlayerItem(asset: asset)
        observe(item)
        player.replaceCurrentItem(with: item)

        do {
            let duration = try await asset.load(.duration)
            durationSeconds = duration.seconds.isFinite ? max(0, duration.seconds) : 0
            try await loadMediaSelections(for: asset, item: item)
            emit(
                "asset_loaded",
                values: [
                    "file": url.lastPathComponent,
                    "duration_seconds": formatNumber(durationSeconds),
                    "audio_options": String(audioOptions.count),
                    "subtitle_options": String(max(0, subtitleOptions.count - 1)),
                ]
            )
        } catch {
            setFailure(
                category: .malformedMetadata,
                title: "Movie metadata is unreadable",
                message: error.localizedDescription
            )
            return
        }

        player.play()
    }

    private func observe(_ item: AVPlayerItem) {
        itemStatusObservation = item.observe(\.status, options: [.initial, .new]) { [weak self] observedItem, _ in
            Task { @MainActor in
                self?.handleItemStatus(observedItem)
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

    private func handleItemStatus(_ item: AVPlayerItem) {
        switch item.status {
        case .unknown:
            playerItemStatusText = "Loading"
        case .readyToPlay:
            playerItemStatusText = "Ready to play"
            isLoading = false
            emit("player_item", values: ["status": "ready_to_play"])
            scheduleAutomatedProbeIfNeeded()
        case .failed:
            setFailure(
                category: .playbackFailure,
                title: "AVPlayer could not load the movie",
                message: item.error?.localizedDescription ?? "The player item failed without an error description."
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

        let newRenderingStatus = component.currentRenderingStatus == .ready ? "Ready" : "Loading"
        let newSpatialMode = component.spatialVideoMode == .spatial ? "Spatial" : "Screen fallback"
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

        let newPresentation = "\(viewingMode) · \(newSpatialMode) · \(immersiveMode)"
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

        scheduleAutomatedProbeIfNeeded()
    }

    private func scheduleAutomatedProbeIfNeeded() {
        guard
            shouldRunAutomatedProbe,
            !automatedProbeStarted,
            immersiveSpaceIsOpen,
            isActuallySpatial,
            player.currentItem?.status == .readyToPlay,
            renderingStatusText == "Ready",
            canSeek
        else {
            return
        }

        automatedProbeStarted = true
        automatedProbeTask = Task { [weak self] in
            await self?.runAutomatedProbe()
        }
    }

    private func runAutomatedProbe() async {
        var seekResults: [String: Bool] = [:]

        for position in ProbeSeekPosition.allCases {
            let target = seekTarget(for: position)
            let finished = await seek(to: target)
            seekResults[position.rawValue] = finished
            player.play()
            try? await Task.sleep(nanoseconds: 600_000_000)
            emit(
                "automated_seek",
                values: [
                    "position": position.rawValue,
                    "target_seconds": formatNumber(target),
                    "current_seconds": formatNumber(currentSeconds),
                    "finished": String(finished),
                    "spatial_video_mode": isActuallySpatial ? "spatial" : "screen",
                ]
            )
        }

        let seeksPassed = seekResults.values.allSatisfy { $0 }
        let result = isActuallySpatial && seeksPassed ? "pass" : "fail"
        emit(
            "automated_probe_complete",
            values: [
                "result": result,
                "rendering_status": renderingStatusText.lowercased(),
                "actual_presentation": actualPresentationText.lowercased(),
                "seeks_passed": String(seeksPassed),
                "audio_options": String(audioOptions.count),
                "subtitle_options": String(max(0, subtitleOptions.count - 1)),
            ]
        )
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
            player.seek(
                to: CMTime(seconds: seconds, preferredTimescale: 600),
                toleranceBefore: .zero,
                toleranceAfter: .zero
            ) { finished in
                continuation.resume(returning: finished)
            }
        }
    }

    private func loadMediaSelections(for asset: AVAsset, item: AVPlayerItem) async throws {
        audioGroup = try await asset.loadMediaSelectionGroup(for: .audible)
        subtitleGroup = try await asset.loadMediaSelectionGroup(for: .legible)

        audioSelectionByID.removeAll()
        subtitleSelectionByID.removeAll()

        if let audioGroup {
            audioOptions = audioGroup.options.enumerated().map { index, option in
                let identifier = "audio-\(index)"
                audioSelectionByID[identifier] = option
                return ProbeMediaOption(id: identifier, name: option.displayName)
            }
            selectedAudioID = audioOptions.first(where: { option in
                guard let selection = audioSelectionByID[option.id] else {
                    return false
                }
                return item.currentMediaSelection.selectedMediaOption(in: audioGroup) === selection
            })?.id ?? audioOptions.first?.id ?? ""
        } else {
            audioOptions = []
            selectedAudioID = ""
        }

        if let subtitleGroup {
            subtitleOptions = [ProbeMediaOption(id: "off", name: "Off")]
            subtitleOptions += subtitleGroup.options.enumerated().map { index, option in
                let identifier = "subtitle-\(index)"
                subtitleSelectionByID[identifier] = option
                return ProbeMediaOption(id: identifier, name: option.displayName)
            }
            selectedSubtitleID = subtitleOptions.first(where: { option in
                guard let selection = subtitleSelectionByID[option.id] else {
                    return false
                }
                return item.currentMediaSelection.selectedMediaOption(in: subtitleGroup) === selection
            })?.id ?? "off"
        } else {
            subtitleOptions = []
            selectedSubtitleID = "off"
        }
    }

    private func automaticAssetURL() -> URL? {
        let fileManager = FileManager.default
        let documentsURL = fileManager.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let requestedPath = environment["BD_TO_AVP_PROBE_ASSET"]?.trimmingCharacters(in: .whitespacesAndNewlines)

        if let requestedPath, !requestedPath.isEmpty {
            let requestedURL = URL(fileURLWithPath: requestedPath)
            let candidate = requestedURL.path.hasPrefix("/")
                ? requestedURL
                : documentsURL.appendingPathComponent(requestedPath)
            return fileManager.fileExists(atPath: candidate.path) ? candidate : nil
        }

        let defaultURL = documentsURL.appendingPathComponent(Self.defaultTransferredAssetName)
        return fileManager.fileExists(atPath: defaultURL.path) ? defaultURL : nil
    }

    private func setFailure(category: ProbeFailureCategory, title: String, message: String) {
        isLoading = false
        playerItemStatusText = "Failed"
        failure = ProbeFailure(category: category, title: title, message: message)
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

    private nonisolated static func copyImportedAsset(_ sourceURL: URL) async throws -> URL {
        try await Task.detached(priority: .userInitiated) {
            let fileManager = FileManager.default
            let supportURL = try fileManager.url(
                for: .applicationSupportDirectory,
                in: .userDomainMask,
                appropriateFor: nil,
                create: true
            )
            let destinationDirectory = supportURL.appendingPathComponent("SpatialPlaybackProbe", isDirectory: true)
            try fileManager.createDirectory(at: destinationDirectory, withIntermediateDirectories: true)

            let extensionName = sourceURL.pathExtension.isEmpty ? "mov" : sourceURL.pathExtension
            let destinationURL = destinationDirectory.appendingPathComponent("CurrentPreview.\(extensionName)")
            if fileManager.fileExists(atPath: destinationURL.path) {
                try fileManager.removeItem(at: destinationURL)
            }
            try fileManager.copyItem(at: sourceURL, to: destinationURL)
            return destinationURL
        }.value
    }
}

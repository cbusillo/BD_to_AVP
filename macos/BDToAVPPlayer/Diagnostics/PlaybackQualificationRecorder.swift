#if BD_TO_AVP_QUALIFICATION
import AVFoundation
import Darwin
import Foundation

final class PlaybackQualificationRecorder {
    private static let sampleIntervalSeconds: TimeInterval = 15

    private let runID: String
    private let mediaID: String
    private let outputURL: URL
    private let handle: FileHandle
    private let bundle: Bundle
    private let dateProvider: () -> Date
    private let uptimeProvider: () -> TimeInterval
    private let thermalStateProvider: () -> ProcessInfo.ThermalState
    private let footprintProvider: () -> UInt64?
    private let hardwareModelProvider: () -> String
    private let startUptime: TimeInterval
    private var lastSampleUptime: TimeInterval?
    private var previousTimeControlStatus: String?
    private var didWriteFooter = false

    init?(
        runID: String,
        mediaID: String,
        fileManager: FileManager = .default,
        bundle: Bundle = .main,
        outputDirectoryURL: URL? = nil,
        dateProvider: @escaping () -> Date = Date.init,
        uptimeProvider: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        thermalStateProvider: @escaping () -> ProcessInfo.ThermalState = {
            ProcessInfo.processInfo.thermalState
        },
        footprintProvider: @escaping () -> UInt64? = PlaybackQualificationRecorder.physFootprintBytes,
        hardwareModelProvider: @escaping () -> String = PlaybackQualificationRecorder.hardwareModel
    ) {
        guard let safeRunID = Self.safeIdentifier(runID),
              let safeMediaID = Self.safeIdentifier(mediaID)
        else {
            return nil
        }

        let rootURL: URL
        if let outputDirectoryURL {
            rootURL = outputDirectoryURL
        } else {
            guard let applicationSupportURL = fileManager.urls(
                for: .applicationSupportDirectory,
                in: .userDomainMask
            ).first else {
                return nil
            }
            rootURL = applicationSupportURL
        }

        let directoryURL = rootURL
            .appendingPathComponent("BDToAVPPlayer", isDirectory: true)
            .appendingPathComponent("PlaybackQualification", isDirectory: true)
        let fileURL = directoryURL.appendingPathComponent("\(safeRunID).jsonl")

        do {
            try fileManager.createDirectory(at: directoryURL, withIntermediateDirectories: true)
            guard !fileManager.fileExists(atPath: fileURL.path) else {
                return nil
            }
            guard fileManager.createFile(atPath: fileURL.path, contents: nil) else {
                return nil
            }

            self.runID = safeRunID
            self.mediaID = safeMediaID
            self.outputURL = fileURL
            self.handle = try FileHandle(forWritingTo: fileURL)
            self.bundle = bundle
            self.dateProvider = dateProvider
            self.uptimeProvider = uptimeProvider
            self.thermalStateProvider = thermalStateProvider
            self.footprintProvider = footprintProvider
            self.hardwareModelProvider = hardwareModelProvider
            self.startUptime = uptimeProvider()

            writeHeader()
        } catch {
            return nil
        }
    }

    deinit {
        try? handle.close()
    }

    var fileURL: URL {
        outputURL
    }

    func recordPrepare(player: AVPlayer) {
        writeEvent("prepare", player: player)
    }

    func recordReady(player: AVPlayer) {
        writeEvent("ready", player: player)
    }

    func recordPlayRequested(player: AVPlayer) {
        writeEvent("play_requested", player: player, detail: "user_resume")
    }

    func recordPauseRequested(player: AVPlayer, detail: String = "user_pause") {
        writeEvent("pause_requested", player: player, detail: detail)
    }

    func recordSeekStarted(player: AVPlayer, detail: String = "seek") {
        writeEvent("seek_started", player: player, detail: detail)
    }

    func recordSeekCompleted(player: AVPlayer, detail: String = "seek") {
        writeEvent("seek_completed", player: player, detail: detail)
    }

    func recordEyeOrderChangeStarted(player: AVPlayer) {
        writeEvent("eye_order_change_started", player: player, detail: "eye_order_change")
    }

    func recordEyeOrderChangeCompleted(player: AVPlayer) {
        writeEvent("eye_order_change_completed", player: player, detail: "eye_order_change")
    }

    func recordEyeOrderChangeFailed(player: AVPlayer) {
        writeEvent("eye_order_change_failed", player: player, detail: "failed")
    }

    func recordSceneInactive(player: AVPlayer) {
        writeEvent("scene_inactive", player: player, detail: "scene_inactive")
    }

    func recordSceneActive(player: AVPlayer) {
        writeEvent("scene_active", player: player, detail: "scene_active")
    }

    func recordTimeControlChanged(
        status: AVPlayer.TimeControlStatus,
        playerTimeSeconds: TimeInterval,
        capturedAt: Date,
        capturedUptime: TimeInterval
    ) {
        let currentStatus = Self.timeControlStatusCategory(status)
        let transition = previousTimeControlStatus.map { "\($0)->\(currentStatus)" }
        let waitingTransitions = Set([
            "playing->waiting",
            "paused->waiting",
            "waiting->playing",
            "waiting->paused"
        ])
        let detail = transition.flatMap { waitingTransitions.contains($0) ? $0 : nil }
            ?? (currentStatus == "unknown" ? "none" : currentStatus)
        previousTimeControlStatus = currentStatus
        writeEvent(
            "time_control_changed",
            playerTimeSeconds: playerTimeSeconds,
            detail: detail,
            capturedAt: capturedAt,
            capturedUptime: capturedUptime
        )
    }

    func recordPlaybackFinished(player: AVPlayer, durationSeconds: TimeInterval) {
        writeEvent("playback_finished", player: player, detail: "finished")
        writeFooter(reason: "playback_finished", player: player, durationSeconds: durationSeconds)
    }

    func recordFailure(player: AVPlayer, durationSeconds: TimeInterval) {
        writeEvent("failure", player: player, detail: "failed")
        writeFooter(reason: "failure", player: player, durationSeconds: durationSeconds)
    }

    func recordSessionFinished(player: AVPlayer, durationSeconds: TimeInterval) {
        writeEvent("session_finished", player: player)
        writeFooter(reason: "session_finished", player: player, durationSeconds: durationSeconds)
    }

    func recordSampleIfNeeded(player: AVPlayer, durationSeconds: TimeInterval) {
        guard !didWriteFooter else {
            return
        }
        let currentUptime = uptimeProvider()
        if let lastSampleUptime,
           currentUptime - lastSampleUptime < Self.sampleIntervalSeconds
        {
            return
        }
        lastSampleUptime = currentUptime
        recordSample(player: player, durationSeconds: durationSeconds)
    }

    func recordSample(player: AVPlayer, durationSeconds: TimeInterval) {
        guard !didWriteFooter else {
            return
        }
        let item = player.currentItem
        writeLine([
            "schema_version": .number(1),
            "kind": .string("sample"),
            "captured_at": .string(timestamp()),
            "elapsed_seconds": .number(elapsedSeconds()),
            "thermal_state": .string(Self.thermalStateCategory(thermalStateProvider())),
            "physical_footprint_bytes": footprintProvider().map { .integer($0) } ?? .null,
            "player_time_seconds": .number(Self.playerTimeSeconds(for: player)),
            "duration_seconds": .number(Self.safeSeconds(durationSeconds)),
            "rate": .number(max(0, Double(player.rate))),
            "time_control_status": .string(Self.timeControlStatusCategory(player.timeControlStatus)),
            "waiting_reason": .string(Self.waitingReasonCategory(player.reasonForWaitingToPlay)),
            "item_status": .string(Self.itemStatusCategory(item?.status)),
            "likely_to_keep_up": item.map { .bool($0.isPlaybackLikelyToKeepUp) } ?? .null,
            "item_error_category": .string(Self.itemErrorCategory(item?.error))
        ])
    }

    static func safeIdentifier(_ value: String) -> String? {
        let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
        let sanitized = value.unicodeScalars.map { allowed.contains($0) ? Character($0) : "_" }
        let result = String(sanitized)
            .trimmingCharacters(in: CharacterSet(charactersIn: "._-"))
        guard !result.isEmpty, result.count <= 96 else {
            return nil
        }
        return result
    }

    static func timeControlStatusCategory(_ status: AVPlayer.TimeControlStatus) -> String {
        switch status {
        case .paused:
            return "paused"
        case .waitingToPlayAtSpecifiedRate:
            return "waiting"
        case .playing:
            return "playing"
        @unknown default:
            return "unknown"
        }
    }

    static func waitingReasonCategory(_ reason: AVPlayer.WaitingReason?) -> String {
        guard let reason else {
            return "none"
        }
        switch reason {
        case .evaluatingBufferingRate:
            return "evaluating_buffering_rate"
        case .toMinimizeStalls:
            return "to_minimize_stalls"
        case .noItemToPlay:
            return "no_item"
        default:
            return "other"
        }
    }

    static func itemStatusCategory(_ status: AVPlayerItem.Status?) -> String {
        guard let status else {
            return "unknown"
        }
        switch status {
        case .unknown:
            return "unknown"
        case .readyToPlay:
            return "ready"
        case .failed:
            return "failed"
        @unknown default:
            return "unknown"
        }
    }

    static func itemErrorCategory(_ error: Error?) -> String {
        guard let error else {
            return "none"
        }
        let nsError = error as NSError
        if nsError.domain == NSURLErrorDomain {
            return "network"
        }
        if nsError.domain == AVFoundationErrorDomain {
            return "decoder"
        }
        if nsError.domain == NSOSStatusErrorDomain {
            return "media_services"
        }
        return "unknown"
    }

    static func thermalStateCategory(_ state: ProcessInfo.ThermalState) -> String {
        switch state {
        case .nominal:
            return "nominal"
        case .fair:
            return "fair"
        case .serious:
            return "serious"
        case .critical:
            return "critical"
        @unknown default:
            return "unknown"
        }
    }

    private func writeHeader() {
        writeLine([
            "schema_version": .number(1),
            "kind": .string("header"),
            "captured_at": .string(timestamp()),
            "elapsed_seconds": .number(0),
            "run_id": .string(runID),
            "media_id": .string(mediaID),
            "sample_interval_seconds": .integer(UInt64(Self.sampleIntervalSeconds)),
            "app": .object([
                "bundle_id": .string(bundle.bundleIdentifier ?? "unknown"),
                "version": .string(bundle.infoDictionary?["CFBundleShortVersionString"] as? String ?? "unknown"),
                "build": .string(bundle.infoDictionary?["CFBundleVersion"] as? String ?? "unknown")
            ]),
            "device": .object([
                "hardware_model": .string(hardwareModelProvider()),
                "operating_system": .string(ProcessInfo.processInfo.operatingSystemVersionString)
            ])
        ])
    }

    private func writeEvent(_ event: String, player: AVPlayer, detail: String? = nil) {
        guard !didWriteFooter else {
            return
        }
        writeLine([
            "schema_version": .number(1),
            "kind": .string("event"),
            "captured_at": .string(timestamp()),
            "elapsed_seconds": .number(elapsedSeconds()),
            "event": .string(event),
            "player_time_seconds": .number(Self.playerTimeSeconds(for: player)),
            "detail": detail.map(JSONValue.string) ?? .null
        ])
    }

    private func writeFooter(reason: String, player: AVPlayer, durationSeconds: TimeInterval) {
        guard !didWriteFooter else {
            return
        }
        didWriteFooter = true
        writeLine([
            "schema_version": .number(1),
            "kind": .string("footer"),
            "captured_at": .string(timestamp()),
            "elapsed_seconds": .number(elapsedSeconds()),
            "reason": .string(reason),
            "final_player_time_seconds": .number(Self.playerTimeSeconds(for: player)),
            "duration_seconds": .number(Self.safeSeconds(durationSeconds))
        ], durable: true)
    }

    @discardableResult
    private func writeLine(
        _ payload: [String: JSONValue],
        durable: Bool = false
    ) -> Bool {
        guard let data = Self.encodedLine(payload) else {
            return false
        }
        do {
            try handle.write(contentsOf: data)
            try handle.write(contentsOf: Data([0x0A]))
            if durable {
                try handle.synchronize()
            }
            return true
        } catch {
            return false
        }
    }

    private func writeEvent(
        _ event: String,
        playerTimeSeconds: TimeInterval,
        detail: String?,
        capturedAt: Date,
        capturedUptime: TimeInterval
    ) {
        guard !didWriteFooter else {
            return
        }
        writeLine([
            "schema_version": .number(1),
            "kind": .string("event"),
            "captured_at": .string(timestamp(capturedAt)),
            "elapsed_seconds": .number(elapsedSeconds(capturedUptime)),
            "event": .string(event),
            "player_time_seconds": .number(Self.safeSeconds(playerTimeSeconds)),
            "detail": detail.map(JSONValue.string) ?? .null
        ])
    }

    private func timestamp() -> String {
        timestamp(dateProvider())
    }

    private func timestamp(_ date: Date) -> String {
        Self.iso8601.string(from: date)
    }

    private func elapsedSeconds() -> TimeInterval {
        elapsedSeconds(uptimeProvider())
    }

    private func elapsedSeconds(_ uptime: TimeInterval) -> TimeInterval {
        max(0, uptime - startUptime)
    }

    private static func playerTimeSeconds(for player: AVPlayer) -> TimeInterval {
        safeSeconds(player.currentTime().seconds)
    }

    private static func safeSeconds(_ value: TimeInterval) -> TimeInterval {
        value.isFinite ? max(0, value) : 0
    }

    private static func encodedLine(_ payload: [String: JSONValue]) -> Data? {
        try? JSONSerialization.data(
            withJSONObject: payload.mapValues { $0.jsonObject },
            options: [.sortedKeys]
        )
    }

    private static func physFootprintBytes() -> UInt64? {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(
            MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size
        )
        let result = withUnsafeMutablePointer(to: &info) { infoPointer in
            infoPointer.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { reboundPointer in
                task_info(
                    mach_task_self_,
                    task_flavor_t(TASK_VM_INFO),
                    reboundPointer,
                    &count
                )
            }
        }
        return result == KERN_SUCCESS ? info.phys_footprint : nil
    }

    private static func hardwareModel() -> String {
        var size: size_t = 0
        guard sysctlbyname("hw.machine", nil, &size, nil, 0) == 0, size > 0 else {
            return "unknown"
        }
        var buffer = [CChar](repeating: 0, count: size)
        guard sysctlbyname("hw.machine", &buffer, &size, nil, 0) == 0 else {
            return "unknown"
        }
        return String(cString: buffer)
    }

    private static let iso8601: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
}

private enum JSONValue {
    case string(String)
    case number(Double)
    case integer(UInt64)
    case bool(Bool)
    case object([String: JSONValue])
    case null

    var jsonObject: Any {
        switch self {
        case let .string(value):
            return value
        case let .number(value):
            return value
        case let .integer(value):
            return value
        case let .bool(value):
            return value
        case let .object(value):
            return value.mapValues { $0.jsonObject }
        case .null:
            return NSNull()
        }
    }
}
#endif

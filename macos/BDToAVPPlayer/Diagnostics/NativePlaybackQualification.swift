#if BD_TO_AVP_QUALIFICATION
import AVFoundation
import Darwin
import Foundation
import RealityKit

/// Opt-in on-device run of native Blu-ray 3D playback. Launch with BD_TO_AVP_NATIVE_PLAY_ITEM set
/// to a library item's file name. It plays the item through the normal player for
/// BD_TO_AVP_NATIVE_PLAY_SECONDS (default 660), pauses at BD_TO_AVP_NATIVE_PAUSE_AT seconds for
/// BD_TO_AVP_NATIVE_PAUSE_SECONDS, switches to the next audio track at
/// BD_TO_AVP_NATIVE_AUDIO_SWITCH_AT, and seeks 10 seconds back at BD_TO_AVP_NATIVE_SEEK_BACK_AT.
/// BD_TO_AVP_NATIVE_SEEKS lists seeks as `second:target` pairs, such as `10:1200,30:5620`; each
/// records how long playback took to run again at its target.
/// It prints NATIVE_PLAY lines and writes `native-playback-<file name>.json` to Documents with a
/// sample per second: position, buffering, frames, CPU, thermal state and memory.
@MainActor
enum NativePlaybackQualification {
    struct Sample: Codable {
        let second: Int
        let position: Double
        let isPlaying: Bool
        let isBuffering: Bool
        let secondsAhead: Double
        let framesQueued: UInt64
        let lateFrames: UInt64
        let stalls: Int
        let audioLeadSeconds: Double
        let cpuPercentOfOneCore: Double
        let thermalState: String
        let footprintMegabytes: Double
    }

    struct Report: Codable {
        var fileName = ""
        var device = ""
        var system = ""
        var decoderVersion = MVCDecoder.version
        var startSeconds: Double?
        var events: [String] = []
        var samples: [Sample] = []
        var rendererTotalFrames: Int?
        var rendererDroppedFrames: Int?
        var rendererCorruptedFrames: Int?
        var framesWithoutDependentView: UInt64 = 0
        var corruptUnits: UInt64 = 0
        var audioError: String?
        var peakFootprintMegabytes: Double = 0
        var averageCPUPercentOfOneCore: Double = 0
        var thermalStatesSeen: [String] = []
        /// The window and the video as drawn, in scene meters; the controls hang below the window.
        var windowSize: [Float]?
        var videoSize: [Float]?
        var viewingMode: String?
        var renderingStatus: String?
        var failure: String?
    }

    static func runIfRequested(model: PlayerAppModel, player: MVHEVCPlayerSession) async {
        let environment = ProcessInfo.processInfo.environment
        guard let fileName = environment["BD_TO_AVP_NATIVE_PLAY_ITEM"], !fileName.isEmpty else { return }
        func number(_ key: String, _ fallback: Double) -> Double { environment[key].flatMap(Double.init) ?? fallback }
        let runSeconds = Int(number("BD_TO_AVP_NATIVE_PLAY_SECONDS", 660))
        let pauseAt = Int(number("BD_TO_AVP_NATIVE_PAUSE_AT", 120))
        let pauseSeconds = Int(number("BD_TO_AVP_NATIVE_PAUSE_SECONDS", 10))
        let audioSwitchAt = Int(number("BD_TO_AVP_NATIVE_AUDIO_SWITCH_AT", 240))
        let seekBackAt = Int(number("BD_TO_AVP_NATIVE_SEEK_BACK_AT", 300))
        var seeks: [Int: Double] = [:]
        for pair in (environment["BD_TO_AVP_NATIVE_SEEKS"] ?? "").split(separator: ",") {
            let parts = pair.split(separator: ":")
            if parts.count == 2, let second = Int(parts[0]), let target = Double(parts[1]) { seeks[second] = target }
        }

        var report = Report(fileName: fileName, device: sysctlString("hw.machine"), system: ProcessInfo.processInfo.operatingSystemVersionString)
        guard let item = model.library.items.first(where: { $0.fileName == fileName }) else {
            emit("item_missing file=\(fileName)")
            return
        }
        let launched = ContinuousClock.now
        model.requestPlayback(for: item.id)
        var engine: NativeBlurayPlayback?
        for _ in 0..<600 where engine == nil {
            try? await Task.sleep(nanoseconds: 100_000_000)
            engine = player.qualificationNativePlayback
            if player.state == .failed { break }
        }
        guard let engine else {
            report.failure = "no native playback; state \(player.state) \(player.failureMessage ?? "")"
            emit("failed \(report.failure ?? "")")
            write(report)
            return
        }
        emit("engine title=\(engine.title.id) audio=\(engine.audioTracks.map(\.label))")

        var lastCPU = cpuSeconds()
        var lastWall = ContinuousClock.now
        var resumeAt: Int?
        var thermal = Set<String>()
        var cpuTotal = 0.0
        for second in 1...max(1, runSeconds) {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            if player.state == .failed {
                report.failure = player.failureMessage
                emit("failed \(player.failureMessage ?? "")")
                break
            }
            let status = engine.status
            if report.startSeconds == nil, !status.isBuffering {
                report.startSeconds = elapsed(launched)
                report.events.append("started t=\(report.startSeconds ?? 0)")
            }
            if second == pauseAt {
                report.events.append("pause position=\(status.currentTime)")
                player.pause()
                resumeAt = second + pauseSeconds
            }
            if second == resumeAt {
                report.events.append("resume position=\(status.currentTime)")
                player.play()
            }
            if second == audioSwitchAt, engine.audioTracks.count > 1,
               let current = engine.audioTracks.firstIndex(where: { $0.id == engine.selectedAudioTrackID }) {
                let next = engine.audioTracks[(current + 1) % engine.audioTracks.count]
                report.events.append("audio_switch to=\(next.label) position=\(status.currentTime)")
                player.selectAudio(id: next.mediaOption.id)
            }
            if let target = seeks[second] {
                let issued = ContinuousClock.now
                player.seek(to: target)
                var ready: Double?
                for _ in 0..<300 {
                    try? await Task.sleep(nanoseconds: 50_000_000)
                    let now = engine.status
                    if !now.isBuffering, now.currentTime >= target - 0.05 { ready = elapsed(issued); break }
                }
                let event = "seek_to target=\(target) ready_after=\(ready.map { String(format: "%.2f", $0) } ?? "none") "
                    + "position=\(String(format: "%.2f", engine.status.currentTime))"
                report.events.append(event)
                emit(event)
            }
            if second == seekBackAt {
                report.events.append("seek_back from=\(status.currentTime) to=\(status.currentTime - 10)")
                player.seek(to: status.currentTime - 10)
            }

            let cpu = cpuSeconds()
            let now = ContinuousClock.now
            let cpuPercent = (cpu - lastCPU) / max(0.001, seconds(lastWall.duration(to: now))) * 100
            lastCPU = cpu
            lastWall = now
            cpuTotal += cpuPercent
            let thermalState = thermalName(ProcessInfo.processInfo.thermalState)
            thermal.insert(thermalState)
            let footprint = memoryFootprint()
            report.peakFootprintMegabytes = max(report.peakFootprintMegabytes, footprint.peak)
            let sample = Sample(
                second: second,
                position: status.currentTime,
                isPlaying: status.wantsPlayback,
                isBuffering: status.isBuffering,
                secondsAhead: status.statistics.secondsAhead,
                framesQueued: status.statistics.framesQueued,
                lateFrames: status.statistics.lateFrames,
                stalls: status.statistics.stalls,
                audioLeadSeconds: engine.audioPlayer?.queuedLeadSeconds ?? 0,
                cpuPercentOfOneCore: cpuPercent,
                thermalState: thermalState,
                footprintMegabytes: footprint.current
            )
            report.samples.append(sample)
            report.framesWithoutDependentView = status.statistics.framesWithoutDependentView
            report.corruptUnits = status.statistics.corruptUnits
            report.audioError = status.audioFailure ?? engine.audioPlayer?.lastError
            report.averageCPUPercentOfOneCore = cpuTotal / Double(second)
            report.thermalStatesSeen = thermal.sorted()
            emit(
                "sample second=\(second) position=\(String(format: "%.2f", status.currentTime)) buffering=\(status.isBuffering) "
                    + "ahead=\(String(format: "%.1f", status.statistics.secondsAhead)) frames=\(status.statistics.framesQueued) "
                    + "late=\(status.statistics.lateFrames) stalls=\(status.statistics.stalls) "
                    + "cpu=\(Int(cpuPercent)) thermal=\(thermalState) mb=\(Int(footprint.current))"
            )
            if second % 10 == 0 || status.hasEnded {
                recordLayout(player, into: &report)
                await loadRendererMetrics(engine, into: &report)
                write(report)
            }
            if status.hasEnded {
                report.events.append("ended position=\(status.currentTime)")
                break
            }
        }
        recordLayout(player, into: &report)
        await loadRendererMetrics(engine, into: &report)
        write(report)
        emit(
            "layout window=\(report.windowSize ?? []) video=\(report.videoSize ?? []) "
                + "viewing=\(report.viewingMode ?? "none") rendering=\(report.renderingStatus ?? "none")"
        )
        emit(
            "complete dropped=\(report.rendererDroppedFrames ?? -1) total=\(report.rendererTotalFrames ?? -1) "
                + "late=\(report.samples.last?.lateFrames ?? 0) stalls=\(report.samples.last?.stalls ?? 0) "
                + "cpu_avg=\(Int(report.averageCPUPercentOfOneCore)) peak_mb=\(Int(report.peakFootprintMegabytes)) "
                + "thermal=\(report.thermalStatesSeen) failure=\(report.failure ?? "none")"
        )
    }

    private static func recordLayout(_ player: MVHEVCPlayerSession, into report: inout Report) {
        report.windowSize = PlayerLayoutProbe.windowSize.map { [$0.x, $0.y] }
        report.videoSize = PlayerLayoutProbe.videoSize.map { [$0.x, $0.y] }
        if let component = player.playerEntity.components[VideoPlayerComponent.self] {
            report.viewingMode = component.viewingMode.map { "\($0)" }
            report.renderingStatus = "\(component.currentRenderingStatus)"
        }
    }

    private static func loadRendererMetrics(_ engine: NativeBlurayPlayback, into report: inout Report) async {
        let renderer = engine.renderer
        let metrics: (Int, Int, Int)? = await withCheckedContinuation { continuation in
            renderer.loadVideoPerformanceMetrics { metrics in
                continuation.resume(returning: metrics.map {
                    ($0.totalNumberOfFrames, $0.numberOfDroppedFrames, $0.numberOfCorruptedFrames)
                })
            }
        }
        if let metrics {
            report.rendererTotalFrames = metrics.0
            report.rendererDroppedFrames = metrics.1
            report.rendererCorruptedFrames = metrics.2
        }
    }

    private static func write(_ report: Report) {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        try? encoder.encode(report).write(to: documents.appendingPathComponent("native-playback-\(report.fileName).json"))
    }

    private static func elapsed(_ start: ContinuousClock.Instant) -> Double {
        seconds(start.duration(to: .now))
    }

    private static func seconds(_ duration: Duration) -> Double {
        Double(duration.components.seconds) + Double(duration.components.attoseconds) / 1e18
    }

    private static func cpuSeconds() -> Double {
        var usage = rusage()
        getrusage(RUSAGE_SELF, &usage)
        func value(_ time: timeval) -> Double { Double(time.tv_sec) + Double(time.tv_usec) / 1e6 }
        return value(usage.ru_utime) + value(usage.ru_stime)
    }

    private static func memoryFootprint() -> (current: Double, peak: Double) {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
        let result = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
            }
        }
        guard result == KERN_SUCCESS else { return (0, 0) }
        return (Double(info.phys_footprint) / 1_048_576, Double(info.ledger_phys_footprint_peak) / 1_048_576)
    }

    private static func thermalName(_ state: ProcessInfo.ThermalState) -> String {
        switch state {
        case .nominal: "nominal"
        case .fair: "fair"
        case .serious: "serious"
        case .critical: "critical"
        @unknown default: "unknown"
        }
    }

    private static func sysctlString(_ name: String) -> String {
        var size = 0
        sysctlbyname(name, nil, &size, nil, 0)
        guard size > 0 else { return "" }
        var buffer = [CChar](repeating: 0, count: size)
        sysctlbyname(name, &buffer, &size, nil, 0)
        return String(cString: buffer)
    }

    private static func emit(_ message: String) {
        FileHandle.standardOutput.write(Data("NATIVE_PLAY \(message)\n".utf8))
    }
}
#endif

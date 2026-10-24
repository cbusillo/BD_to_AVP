// Throwaway spike for #1101: measures edge264-mvc software decode of a real
// Blu-ray 3D MVC clip on the device and writes Documents/mvc-spike-result.json.
import Darwin
import Foundation
import SwiftUI

@main
struct MVCSpikeApp: App {
    @State private var runner = BenchRunner.shared

    var body: some Scene {
        WindowGroup {
            VStack(alignment: .leading, spacing: 12) {
                Text("MVC decode spike #1101").font(.title)
                Text(runner.status).font(.body.monospaced())
            }
            .padding(40)
            .onAppear { runner.startOnce() }
        }
    }
}

@Observable
final class BenchRunner: @unchecked Sendable {
    static let shared = BenchRunner()
    var status = "starting"
    private var started = false
    private let progress = UnsafeMutablePointer<Int64>.allocate(capacity: 1)

    init() {
        progress.initialize(to: 0)
        startOnce()
    }

    func startOnce() {
        guard !started else { return }
        started = true
        let thread = Thread { self.run() }
        thread.qualityOfService = .userInteractive
        thread.stackSize = 8 << 20
        thread.start()
    }

    private func setStatus(_ text: String) {
        print("MVCSPIKE: \(text)")
        DispatchQueue.main.async { self.status = text }
    }

    private func run() {
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let resultURL = documents.appendingPathComponent("mvc-spike-result.json")
        var result: [String: Any] = [
            "schema": 1,
            "issue": 1101,
            "started_at": ISO8601DateFormatter().string(from: Date()),
            "device": deviceInfo(),
            "decoder_version": String(cString: bench_decoder_version()),
            "phases": [[String: Any]](),
            "complete": false,
        ]
        func save() {
            if let data = try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]) {
                try? data.write(to: resultURL, options: .atomic)
            }
        }
        guard let clipURL = Bundle.main.url(forResource: "clip", withExtension: "264"),
              let clip = try? Data(contentsOf: clipURL, options: .alwaysMapped) else {
            result["error"] = "clip missing"
            save()
            setStatus("clip missing")
            return
        }
        result["clip_bytes"] = clip.count
        setStatus("pre-reading clip")
        let readStart = Date()
        var touch: UInt64 = 0
        clip.withUnsafeBytes { raw in
            let bytes = raw.bindMemory(to: UInt8.self)
            var i = 0
            while i < bytes.count { touch &+= UInt64(bytes[i]); i += 16384 }
        }
        result["clip_preread_seconds"] = Date().timeIntervalSince(readStart)
        result["clip_touch"] = touch
        save()

        let phases: [(String, Int32, Double, Int32)] = [
            ("unpaced_auto_threads", 0, 0, 1),
            ("unpaced_2_threads", 2, 0, 1),
            ("unpaced_single_thread", 1, 0, 1),
            ("paced_23.976_auto_threads", 0, 24000.0 / 1001.0, 2),
        ]
        for (name, threads, pace, loops) in phases {
            setStatus("cooldown before \(name)")
            Thread.sleep(forTimeInterval: 15)
            setStatus("running \(name)")
            var phase = runPhase(name: name, clip: clip, threads: threads, pace: pace, loops: loops)
            phase["name"] = name
            var list = result["phases"] as! [[String: Any]]
            list.append(phase)
            result["phases"] = list
            save()
            setStatus("finished \(name): fps \(phase["fps"] ?? "?")")
        }
        result["complete"] = true
        result["finished_at"] = ISO8601DateFormatter().string(from: Date())
        save()
        setStatus("DONE. Result in Documents/mvc-spike-result.json")
    }

    private func runPhase(name: String, clip: Data, threads: Int32, pace: Double, loops: Int32) -> [String: Any] {
        let capacity = 3000 * Int(loops) + 1000
        let times = UnsafeMutablePointer<Double>.allocate(capacity: capacity)
        defer { times.deallocate() }
        progress.pointee = 0
        var stats = BenchStats()
        var samples: [[String: Any]] = []
        let sampling = DispatchSemaphore(value: 0)
        let stop = ManagedAtomicFlag()
        let progressPointer = progress
        let sampler = Thread {
            var lastCPU = cpuSeconds()
            var lastWall = monotonic()
            var lastFrames: Int64 = 0
            let start = lastWall
            while !stop.value {
                Thread.sleep(forTimeInterval: 1.0)
                let cpu = cpuSeconds(), wall = monotonic(), frames = progressPointer.pointee
                let dt = wall - lastWall
                samples.append([
                    "t": wall - start,
                    "frames": frames,
                    "fps": Double(frames - lastFrames) / dt,
                    "cpu_percent_of_one_core": (cpu - lastCPU) / dt * 100,
                    "thermal_state": thermalName(ProcessInfo.processInfo.thermalState),
                    "footprint_mb": Double(memoryFootprint().current) / 1_048_576,
                ])
                lastCPU = cpu; lastWall = wall; lastFrames = frames
            }
            sampling.signal()
        }
        sampler.start()
        let cpuStart = cpuSeconds()
        let rc = clip.withUnsafeBytes { raw -> Int32 in
            bench_run(raw.bindMemory(to: UInt8.self).baseAddress, raw.count, threads, pace, loops, 1,
                      times, capacity, progressPointer, &stats)
        }
        let cpuUsed = cpuSeconds() - cpuStart
        stop.value = true
        sampling.wait()

        let n = Int(min(stats.frames, Int64(capacity)))
        var intervals: [Double] = []
        intervals.reserveCapacity(n)
        for i in 1..<max(n, 1) { intervals.append((times[i] - times[i - 1]) * 1000) }
        intervals.sort()
        func pct(_ p: Double) -> Double {
            guard !intervals.isEmpty else { return 0 }
            return intervals[min(intervals.count - 1, Int(Double(intervals.count - 1) * p))]
        }
        let thermalStates = Set(samples.compactMap { $0["thermal_state"] as? String })
        return [
            "threads_setting": threads,
            "pace_fps": pace,
            "loops": loops,
            "result_code": rc,
            "frames": stats.frames,
            "stereo_frames": stats.stereo_frames,
            "concealed_views": stats.concealed_views,
            "corrupt_nals": stats.corrupt_nals,
            "unsupported_nals": stats.unsupported_nals,
            "width": stats.width,
            "height": stats.height,
            "wall_seconds": stats.wall_seconds,
            "fps": Double(stats.frames) / stats.wall_seconds,
            "realtime_multiple": Double(stats.frames) / stats.wall_seconds / (24000.0 / 1001.0),
            "cpu_seconds": cpuUsed,
            "avg_cpu_percent_of_one_core": cpuUsed / stats.wall_seconds * 100,
            "late_frames": stats.late_frames,
            "max_lateness_ms": stats.max_lateness * 1000,
            "frame_interval_ms": [
                "p50": pct(0.5), "p90": pct(0.9), "p99": pct(0.99), "p999": pct(0.999),
                "max": intervals.last ?? 0, "mean": intervals.isEmpty ? 0 : intervals.reduce(0, +) / Double(intervals.count),
            ],
            "peak_footprint_mb": Double(memoryFootprint().peak) / 1_048_576,
            "thermal_states_seen": Array(thermalStates).sorted(),
            "samples": samples,
        ]
    }
}

final class ManagedAtomicFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var flag = false
    var value: Bool {
        get { lock.lock(); defer { lock.unlock() }; return flag }
        set { lock.lock(); flag = newValue; lock.unlock() }
    }
}

func monotonic() -> Double { Double(clock_gettime_nsec_np(CLOCK_UPTIME_RAW)) / 1e9 }

func cpuSeconds() -> Double {
    var usage = rusage()
    getrusage(RUSAGE_SELF, &usage)
    func s(_ t: timeval) -> Double { Double(t.tv_sec) + Double(t.tv_usec) / 1e6 }
    return s(usage.ru_utime) + s(usage.ru_stime)
}

func memoryFootprint() -> (current: UInt64, peak: UInt64) {
    var info = task_vm_info_data_t()
    var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
    let kr = withUnsafeMutablePointer(to: &info) {
        $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
            task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
        }
    }
    guard kr == KERN_SUCCESS else { return (0, 0) }
    return (info.phys_footprint, UInt64(info.ledger_phys_footprint_peak))
}

func thermalName(_ state: ProcessInfo.ThermalState) -> String {
    switch state {
    case .nominal: return "nominal"
    case .fair: return "fair"
    case .serious: return "serious"
    case .critical: return "critical"
    @unknown default: return "unknown"
    }
}

func sysctlString(_ name: String) -> String {
    var size = 0
    sysctlbyname(name, nil, &size, nil, 0)
    guard size > 0 else { return "" }
    var buffer = [CChar](repeating: 0, count: size)
    sysctlbyname(name, &buffer, &size, nil, 0)
    return String(cString: buffer)
}

func sysctlInt(_ name: String) -> Int {
    var value: Int32 = 0
    var size = MemoryLayout<Int32>.size
    sysctlbyname(name, &value, &size, nil, 0)
    return Int(value)
}

func deviceInfo() -> [String: Any] {
    [
        "hw_model": sysctlString("hw.model"),
        "hw_machine": sysctlString("hw.machine"),
        "os": ProcessInfo.processInfo.operatingSystemVersionString,
        "active_processors": ProcessInfo.processInfo.activeProcessorCount,
        "perflevel0_cores": sysctlInt("hw.perflevel0.logicalcpu"),
        "perflevel1_cores": sysctlInt("hw.perflevel1.logicalcpu"),
        "physical_memory_gb": Double(ProcessInfo.processInfo.physicalMemory) / 1_073_741_824,
        "low_power_mode": ProcessInfo.processInfo.isLowPowerModeEnabled,
    ]
}

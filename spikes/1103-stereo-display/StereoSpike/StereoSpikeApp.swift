// Throwaway spike for #1103: show Blu-ray 3D frames as stereo video in a
// visionOS window, three ways, switchable in place:
//   A  edge264-mvc decode -> NV12 CVPixelBuffer per eye, tagged
//      .stereoView(.leftEye/.rightEye) -> AVSampleBufferVideoRenderer ->
//      RealityKit VideoPlayerComponent(videoRenderer:). No AVPlayer, no encode.
//   B  AVPlayer -> VideoPlayerComponent(avPlayer:) playing an MV-HEVC encode
//      of the same clip, made on the Mac with the repo's mv-hevc-encoder.
//   C  A, plus window.avDisplayManager.preferredDisplayCriteria at 23.976 Hz.
// Logs per-frame present timing, display callback rates, CPU, thermal state
// and memory per mode to Documents/stereo-spike-*.json.
// Modes step A, B, C, A... on their own every 60 s (launch argument
// `-autocycle SECONDS` changes it, 0 turns it off); a tap restarts the timer.
// Each mode's letter is drawn into the video itself, top right, in both eyes.
// The controls live in an ornament hanging fully below the window, so no part
// of the stereo video can draw over them.
import AVFoundation
import AVKit
import CoreMedia
import CoreVideo
import Darwin
import Foundation
import QuartzCore
import RealityKit
import SwiftUI
import UIKit

enum Mode: String, CaseIterable, Identifiable {
    case a = "A", b = "B", c = "C"
    var id: String { rawValue }
    var label: String {
        switch self {
        case .a: return "A  custom (edge264)"
        case .b: return "B  AVPlayer MV-HEVC"
        case .c: return "C  custom + 24p hint"
        }
    }
}

@main
struct StereoSpikeApp: App {
    var body: some SwiftUI.Scene {
        WindowGroup {
            PlayerView()
        }
        .defaultSize(width: 1600, height: 900)
    }
}

struct PlayerView: View {
    @State private var status = "starting"
    @State private var mode: Mode = .a
    @State private var meshSizes: [String: SIMD2<Float>] = [:]
    @State private var window: UIWindow?
    @Environment(\.physicalMetrics) private var metrics
    private let player = StereoPlayer.shared
    private let timer = Timer.publish(every: 0.5, on: .main, in: .common).autoconnect()

    private func makeEntity(_ name: String, _ component: VideoPlayerComponent, _ content: RealityViewContent) -> Entity {
        let entity = Entity()
        var component = component
        component.desiredViewingMode = .stereo
        entity.components.set(component)
        entity.name = name
        content.add(entity)
        _ = content.subscribe(to: VideoPlayerEvents.ContentTypeDidChange.self, on: entity) { event in
            player.note("\(name).content_type", "\(event.previousType) -> \(event.contentType)")
        }
        _ = content.subscribe(to: VideoPlayerEvents.ViewingModeDidChange.self, on: entity) { event in
            player.note("\(name).viewing_mode", "\(String(describing: event.previousViewingMode)) -> \(String(describing: event.currentViewingMode))")
        }
        _ = content.subscribe(to: VideoPlayerEvents.RenderingStatusDidChange.self, on: entity) { event in
            player.note("\(name).rendering_status", "\(event.previousStatus) -> \(event.currentStatus)")
        }
        _ = content.subscribe(to: VideoPlayerEvents.VideoSizeDidChange.self, on: entity) { event in
            player.note("\(name).video_size", "mesh \(event.screenMeshSize) video \(event.videoDimension)")
            meshSizes[name] = event.screenMeshSize
        }
        return entity
    }

    var body: some View {
        GeometryReader3D { proxy in
            RealityView { content in
                _ = makeEntity("custom", VideoPlayerComponent(videoRenderer: player.renderer), content)
                let avEntity = makeEntity("avplayer", VideoPlayerComponent(avPlayer: player.avPlayer), content)
                avEntity.isEnabled = false
                _ = content.subscribe(to: SceneEvents.Update.self) { event in
                    player.renderTick(event.deltaTime)
                }
                player.start()
            } update: { content in
                for entity in content.entities {
                    entity.isEnabled = (entity.name == "avplayer") == (mode == .b)
                    if entity.isEnabled, let component = entity.components[VideoPlayerComponent.self] {
                        player.setComponentState(entity.name, viewingMode: String(describing: component.viewingMode),
                                                 rendering: String(describing: component.currentRenderingStatus))
                    }
                    guard let mesh = meshSizes[entity.name], mesh.x > 0 else { continue }
                    // The view's bounds in scene meters; keep the video well inside them so
                    // it never reaches the ornament below the window.
                    let bounds = content.convert(proxy.frame(in: .local), from: .local, to: .scene)
                    let width = (bounds.max.x - bounds.min.x) * 0.75
                    let height = (bounds.max.y - bounds.min.y) * 0.75
                    let scale = min(width / mesh.x, height / mesh.y)
                    entity.scale = SIMD3(repeating: scale)
                    entity.position = SIMD3(bounds.center.x, bounds.center.y, bounds.min.z)
                    player.setLayout(entity.name, bounds: bounds, scale: scale,
                                     pointsMeters: Float(metrics.convert(proxy.size.width, to: .meters)))
                }
            }
        }
        .background(WindowReader { window = $0 })
        .ornament(attachmentAnchor: .scene(.bottom), contentAlignment: .top) {
            VStack(spacing: 14) {
                HStack(spacing: 20) {
                    ForEach(Mode.allCases) { m in
                        Button { mode = m } label: {
                            Text(m.label).font(.title).padding(.horizontal, 16).padding(.vertical, 10)
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(m == mode ? .blue : .gray.opacity(0.35))
                        .controlSize(.extraLarge)
                    }
                }
                Text(status).font(.body.monospaced())
            }
            .padding(20)
            .glassBackgroundEffect()
        }
        .onChange(of: mode) { _, newMode in player.setMode(newMode, window: window) }
        .onReceive(timer) { _ in
            status = player.statusLine()
            if let next = player.autoCycleDue() { mode = next }
        }
    }
}

struct WindowReader: UIViewRepresentable {
    let onWindow: (UIWindow) -> Void
    final class Probe: UIView {
        var onWindow: ((UIWindow) -> Void)?
        override func didMoveToWindow() {
            super.didMoveToWindow()
            if let window { onWindow?(window) }
        }
    }
    func makeUIView(context: Context) -> Probe {
        let view = Probe()
        view.onWindow = onWindow
        return view
    }
    func updateUIView(_ view: Probe, context: Context) {}
}

final class DisplayLinkTarget: NSObject {
    let tick: (CADisplayLink) -> Void
    init(_ tick: @escaping (CADisplayLink) -> Void) { self.tick = tick }
    @objc func fire(_ link: CADisplayLink) { tick(link) }
}

/// Per-mode measurement buckets.
final class ModeStats {
    var displayedIntervalsMs: [Double] = []
    var displayedFrames = 0
    var skippedFrames = 0
    var clockMinusPtsMs: [Double] = []
    var displayLinkIntervalsMs: [Double] = []
    var displayLinkDurationMs: [Double] = []
    var renderDeltaMs: [Double] = []
    var cpu: [Double] = []
    var seconds = 0.0
}

final class StereoPlayer: @unchecked Sendable {
    static let shared = StereoPlayer()
    static let fps = 24000.0 / 1001.0
    static let maxLead = 0.25 // seconds of video queued ahead of the clock
    static let prerollFrames = 4 // the renderer stops accepting after 6 with the clock stopped
    static let capacity = 60 * 60 * 24 // one hour of frames

    let renderer = AVSampleBufferVideoRenderer()
    let synchronizer = AVSampleBufferRenderSynchronizer()
    let avPlayer = AVQueuePlayer()
    private var looper: AVPlayerLooper?
    private let lock = NSLock()
    private var started = false
    private var clockStarted = false
    private var pool: CVPixelBufferPool?
    private var formatDescription: CMTaggedBufferGroupFormatDescription?
    private var eyeFormatDescription: CMVideoFormatDescription?
    private var clip = Data()
    private var stats = DecodeStats()
    private let launch = monotonic()
    private var startHost = 0.0
    private let resultURL: URL

    private var mode: Mode = .a
    private var segment = 0 // increments on every mode switch
    private var modeSince = monotonic()
    private var buckets: [Mode: ModeStats] = [.a: ModeStats(), .b: ModeStats(), .c: ModeStats()]
    private var modeLog: [[String: Any]] = []
    private var autoCycle: Double?
    private var nextCycle = 0.0

    private var enqueueLeadMs = [Float](repeating: .nan, count: capacity)
    private var prepareMs = [Float](repeating: .nan, count: capacity)
    private var enqueued = 0
    private var lateEnqueues = 0
    private var poolFailures = 0

    private var lastDisplayed: (frame: Int, host: Double, segment: Int)?
    private var displayPolls = 0
    private var displayUnknown = 0
    private var displayedLeftEye = 0
    private var displayedRightEye = 0

    private var displayLink: CADisplayLink?
    private var displayLinkTarget: DisplayLinkTarget?
    private var lastLinkTimestamp: (Double, Int)?
    private var linkTicksThisSecond = 0
    private var renderTicksThisSecond = 0

    private var samples: [[String: Any]] = []
    private var events: [[String: Any]] = []
    private var metrics: [String: Any] = [:]
    private var componentState: [String: Any] = [:]
    private var layout: [String: Any] = [:]
    private var thermalSeen = Set<String>()
    private var criteriaMatchingEnabled: Bool?

    init() {
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let stamp = ISO8601DateFormatter().string(from: Date()).replacingOccurrences(of: ":", with: "-")
        resultURL = documents.appendingPathComponent("stereo-spike-\(stamp).json")
        synchronizer.addRenderer(renderer)
        NotificationCenter.default.addObserver(forName: AVSampleBufferVideoRenderer.didFailToDecodeNotification, object: renderer,
                                               queue: nil) { [weak self] note in
            let error = note.userInfo?[AVSampleBufferVideoRenderer.didFailToDecodeNotificationErrorKey]
            self?.note("renderer_failed_to_decode", String(describing: error))
        }
        if let url = Bundle.main.url(forResource: "clip_mvhevc_labeled", withExtension: "mov") {
            looper = AVPlayerLooper(player: avPlayer, templateItem: AVPlayerItem(url: url))
        }
        let args = ProcessInfo.processInfo.arguments
        var cycle = 60.0
        if let i = args.firstIndex(of: "-autocycle"), i + 1 < args.count, let s = Double(args[i + 1]) { cycle = s }
        if cycle > 0 {
            autoCycle = cycle
            nextCycle = monotonic() + cycle
        }
    }

    func note(_ kind: String, _ detail: String) {
        print("STEREOSPIKE: \(kind) \(detail)")
        lock.lock()
        events.append(["t": monotonic() - launch, "kind": kind, "detail": detail])
        lock.unlock()
    }

    func setComponentState(_ entity: String, viewingMode: String, rendering: String) {
        lock.lock()
        componentState[entity] = ["viewing_mode": viewingMode, "rendering_status": rendering]
        lock.unlock()
    }

    func setLayout(_ entity: String, bounds: BoundingBox, scale: Float, pointsMeters: Float) {
        lock.lock()
        layout[entity] = ["bounds_min": [bounds.min.x, bounds.min.y, bounds.min.z],
                          "bounds_max": [bounds.max.x, bounds.max.y, bounds.max.z],
                          "scale": scale, "view_width_m_from_metrics": pointsMeters]
        lock.unlock()
    }

    func autoCycleDue() -> Mode? {
        guard let autoCycle, monotonic() >= nextCycle else { return nil }
        nextCycle = monotonic() + autoCycle
        let all = Mode.allCases
        return all[(all.firstIndex(of: mode)! + 1) % all.count]
    }

    // MARK: modes

    /// Main thread.
    func setMode(_ newMode: Mode, window: UIWindow?) {
        lock.lock()
        let old = mode
        buckets[old]!.seconds += monotonic() - modeSince
        mode = newMode
        modeSince = monotonic()
        if let autoCycle { nextCycle = modeSince + autoCycle }
        segment += 1
        lastLinkTimestamp = nil
        let fd = eyeFormatDescription
        lock.unlock()

        if newMode == .b {
            synchronizer.rate = 0
            avPlayer.play()
        } else {
            avPlayer.pause()
            if clockStarted { synchronizer.rate = 1 }
        }
        var criteria = "nil"
        // The simulator's UIWindow has no avDisplayManager and throws on the selector.
        if let window, window.responds(to: Selector(("avDisplayManager"))) {
            let manager = window.avDisplayManager
            criteriaMatchingEnabled = manager.isDisplayCriteriaMatchingEnabled
            if newMode == .c, let fd {
                manager.preferredDisplayCriteria = AVDisplayCriteria(refreshRate: Float(Self.fps), formatDescription: fd)
                criteria = "refreshRate \(Self.fps)"
            } else {
                manager.preferredDisplayCriteria = nil
            }
        } else {
            criteria = "no window"
        }
        // Second hint for C: ask the app's display link for 96 Hz.
        displayLink?.preferredFrameRateRange = newMode == .c
            ? CAFrameRateRange(minimum: 96, maximum: 96, preferred: 96) : .default
        criteria += newMode == .c ? ", display link 96 Hz" : ""
        lock.lock()
        modeLog.append(["t": monotonic() - launch, "from": old.rawValue, "to": newMode.rawValue, "display_criteria": criteria,
                        "criteria_matching_enabled": criteriaMatchingEnabled.map { $0 as Any } ?? NSNull()])
        lock.unlock()
        note("mode", "\(old.rawValue) -> \(newMode.rawValue), criteria \(criteria), matching enabled \(String(describing: criteriaMatchingEnabled))")
    }

    // MARK: refresh observation

    /// RealityKit scene update, main thread.
    func renderTick(_ delta: TimeInterval) {
        lock.lock()
        buckets[mode]!.renderDeltaMs.append(delta * 1000)
        renderTicksThisSecond += 1
        lock.unlock()
    }

    private func startDisplayLink() {
        DispatchQueue.main.async {
            let target = DisplayLinkTarget { [weak self] link in self?.linkTick(link) }
            let link = CADisplayLink(target: target, selector: #selector(DisplayLinkTarget.fire(_:)))
            link.add(to: .main, forMode: .common)
            self.displayLink = link
            self.displayLinkTarget = target
        }
    }

    private func linkTick(_ link: CADisplayLink) {
        lock.lock()
        let bucket = buckets[mode]!
        if let (last, seg) = lastLinkTimestamp, seg == segment {
            bucket.displayLinkIntervalsMs.append((link.timestamp - last) * 1000)
        }
        bucket.displayLinkDurationMs.append((link.targetTimestamp - link.timestamp) * 1000)
        lastLinkTimestamp = (link.timestamp, segment)
        linkTicksThisSecond += 1
        lock.unlock()
    }

    func start() {
        lock.lock()
        defer { lock.unlock() }
        guard !started else { return }
        started = true
        for (name, body) in [("decode", run), ("display-poll", pollDisplay), ("sampler", sample)] as [(String, () -> Void)] {
            let thread = Thread(block: body)
            thread.name = name
            thread.qualityOfService = .userInteractive
            thread.stackSize = 8 << 20
            thread.start()
        }
        startDisplayLink()
        if looper == nil { events.append(["t": 0, "kind": "error", "detail": "clip_mvhevc.mov missing"]) }
    }

    // MARK: decode and present (modes A and C)

    private func run() {
        guard let url = Bundle.main.url(forResource: "clip", withExtension: "264"),
              let data = try? Data(contentsOf: url, options: .alwaysMapped) else {
            note("error", "clip missing")
            return
        }
        clip = data
        note("decoder", String(cString: spike_decoder_version()))
        let context = Unmanaged.passUnretained(self).toOpaque()
        let rc = clip.withUnsafeBytes { raw -> Int32 in
            withUnsafeMutablePointer(to: &stats) { statsPointer in
                decode_run(raw.bindMemory(to: UInt8.self).baseAddress, raw.count, 0, 0, { ctx, frame in
                    let player = Unmanaged<StereoPlayer>.fromOpaque(ctx!).takeUnretainedValue()
                    return player.present(frame!.pointee)
                }, context, statsPointer)
            }
        }
        note("decode_finished", "rc \(rc)")
    }

    private func makePool(width: Int, height: Int) {
        var attributes: [String: Any] = [:]
        attributes[kCVPixelBufferPixelFormatTypeKey as String] = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        attributes[kCVPixelBufferWidthKey as String] = width
        attributes[kCVPixelBufferHeightKey as String] = height
        attributes[kCVPixelBufferIOSurfacePropertiesKey as String] = [String: Any]()
        attributes[kCVPixelBufferMetalCompatibilityKey as String] = true
        CVPixelBufferPoolCreate(nil, [kCVPixelBufferPoolMinimumBufferCountKey as String: 16] as CFDictionary,
                                attributes as CFDictionary, &pool)
    }

    private func pixelBuffer(_ frame: SpikeFrame, view: Int32, marker: CChar, index: Int, label: CChar) -> CVPixelBuffer? {
        var buffer: CVPixelBuffer?
        guard let pool, CVPixelBufferPoolCreatePixelBuffer(nil, pool, &buffer) == kCVReturnSuccess, let buffer else {
            return nil
        }
        CVPixelBufferLockBaseAddress(buffer, [])
        var f = frame
        spike_copy_nv12(&f, view,
                        CVPixelBufferGetBaseAddressOfPlane(buffer, 0)!.assumingMemoryBound(to: UInt8.self),
                        CVPixelBufferGetBytesPerRowOfPlane(buffer, 0),
                        CVPixelBufferGetBaseAddressOfPlane(buffer, 1)!.assumingMemoryBound(to: UInt8.self),
                        CVPixelBufferGetBytesPerRowOfPlane(buffer, 1), marker, Int64(index), label)
        CVPixelBufferUnlockBaseAddress(buffer, [])
        CVBufferSetAttachment(buffer, kCVImageBufferColorPrimariesKey, kCVImageBufferColorPrimaries_ITU_R_709_2, .shouldPropagate)
        CVBufferSetAttachment(buffer, kCVImageBufferTransferFunctionKey, kCVImageBufferTransferFunction_ITU_R_709_2, .shouldPropagate)
        CVBufferSetAttachment(buffer, kCVImageBufferYCbCrMatrixKey, kCVImageBufferYCbCrMatrix_ITU_R_709_2, .shouldPropagate)
        return buffer
    }

    private func clockNow() -> Double { synchronizer.currentTime().seconds }

    /// Decoding thread. Returns nonzero to stop decoding.
    private func present(_ frame: SpikeFrame) -> Int32 {
        let begin = monotonic()
        if pool == nil { makePool(width: Int(frame.width), height: Int(frame.height)) }
        lock.lock()
        let index = enqueued
        let label = CChar(UInt8(ascii: mode == .c ? "C" : "A")) // B never shows these frames
        lock.unlock()
        guard let left = pixelBuffer(frame, view: 0, marker: CChar(UInt8(ascii: "L")), index: index, label: label),
              let right = pixelBuffer(frame, view: 1, marker: CChar(UInt8(ascii: "R")), index: index, label: label) else {
            lock.lock(); poolFailures += 1; lock.unlock()
            return 0
        }
        let tagged = [
            CMTaggedBuffer(tags: [.mediaType(.video), .stereoView(.leftEye), .videoLayerID(0)], pixelBuffer: left),
            CMTaggedBuffer(tags: [.mediaType(.video), .stereoView(.rightEye), .videoLayerID(1)], pixelBuffer: right),
        ]
        if formatDescription == nil {
            formatDescription = CMTaggedBufferGroupFormatDescription(taggedBuffers: tagged)
            var eye: CMVideoFormatDescription?
            CMVideoFormatDescriptionCreateForImageBuffer(allocator: nil, imageBuffer: left, formatDescriptionOut: &eye)
            lock.lock(); eyeFormatDescription = eye; lock.unlock()
        }
        let pts = CMTime(value: CMTimeValue(index) * 1001, timescale: 24000)
        let sample = CMSampleBuffer(taggedBuffers: tagged, presentationTimeStamp: pts,
                                    duration: CMTime(value: 1001, timescale: 24000),
                                    formatDescription: formatDescription!)
        let prepared = monotonic()

        // Backpressure: the renderer's queue, and at most maxLead ahead of the clock.
        let ptsSeconds = pts.seconds
        while !renderer.isReadyForMoreMediaData || (clockStarted && ptsSeconds - clockNow() > Self.maxLead) {
            if renderer.status == .failed { break }
            usleep(1000)
        }
        let lead = clockStarted ? ptsSeconds - clockNow() : .nan
        renderer.enqueue(sample)

        lock.lock()
        if index < Self.capacity {
            enqueueLeadMs[index] = Float(lead * 1000)
            prepareMs[index] = Float((prepared - begin) * 1000)
        }
        if clockStarted && lead < 0 { lateEnqueues += 1 }
        enqueued += 1
        let startClock = !clockStarted && enqueued >= Self.prerollFrames
        let rate: Float = mode == .b ? 0 : 1
        lock.unlock()

        if renderer.status == .failed {
            note("renderer_failed", String(describing: renderer.error))
            return 1
        }
        if startClock {
            synchronizer.setRate(rate, time: .zero)
            startHost = monotonic()
            clockStarted = true
            note("clock_started", "after \(Self.prerollFrames) frames")
        }
        return 0
    }

    // MARK: measurement

    private func pollDisplay() {
        while true {
            usleep(5000)
            guard clockStarted else { continue }
            lock.lock()
            let current = mode
            lock.unlock()
            guard current != .b, let buffer = renderer.displayedPixelBuffer() else { continue }
            let host = monotonic(), clock = clockNow()
            CVPixelBufferLockBaseAddress(buffer, .readOnly)
            var markerOn: Int32 = 0
            let code = spike_read_index(CVPixelBufferGetBaseAddressOfPlane(buffer, 0)!.assumingMemoryBound(to: UInt8.self),
                                        CVPixelBufferGetBytesPerRowOfPlane(buffer, 0), &markerOn)
            CVPixelBufferUnlockBaseAddress(buffer, .readOnly)
            lock.lock()
            displayPolls += 1
            if markerOn != 0 { displayedRightEye += 1 } else { displayedLeftEye += 1 }
            if code >= 0 {
                // The cells hold the low 16 bits; take the nearest enqueued index.
                let index = enqueued - Int((Int32(enqueued & 0xffff) - code) & 0xffff)
                if index != lastDisplayed?.frame {
                    let bucket = buckets[current]!
                    bucket.displayedFrames += 1
                    bucket.clockMinusPtsMs.append((clock - Double(index) / Self.fps) * 1000)
                    if let last = lastDisplayed, last.segment == segment {
                        bucket.displayedIntervalsMs.append((host - last.host) * 1000)
                        if index - last.frame > 1 { bucket.skippedFrames += index - last.frame - 1 }
                    }
                    lastDisplayed = (index, host, segment)
                }
            } else {
                displayUnknown += 1
            }
            lock.unlock()
        }
    }

    private func avPlayerAccess() -> [String: Any] {
        var dropped = 0, observed = 0.0, indicated = 0.0, events = 0
        for item in looper?.loopingPlayerItems ?? [] {
            for event in item.accessLog()?.events ?? [] {
                events += 1
                dropped += max(0, event.numberOfDroppedVideoFrames)
                observed = max(observed, event.observedBitrate)
                indicated = max(indicated, event.indicatedBitrate)
            }
        }
        return ["access_log_events": events, "dropped_video_frames": dropped, "max_observed_bitrate": observed,
                "loop_count": looper?.loopCount ?? 0]
    }

    private func sample() {
        var lastCPU = cpuSeconds(), lastWall = monotonic(), lastFrames = 0
        var tick = 0
        while true {
            Thread.sleep(forTimeInterval: 1)
            tick += 1
            let cpu = cpuSeconds(), wall = monotonic()
            let thermal = thermalName(ProcessInfo.processInfo.thermalState)
            let cpuPercent = (cpu - lastCPU) / (wall - lastWall) * 100
            let avTime = avPlayer.currentTime().seconds
            lock.lock()
            let frames = enqueued
            thermalSeen.insert(thermal)
            buckets[mode]!.cpu.append(cpuPercent)
            samples.append([
                "t": wall - launch,
                "mode": mode.rawValue,
                "frames_enqueued": frames,
                "fps_enqueued": Double(frames - lastFrames) / (wall - lastWall),
                "custom_clock": clockStarted ? clockNow() : 0,
                "avplayer_time": avTime.isFinite ? avTime : 0,
                "display_link_hz": Double(linkTicksThisSecond) / (wall - lastWall),
                "realitykit_update_hz": Double(renderTicksThisSecond) / (wall - lastWall),
                "cpu_percent_of_one_core": cpuPercent,
                "thermal_state": thermal,
                "footprint_mb": Double(memoryFootprint().current) / 1_048_576,
            ])
            linkTicksThisSecond = 0
            renderTicksThisSecond = 0
            lock.unlock()
            lastCPU = cpu; lastWall = wall; lastFrames = frames
            if tick % 5 == 0 {
                renderer.loadVideoPerformanceMetrics { [weak self] m in
                    guard let self, let m else { return }
                    self.lock.lock()
                    self.metrics = ["total_frames": m.totalNumberOfFrames, "dropped_frames": m.numberOfDroppedFrames,
                                    "corrupted_frames": m.numberOfCorruptedFrames,
                                    "optimized_compositing_frames": m.numberOfFramesDisplayedUsingOptimizedCompositing,
                                    "accumulated_frame_delay_s": m.totalAccumulatedFrameDelay]
                    self.lock.unlock()
                }
                save()
            }
        }
    }

    func statusLine() -> String {
        lock.lock()
        defer { lock.unlock() }
        let last = samples.last
        let linkHz = last?["display_link_hz"] as? Double ?? 0
        let rkHz = last?["realitykit_update_hz"] as? Double ?? 0
        let dropped = metrics["dropped_frames"].map { "\($0)" } ?? "?"
        let next = autoCycle == nil ? "auto off" : String(format: "next switch %.0fs", max(0, nextCycle - monotonic()))
        return String(format: "mode %@   %@   display link %.1f Hz   RealityKit %.1f Hz   custom frames %d  late %d  dropped %@   thermal %@",
                      mode.rawValue, next, linkHz, rkHz, enqueued, lateEnqueues, dropped,
                      thermalName(ProcessInfo.processInfo.thermalState))
    }

    private func save() {
        let access = avPlayerAccess()
        lock.lock()
        var perMode: [String: Any] = [:]
        for (m, b) in buckets {
            let seconds = b.seconds + (m == mode ? monotonic() - modeSince : 0)
            perMode[m.rawValue] = [
                "seconds": seconds,
                "displayed_frames": b.displayedFrames,
                "skipped_frames": b.skippedFrames,
                "displayed_interval_ms": summary(b.displayedIntervalsMs),
                "displayed_interval_histogram_ms": histogram(b.displayedIntervalsMs),
                "clock_minus_pts_ms": summary(b.clockMinusPtsMs),
                "display_link_interval_ms": summary(b.displayLinkIntervalsMs),
                "display_link_duration_ms": summary(b.displayLinkDurationMs),
                "display_link_hz_from_median": b.displayLinkIntervalsMs.isEmpty ? 0 : 1000 / median(b.displayLinkIntervalsMs),
                "realitykit_delta_ms": summary(b.renderDeltaMs),
                "realitykit_hz_from_median": b.renderDeltaMs.isEmpty ? 0 : 1000 / median(b.renderDeltaMs),
                "avg_cpu_percent_of_one_core": b.cpu.isEmpty ? 0 : b.cpu.reduce(0, +) / Double(b.cpu.count),
            ] as [String: Any]
        }
        let n = min(enqueued, Self.capacity)
        let result: [String: Any] = [
            "schema": 2,
            "issue": 1103,
            "modes": [
                "A": "edge264 -> tagged NV12 CVPixelBuffers -> AVSampleBufferVideoRenderer -> VideoPlayerComponent(videoRenderer:)",
                "B": "AVQueuePlayer + AVPlayerLooper -> VideoPlayerComponent(avPlayer:), MV-HEVC 40 Mbps encode of the same clip with a B label burned in",
                "C": "A + UIWindow.avDisplayManager.preferredDisplayCriteria = AVDisplayCriteria(refreshRate: 23.976, eye format) + CADisplayLink preferredFrameRateRange 96",
            ],
            "measurement_notes": [
                "displayed_*: displayedPixelBuffer polled every 5 ms (left eye), frame index read from cells in the picture; modes A and C only",
                "display_link_*: CADisplayLink on the main run loop, default frame rate range",
                "realitykit_*: SceneEvents.Update deltaTime",
            ],
            "autocycle_seconds": autoCycle.map { $0 as Any } ?? NSNull(),
            "device": deviceInfo(),
            "seconds_since_launch": monotonic() - launch,
            "current_mode": mode.rawValue,
            "per_mode": perMode,
            "mode_switches": modeLog,
            "frames_decoded": stats.frames,
            "decoder_mono_frames": stats.mono_frames,
            "decoder_corrupt_nals": stats.corrupt_nals,
            "frames_enqueued": enqueued,
            "pool_failures": poolFailures,
            "late_enqueues": lateEnqueues,
            "enqueue_lead_ms": summary(enqueueLeadMs[0..<n].filter { !$0.isNaN }.map(Double.init)),
            "prepare_two_eyes_ms": summary(prepareMs[0..<n].filter { !$0.isNaN }.map(Double.init)),
            "display_poll": ["polls": displayPolls, "unreadable": displayUnknown,
                             "eye": ["left": displayedLeftEye, "right": displayedRightEye]],
            "renderer_metrics": metrics,
            "renderer_status": renderer.status.rawValue,
            "renderer_error": renderer.error.map { String(describing: $0) } ?? NSNull(),
            "avplayer": access,
            "component": componentState,
            "layout": layout,
            "criteria_matching_enabled": criteriaMatchingEnabled.map { $0 as Any } ?? NSNull(),
            "peak_footprint_mb": Double(memoryFootprint().peak) / 1_048_576,
            "thermal_states_seen": Array(thermalSeen).sorted(),
            "events": events,
            "samples": samples,
        ]
        lock.unlock()
        if let data = try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: resultURL, options: .atomic)
        }
    }
}

func median(_ values: [Double]) -> Double { values.sorted()[values.count / 2] }

/// Counts per 2 ms bin, keyed by the bin's lower edge.
func histogram(_ values: [Double]) -> [String: Int] {
    var bins: [String: Int] = [:]
    for v in values { bins[String(format: "%03d", Int(v / 2) * 2), default: 0] += 1 }
    return bins
}

func summary(_ values: [Double]) -> [String: Double] {
    guard !values.isEmpty else { return ["count": 0] }
    let sorted = values.sorted()
    func pct(_ p: Double) -> Double { sorted[min(sorted.count - 1, Int(Double(sorted.count - 1) * p))] }
    let mean = sorted.reduce(0, +) / Double(sorted.count)
    let sd = (sorted.reduce(0) { $0 + ($1 - mean) * ($1 - mean) } / Double(sorted.count)).squareRoot()
    return ["count": Double(sorted.count), "min": sorted[0], "p01": pct(0.01), "p10": pct(0.1), "p50": pct(0.5),
            "p90": pct(0.9), "p99": pct(0.99), "max": sorted[sorted.count - 1], "mean": mean, "stddev": sd]
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

func deviceInfo() -> [String: Any] {
    [
        "hw_machine": sysctlString("hw.machine"),
        "os": ProcessInfo.processInfo.operatingSystemVersionString,
        "active_processors": ProcessInfo.processInfo.activeProcessorCount,
        "low_power_mode": ProcessInfo.processInfo.isLowPowerModeEnabled,
    ]
}

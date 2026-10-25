#if BD_TO_AVP_QUALIFICATION
import Foundation

/// Opt-in read-speed measurement for a Blu-ray 3D library item, from local storage or a Files
/// share. Launch with BD_TO_AVP_SOURCE_READ_ITEM set to the item's file name; it reads the
/// item's title as fast as the source allows for BD_TO_AVP_SOURCE_READ_SECONDS (default 120)
/// and writes `source-read-<file name>.json` to Documents.
@MainActor
enum SourceReadQualification {
    struct Report: Codable {
        struct Sample: Codable {
            let second: Int
            let megabytes: Double
            let videoFrames: Int
        }

        let fileName: String
        let container: String
        let titleID: String
        let openSeconds: Double
        let readSeconds: Double
        /// Bytes the reader took from the source; 0 when the read ended in an error.
        let bytesRead: UInt64
        /// Video, audio and subtitle bytes delivered to the sink, counted even after an error.
        let deliveredMegabytes: Double
        let megabytesPerSecond: Double
        let slowestSecondMegabytes: Double
        let videoFrames: UInt64
        let audioPackets: UInt64
        let subtitlePackets: UInt64
        let contentSeconds: Double
        let realTimeMultiple: Double
        let samples: [Sample]
        let error: String?
    }

    static func runIfRequested(model: PlayerAppModel) async {
        let environment = ProcessInfo.processInfo.environment
        guard let fileName = environment["BD_TO_AVP_SOURCE_READ_ITEM"], !fileName.isEmpty else { return }
        let seconds = environment["BD_TO_AVP_SOURCE_READ_SECONDS"].flatMap(Double.init) ?? 120
        guard let item = model.library.items.first(where: { $0.fileName == fileName }) else {
            emit("item_missing file=\(fileName)")
            return
        }
        do {
            let lease = try await model.bookmarkStore.open(id: item.id)
            defer { lease.close() }
            let url = lease.url
            let report = await Task.detached(priority: .userInitiated) {
                measure(url: url, fileName: fileName, seconds: seconds)
            }.value
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            try encoder.encode(report).write(to: documents.appendingPathComponent("source-read-\(fileName).json"))
            emit(
                "complete mbps=\(report.megabytesPerSecond) slowest_second_mb=\(report.slowestSecondMegabytes) "
                    + "frames=\(report.videoFrames) realtime=\(report.realTimeMultiple) error=\(report.error ?? "none")"
            )
        } catch {
            emit("failed error=\(error)")
        }
    }

    private final class MeasuringSink: Bluray3DPacketSink {
        let started = ContinuousClock.now
        let limit: Duration
        var samples: [Report.Sample] = []
        var bytes = 0
        var frames = 0
        var totalBytes = 0
        var totalFrames: UInt64 = 0
        var timeline: BluRayPresentationTimeline?
        var latestTicks: Int64 = 0

        init(seconds: Double) { limit = .seconds(seconds) }

        func receive(_ packet: Bluray3DPacket) -> Bool {
            switch packet {
            case let .video(unit):
                bytes += unit.data.count
                totalBytes += unit.data.count
                frames += 1
                totalFrames += 1
                let timeline = timeline ?? BluRayPresentationTimeline(originPTS: unit.pts)
                self.timeline = timeline
                latestTicks = max(latestTicks, timeline.ticks(for: unit.pts, near: latestTicks))
            case let .audio(pes), let .subtitle(pes):
                bytes += pes.payload.count
                totalBytes += pes.payload.count
            }
            let elapsed = started.duration(to: .now)
            let second = Int(elapsed.components.seconds)
            if second >= samples.count + 1 {
                samples.append(.init(second: samples.count, megabytes: Double(bytes) / 1_000_000, videoFrames: frames))
                emit("sample second=\(samples.count) mb=\(Double(bytes) / 1_000_000) frames=\(frames)")
                bytes = 0
                frames = 0
            }
            return elapsed < limit
        }
    }

    nonisolated private static func measure(url: URL, fileName: String, seconds: Double) -> Report {
        let opening = ContinuousClock.now
        let source: Bluray3DSource
        do {
            source = try Bluray3DSourceOpener.open(url)
        } catch {
            return failed(fileName: fileName, error: error)
        }
        let openSeconds = elapsedSeconds(opening.duration(to: .now))
        guard let title = source.info.mainTitle else {
            return failed(fileName: fileName, error: Bluray3DSourceError.titleUnavailable)
        }
        emit("opened container=\(source.info.container.rawValue) title=\(title.id) open_seconds=\(openSeconds)")
        let sink = MeasuringSink(seconds: seconds)
        var statistics = Bluray3DReadStatistics()
        var failure: String?
        do {
            statistics = try source.read(
                title: title,
                audioTrackIDs: title.audioTracks.map(\.id),
                subtitleTrackIDs: title.subtitleTracks.map(\.id),
                into: sink
            )
        } catch {
            failure = String(describing: error)
        }
        let readSeconds = elapsedSeconds(sink.started.duration(to: .now))
        let contentSeconds = Double(sink.latestTicks) / Double(BluRayPresentationTimeline.ticksPerSecond)
        return Report(
            fileName: fileName,
            container: source.info.container.rawValue,
            titleID: title.id,
            openSeconds: openSeconds,
            readSeconds: readSeconds,
            bytesRead: statistics.bytes,
            deliveredMegabytes: Double(sink.totalBytes) / 1_000_000,
            megabytesPerSecond: Double(max(statistics.bytes, UInt64(sink.totalBytes))) / 1_000_000 / readSeconds,
            slowestSecondMegabytes: sink.samples.map(\.megabytes).min() ?? 0,
            videoFrames: sink.totalFrames,
            audioPackets: statistics.audioPackets,
            subtitlePackets: statistics.subtitlePackets,
            contentSeconds: contentSeconds,
            realTimeMultiple: contentSeconds / readSeconds,
            samples: sink.samples,
            error: failure
        )
    }

    nonisolated private static func failed(fileName: String, error: Error) -> Report {
        Report(
            fileName: fileName, container: "", titleID: "", openSeconds: 0, readSeconds: 0, bytesRead: 0,
            deliveredMegabytes: 0, megabytesPerSecond: 0, slowestSecondMegabytes: 0, videoFrames: 0, audioPackets: 0, subtitlePackets: 0,
            contentSeconds: 0, realTimeMultiple: 0, samples: [], error: String(describing: error)
        )
    }

    nonisolated private static func elapsedSeconds(_ duration: Duration) -> Double {
        Double(duration.components.seconds) + Double(duration.components.attoseconds) / 1e18
    }

    nonisolated private static func emit(_ message: String) {
        FileHandle.standardOutput.write(Data("SOURCE_READ \(message)\n".utf8))
    }
}
#endif

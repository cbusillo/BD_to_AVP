import AVFoundation
import Foundation

/// Compiled with the production relay bridge by the fixture generator. This
/// checks decoded frames, not merely FFprobe parsing or AVURLAsset.isPlayable.
@main struct EventHLSPlaybackAcceptance {
    @MainActor static func main() async {
        do {
            guard CommandLine.arguments.count == 2 else { throw URLError(.badURL) }
            let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
            let (server, url) = try await RelayLoopbackHTTPServer.start(serverBaseURL: URL(string: "http://fixture.invalid")!) { url in
                let name = url.lastPathComponent == "playlist.m3u8" ? "media.m3u8" : url.lastPathComponent
                var data = try Data(contentsOf: root.appendingPathComponent(name))
                if name == "media.m3u8" {
                    data = Data(String(decoding: data, as: UTF8.self)
                        .replacingOccurrences(of: "URI=\"init.mp4", with: "URI=\"/relay/v1/media/init.mp4")
                        .replacingOccurrences(of: "\nsegment-", with: "\n/relay/v1/media/segment-").utf8)
                }
                return (data, HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!)
            }
            defer { server.cancelAllRequests() }
            let asset = AVURLAsset(url: url)
            guard try await asset.load(.isPlayable) else { throw URLError(.cannotDecodeContentData) }
            let duration = try await asset.load(.duration).seconds
            guard duration.isFinite, duration >= 2, duration <= 30 else { throw URLError(.cannotDecodeContentData) }
            let item = AVPlayerItem(asset: asset)
            let video = AVPlayerItemVideoOutput(pixelBufferAttributes: [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            ])
            let player = AVPlayer(playerItem: item)
            player.isMuted = true
            player.play()
            defer { player.pause() }
            // HLS discovers its tracks asynchronously. Attach the output after
            // readiness so it binds to the actual video track.
            let readinessDeadline = ContinuousClock.now.advanced(by: .seconds(10))
            while item.status == .unknown, ContinuousClock.now < readinessDeadline {
                try await Task.sleep(for: .milliseconds(50))
            }
            guard item.status == .readyToPlay else { throw item.error ?? URLError(.timedOut) }
            item.add(video)
            var decodedFrames = 0
            var decodedIntervals = Set<Int>()
            var latestDecodedTime: Double = -.infinity
            let deadline = ContinuousClock.now.advanced(by: .seconds(30))
            while ContinuousClock.now < deadline {
                try await Task.sleep(for: .milliseconds(50))
                if item.status == .failed { throw item.error ?? URLError(.cannotDecodeContentData) }
                let time = player.currentTime()
                var displayTime = CMTime.invalid
                if video.hasNewPixelBuffer(forItemTime: time), video.copyPixelBuffer(forItemTime: time, itemTimeForDisplay: &displayTime) != nil,
                   displayTime.seconds.isFinite {
                    decodedFrames += 1
                    latestDecodedTime = displayTime.seconds
                    decodedIntervals.insert(Int(displayTime.seconds / 2))
                }
                let requiredIntervals = Set(0..<Int(ceil(duration / 2)))
                if latestDecodedTime >= duration - 0.25,
                   decodedIntervals.isSuperset(of: requiredIntervals) {
                    print("AVFoundation relay HLS accepted: \(duration)s, \(decodedFrames) decoded frame samples across \(requiredIntervals.count) two-second intervals; final sample at \(latestDecodedTime)s")
                    return
                }
            }
            throw URLError(.timedOut)
        } catch {
            FileHandle.standardError.write(Data("AVFoundation relay HLS rejected: \(error)\n".utf8))
            exit(1)
        }
    }
}

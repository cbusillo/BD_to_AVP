import AVFoundation
import Foundation

struct MediaFormatInspector {
    struct Signals: Equatable, Sendable {
        let isStereoVideo: Bool
        let isStereoMultiviewVideo: Bool
        let width: Int?
        let height: Int?
    }

    static func classify(_ signals: Signals) -> StereoFormat {
        if signals.isStereoMultiviewVideo {
            return .mvHEVC
        }
        guard signals.isStereoVideo,
              let width = signals.width,
              let height = signals.height,
              width > 0,
              height > 0
        else {
            return .unsupported
        }
        return width >= height ? .sideBySide : .overUnder
    }

    static func inspect(url: URL) async throws -> StereoFormat {
        let asset = AVURLAsset(url: url)
        let assistant = AVAssetPlaybackAssistant(asset: asset)
        let options = await assistant.playbackConfigurationOptions
        let tracks = try await asset.load(.tracks)
        let videoTrack = tracks.first { $0.mediaType == .video }
        let naturalSize = try await videoTrack?.load(.naturalSize)

        return classify(
            Signals(
                isStereoVideo: options.contains(.stereoVideo),
                isStereoMultiviewVideo: options.contains(.stereoMultiviewVideo),
                width: naturalSize.map { Int(abs($0.width)) },
                height: naturalSize.map { Int(abs($0.height)) }
            )
        )
    }
}

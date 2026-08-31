import AVFoundation
import Foundation

struct MediaFormatInspector {
    struct Signals: Equatable, Sendable {
        let isStereoVideo: Bool
        let isStereoMultiviewVideo: Bool
        let isHEVC: Bool
        let isHDR: Bool
        let width: Int?
        let height: Int?
        let packedStereoFormat: StereoFormat?

        init(
            isStereoVideo: Bool,
            isStereoMultiviewVideo: Bool,
            isHEVC: Bool = true,
            isHDR: Bool = false,
            width: Int?,
            height: Int?,
            packedStereoFormat: StereoFormat? = nil
        ) {
            self.isStereoVideo = isStereoVideo
            self.isStereoMultiviewVideo = isStereoMultiviewVideo
            self.isHEVC = isHEVC
            self.isHDR = isHDR
            self.width = width
            self.height = height
            self.packedStereoFormat = packedStereoFormat
        }
    }

    static func classify(_ signals: Signals) -> StereoFormat {
        if signals.isStereoMultiviewVideo {
            return .mvHEVC
        }
        guard signals.isHEVC, !signals.isHDR else {
            return .unsupported
        }
        if let packedStereoFormat = signals.packedStereoFormat {
            return packedStereoFormat
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
        let formatDescriptions = try await videoTrack?.load(.formatDescriptions) ?? []
        let mediaCharacteristics = try await videoTrack?.load(.mediaCharacteristics) ?? []
        let isHEVC = formatDescriptions.contains { $0.mediaSubType == .hevc }
        let packedStereoFormat = formatDescriptions.lazy.compactMap(packedStereoFormat).first
            ?? packedStereoFormat(fileName: url.lastPathComponent)

        return classify(
            Signals(
                isStereoVideo: options.contains(.stereoVideo),
                isStereoMultiviewVideo: options.contains(.stereoMultiviewVideo),
                isHEVC: isHEVC,
                isHDR: mediaCharacteristics.contains(.containsHDRVideo),
                width: naturalSize.map { Int(abs($0.width)) },
                height: naturalSize.map { Int(abs($0.height)) },
                packedStereoFormat: packedStereoFormat
            )
        )
    }

    private static func packedStereoFormat(from description: CMFormatDescription) -> StereoFormat? {
        let packing = description.extensions[.viewPackingKind]
        if packing == .viewPackingKind(.sideBySide) {
            return .sideBySide
        }
        if packing == .viewPackingKind(.overUnder) {
            return .overUnder
        }
        return nil
    }

    static func packedStereoFormat(fileName: String) -> StereoFormat? {
        let tokens = Set(
            fileName
                .lowercased()
                .split(whereSeparator: { !$0.isLetter && !$0.isNumber })
                .map(String.init)
        )
        if !tokens.isDisjoint(with: ["half", "hsbs", "hou"])
            || (tokens.contains("h") && !tokens.isDisjoint(with: ["sbs", "ou"]))
        {
            return nil
        }
        if !tokens.isDisjoint(with: ["sbs", "fsbs"]) {
            return .sideBySide
        }
        if !tokens.isDisjoint(with: ["ou", "fou"]) {
            return .overUnder
        }
        return nil
    }
}

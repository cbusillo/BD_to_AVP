import AVFoundation
import CoreMedia

final class PackedStereoCompositionInstruction: NSObject, AVVideoCompositionInstructionProtocol, @unchecked Sendable {
    let timeRange: CMTimeRange
    let enablePostProcessing = false
    let containsTweening = false
    let requiredSourceTrackIDs: [NSValue]?
    let passthroughTrackID = kCMPersistentTrackID_Invalid
    let sourceTrackID: CMPersistentTrackID
    let geometry: PackedStereoGeometry
    let eyeOrder: PackedStereoEyeOrder

    init(
        timeRange: CMTimeRange,
        sourceTrackID: CMPersistentTrackID,
        geometry: PackedStereoGeometry,
        eyeOrder: PackedStereoEyeOrder
    ) {
        self.timeRange = timeRange
        self.sourceTrackID = sourceTrackID
        self.geometry = geometry
        self.eyeOrder = eyeOrder
        requiredSourceTrackIDs = [NSNumber(value: sourceTrackID)]
    }
}

enum PackedStereoComposition {
    enum Error: LocalizedError {
        case invalidPackedDimensions
        case unsupportedFormat
        case unsupportedPresentationGeometry
        case missingVideoTrack

        var errorDescription: String? {
            switch self {
            case .invalidPackedDimensions:
                return "The packed stereo movie dimensions do not contain two chroma-aligned eye images."
            case .unsupportedFormat:
                return "The movie does not use a supported packed stereo layout."
            case .unsupportedPresentationGeometry:
                return "Rotated, cropped, or anamorphic packed stereo movies are not supported."
            case .missingVideoTrack:
                return "The movie does not contain a video track."
            }
        }
    }

    static let outputBufferDescription: [[CMTag]] = [
        [
            CMTag.mediaType(.video),
            CMTag.videoLayerID(0),
            CMTag.stereoView(.leftEye),
            CMTag.projectionType(.rectangular),
        ],
        [
            CMTag.mediaType(.video),
            CMTag.videoLayerID(1),
            CMTag.stereoView(.rightEye),
            CMTag.projectionType(.rectangular),
        ],
    ]

    static func make(
        asset: AVAsset,
        format: StereoFormat,
        duration: CMTime,
        eyeOrder: PackedStereoEyeOrder
    ) async throws -> AVVideoComposition {
        guard format == .sideBySide || format == .overUnder else {
            throw Error.unsupportedFormat
        }
        guard let videoTrack = try await asset.loadTracks(withMediaType: .video).first else {
            throw Error.missingVideoTrack
        }
        let preferredTransform = try await videoTrack.load(.preferredTransform)
        let formatDescriptions = try await videoTrack.load(.formatDescriptions)
        guard preferredTransform.isIdentity,
              let formatDescription = formatDescriptions.first
        else {
            throw Error.unsupportedPresentationGeometry
        }
        guard formatDescription.mediaSubType == .hevc else {
            throw Error.unsupportedFormat
        }
        let codedDimensions = CMVideoFormatDescriptionGetDimensions(formatDescription)
        let presentationSize = CMVideoFormatDescriptionGetPresentationDimensions(
            formatDescription,
            usePixelAspectRatio: true,
            useCleanAperture: true
        )
        guard Int(presentationSize.width.rounded()) == codedDimensions.width,
              Int(presentationSize.height.rounded()) == codedDimensions.height
        else {
            throw Error.unsupportedPresentationGeometry
        }
        guard let geometry = PackedStereoGeometry(
            sourceWidth: Int(codedDimensions.width),
            sourceHeight: Int(codedDimensions.height),
            format: format
        ) else {
            throw Error.invalidPackedDimensions
        }

        let instruction = PackedStereoCompositionInstruction(
            timeRange: CMTimeRange(start: .zero, duration: duration),
            sourceTrackID: videoTrack.trackID,
            geometry: geometry,
            eyeOrder: eyeOrder
        )
        var configuration = try await AVVideoComposition.Configuration(for: asset)
        configuration.customVideoCompositorClass = PackedStereoVideoCompositor.self
        configuration.instructions = [instruction]
        configuration.outputBufferDescription = outputBufferDescription
        configuration.renderScale = 1
        configuration.renderSize = geometry.outputSize
        configuration.sourceTrackIDForFrameTiming = videoTrack.trackID
        return AVVideoComposition(configuration: configuration)
    }
}

import AVFoundation
import CoreMedia

struct PackedStereoSpatialMetadata: Equatable, Sendable {
    let horizontalFieldOfView: UInt32?
    let cameraSystemBaseline: UInt32?
    let disparityAdjustment: Int32?

    static let qualificationFixture = PackedStereoSpatialMetadata(
        horizontalFieldOfView: 90_000,
        cameraSystemBaseline: 64_000,
        disparityAdjustment: 0
    )
}

final class PackedStereoCompositionInstruction: NSObject, AVVideoCompositionInstructionProtocol, @unchecked Sendable {
    let timeRange: CMTimeRange
    let enablePostProcessing = false
    let containsTweening = false
    let requiredSourceTrackIDs: [NSValue]?
    let passthroughTrackID = kCMPersistentTrackID_Invalid
    let sourceTrackID: CMPersistentTrackID
    let geometry: PackedStereoGeometry
    let eyeOrder: PackedStereoEyeOrder
    let spatialConfiguration: AVSpatialVideoConfiguration

    init(
        timeRange: CMTimeRange,
        sourceTrackID: CMPersistentTrackID,
        geometry: PackedStereoGeometry,
        eyeOrder: PackedStereoEyeOrder,
        spatialConfiguration: AVSpatialVideoConfiguration
    ) {
        self.timeRange = timeRange
        self.sourceTrackID = sourceTrackID
        self.geometry = geometry
        self.eyeOrder = eyeOrder
        self.spatialConfiguration = spatialConfiguration
        requiredSourceTrackIDs = [NSNumber(value: sourceTrackID)]
    }
}

enum PackedStereoComposition {
    static let outputColorYCbCrMatrix = kCVImageBufferYCbCrMatrix_ITU_R_709_2 as String
    static let outputColorPrimaries = kCVImageBufferColorPrimaries_ITU_R_709_2 as String
    static let outputColorTransferFunction = kCVImageBufferTransferFunction_ITU_R_709_2 as String

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
            CMTag.stereoView(.leftEye),
            CMTag.projectionType(.rectangular),
        ],
        [
            CMTag.mediaType(.video),
            CMTag.stereoView(.rightEye),
            CMTag.projectionType(.rectangular),
        ],
    ]

    static func make(
        asset: AVAsset,
        format: StereoFormat,
        duration: CMTime,
        eyeOrder: PackedStereoEyeOrder,
        spatialMetadataFallback: PackedStereoSpatialMetadata? = nil
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

        let extensions = formatDescription.extensions
        var spatialConfiguration = AVSpatialVideoConfiguration()
        spatialConfiguration.horizontalFieldOfView =
            (extensions[kCMFormatDescriptionExtension_HorizontalFieldOfView] as? NSNumber)?.uint32Value
                ?? spatialMetadataFallback?.horizontalFieldOfView
        spatialConfiguration.cameraSystemBaseline =
            (extensions[kCMFormatDescriptionExtension_StereoCameraBaseline] as? NSNumber)?.uint32Value
                ?? spatialMetadataFallback?.cameraSystemBaseline
        spatialConfiguration.disparityAdjustment =
            (extensions[kCMFormatDescriptionExtension_HorizontalDisparityAdjustment] as? NSNumber)?.int32Value
                ?? spatialMetadataFallback?.disparityAdjustment
        let instruction = PackedStereoCompositionInstruction(
            timeRange: CMTimeRange(start: .zero, duration: duration),
            sourceTrackID: videoTrack.trackID,
            geometry: geometry,
            eyeOrder: eyeOrder,
            spatialConfiguration: spatialConfiguration
        )
        var configuration = try await AVVideoComposition.Configuration(for: asset)
        configuration.customVideoCompositorClass = PackedStereoVideoCompositor.self
        configuration.instructions = [instruction]
        configuration.outputBufferDescription = outputBufferDescription
        configuration.spatialVideoConfigurations = [spatialConfiguration]
        configuration.renderScale = 1
        configuration.renderSize = geometry.outputSize
        configuration.sourceTrackIDForFrameTiming = videoTrack.trackID
        configuration.colorYCbCrMatrix = outputColorYCbCrMatrix
        configuration.colorPrimaries = outputColorPrimaries
        configuration.colorTransferFunction = outputColorTransferFunction
        return AVVideoComposition(configuration: configuration)
    }
}

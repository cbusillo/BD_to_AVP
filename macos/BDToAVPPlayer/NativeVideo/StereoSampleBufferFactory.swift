import CoreMedia
import CoreVideo
import Edge264MVC
import Foundation

enum StereoSampleBufferError: Error, Equatable {
    case pixelBufferUnavailable(CVReturn)
    case sampleBufferFailed
}

/// Turns decoded MVC frames into the stereo sample buffers `AVSampleBufferVideoRenderer` shows in
/// RealityKit: one NV12 pixel buffer per eye, tagged `.stereoView(.leftEye)` and
/// `.stereoView(.rightEye)`, in one `CMSampleBuffer` (#1103). Blu-ray 3D codes the left eye as
/// the base view unless the title says otherwise. A subtitle is drawn into both eyes, each copy
/// shifted for its eye so it stands in front of the screen.
final class StereoSampleBufferFactory {
    /// Every Blu-ray frame is 23.976 fps or slower; the renderer uses the next frame's time anyway.
    static let defaultFrameDuration = CMTime(value: 1_001, timescale: 24_000)

    private let baseViewIsRightEye: Bool
    private var pool: CVPixelBufferPool?
    private var poolSize: (width: Int32, height: Int32)?
    private var formatDescription: CMTaggedBufferGroupFormatDescription?

    init(baseViewIsRightEye: Bool) {
        self.baseViewIsRightEye = baseViewIsRightEye
    }

    /// A stereo sample buffer for `frame`, presented at `presentationTime` on the shared clock,
    /// with `subtitle` drawn over it.
    func sampleBuffer(
        for frame: UnsafePointer<BDAVPMVCFrame>,
        presentationTime: CMTime,
        duration: CMTime = defaultFrameDuration,
        subtitle: SubtitleOverlay? = nil
    ) throws -> CMSampleBuffer {
        let baseEye: SubtitleOverlay.Eye = baseViewIsRightEye ? .right : .left
        let base = try pixelBuffer(frame, view: 0, subtitle: subtitle.map { ($0, baseEye) })
        let dependent = try pixelBuffer(frame, view: 1, subtitle: subtitle.map { ($0, baseEye == .left ? .right : .left) })
        let (left, right) = baseViewIsRightEye ? (dependent, base) : (base, dependent)
        let tagged = [
            CMTaggedBuffer(tags: [.mediaType(.video), .stereoView(.leftEye), .videoLayerID(0)], pixelBuffer: left),
            CMTaggedBuffer(tags: [.mediaType(.video), .stereoView(.rightEye), .videoLayerID(1)], pixelBuffer: right),
        ]
        let description: CMTaggedBufferGroupFormatDescription
        if let formatDescription {
            description = formatDescription
        } else {
            description = CMTaggedBufferGroupFormatDescription(taggedBuffers: tagged)
            formatDescription = description
        }
        return CMSampleBuffer(
            taggedBuffers: tagged,
            presentationTimeStamp: presentationTime,
            duration: duration,
            formatDescription: description
        )
    }

    private func pixelBuffer(
        _ frame: UnsafePointer<BDAVPMVCFrame>,
        view: Int32,
        subtitle: (overlay: SubtitleOverlay, eye: SubtitleOverlay.Eye)?
    ) throws -> CVPixelBuffer {
        let width = frame.pointee.width
        let height = frame.pointee.height
        if poolSize?.width != width || poolSize?.height != height {
            pool = Self.makePool(width: Int(width), height: Int(height))
            poolSize = (width, height)
            formatDescription = nil
        }
        var created: CVPixelBuffer?
        let status = pool.map { CVPixelBufferPoolCreatePixelBuffer(nil, $0, &created) } ?? kCVReturnInvalidArgument
        guard status == kCVReturnSuccess, let buffer = created else {
            throw StereoSampleBufferError.pixelBufferUnavailable(status)
        }
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        guard let luma = CVPixelBufferGetBaseAddressOfPlane(buffer, 0),
              let chroma = CVPixelBufferGetBaseAddressOfPlane(buffer, 1)
        else {
            throw StereoSampleBufferError.pixelBufferUnavailable(kCVReturnInvalidPixelBufferAttributes)
        }
        bdavp_mvc_copy_nv12(
            frame, view,
            luma.assumingMemoryBound(to: UInt8.self), CVPixelBufferGetBytesPerRowOfPlane(buffer, 0),
            chroma.assumingMemoryBound(to: UInt8.self), CVPixelBufferGetBytesPerRowOfPlane(buffer, 1)
        )
        if let subtitle {
            subtitle.overlay.draw(into: buffer, eye: subtitle.eye)
        }
        // Blu-ray video is BT.709.
        CVBufferSetAttachment(buffer, kCVImageBufferColorPrimariesKey, kCVImageBufferColorPrimaries_ITU_R_709_2, .shouldPropagate)
        CVBufferSetAttachment(buffer, kCVImageBufferTransferFunctionKey, kCVImageBufferTransferFunction_ITU_R_709_2, .shouldPropagate)
        CVBufferSetAttachment(buffer, kCVImageBufferYCbCrMatrixKey, kCVImageBufferYCbCrMatrix_ITU_R_709_2, .shouldPropagate)
        return buffer
    }

    private static func makePool(width: Int, height: Int) -> CVPixelBufferPool? {
        let attributes: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
            kCVPixelBufferWidthKey as String: width,
            kCVPixelBufferHeightKey as String: height,
            kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any](),
            kCVPixelBufferMetalCompatibilityKey as String: true,
        ]
        var pool: CVPixelBufferPool?
        CVPixelBufferPoolCreate(
            nil,
            [kCVPixelBufferPoolMinimumBufferCountKey as String: 16] as CFDictionary,
            attributes as CFDictionary,
            &pool
        )
        return pool
    }
}

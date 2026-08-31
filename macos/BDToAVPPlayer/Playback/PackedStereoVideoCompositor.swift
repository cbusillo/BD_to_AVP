import AVFoundation
import CoreMedia
import CoreVideo
import Foundation

enum PackedStereoEyeOrder: Equatable, Sendable {
    case normal
    case reversed
}

enum PackedStereoOutput: Int, CaseIterable, Sendable {
    case left
    case right
}

struct PackedStereoRegion: Equatable, Sendable {
    let x: Int
    let y: Int
    let width: Int
    let height: Int
}

struct PackedStereoGeometry: Equatable, Sendable {
    let sourceWidth: Int
    let sourceHeight: Int
    let eyeWidth: Int
    let eyeHeight: Int
    let format: StereoFormat

    init?(sourceWidth: Int, sourceHeight: Int, format: StereoFormat) {
        guard sourceWidth > 0, sourceHeight > 0 else {
            return nil
        }

        switch format {
        case .sideBySide:
            guard sourceWidth.isMultiple(of: 4), sourceHeight.isMultiple(of: 2) else {
                return nil
            }
            eyeWidth = sourceWidth / 2
            eyeHeight = sourceHeight
        case .overUnder:
            guard sourceWidth.isMultiple(of: 2), sourceHeight.isMultiple(of: 4) else {
                return nil
            }
            eyeWidth = sourceWidth
            eyeHeight = sourceHeight / 2
        case .mvHEVC, .unsupported:
            return nil
        }

        self.sourceWidth = sourceWidth
        self.sourceHeight = sourceHeight
        self.format = format
    }

    var outputSize: CGSize {
        CGSize(width: eyeWidth, height: eyeHeight)
    }

    func sourceRegion(for output: PackedStereoOutput, eyeOrder: PackedStereoEyeOrder) -> PackedStereoRegion {
        let firstRegion = PackedStereoRegion(x: 0, y: 0, width: eyeWidth, height: eyeHeight)
        let secondRegion: PackedStereoRegion
        switch format {
        case .sideBySide:
            secondRegion = PackedStereoRegion(x: eyeWidth, y: 0, width: eyeWidth, height: eyeHeight)
        case .overUnder:
            secondRegion = PackedStereoRegion(x: 0, y: eyeHeight, width: eyeWidth, height: eyeHeight)
        case .mvHEVC, .unsupported:
            preconditionFailure("Packed stereo geometry cannot use a non-packed format.")
        }

        let normalRegion = output == .left ? firstRegion : secondRegion
        let reversedRegion = output == .left ? secondRegion : firstRegion
        return eyeOrder == .normal ? normalRegion : reversedRegion
    }
}

enum PackedStereoFrameRenderer {
    static let pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange

    enum Error: LocalizedError {
        case invalidSourceFormat
        case invalidSourceDimensions
        case invalidOutputFormat
        case invalidOutputDimensions
        case bufferLockFailed(CVReturn)

        var errorDescription: String? {
            switch self {
            case .invalidSourceFormat:
                return "The packed stereo source frame is not an 8-bit biplanar video-range buffer."
            case .invalidSourceDimensions:
                return "The packed stereo source frame dimensions changed during playback."
            case .invalidOutputFormat:
                return "The packed stereo output frame is not an 8-bit biplanar video-range buffer."
            case .invalidOutputDimensions:
                return "The packed stereo output frame dimensions do not match one eye view."
            case let .bufferLockFailed(status):
                return "The packed stereo frame memory could not be accessed (Core Video error \(status))."
            }
        }
    }

    static func render(
        source: borrowing CVReadOnlyPixelBuffer,
        geometry: PackedStereoGeometry,
        eyeOrder: PackedStereoEyeOrder,
        leftOutput: inout CVMutablePixelBuffer,
        rightOutput: inout CVMutablePixelBuffer
    ) throws {
        try source.withUnsafeBuffer { sourceBuffer in
            try leftOutput.withUnsafeBuffer { leftBuffer in
                try rightOutput.withUnsafeBuffer { rightBuffer in
                    try render(
                        source: sourceBuffer,
                        geometry: geometry,
                        eyeOrder: eyeOrder,
                        leftOutput: leftBuffer,
                        rightOutput: rightBuffer
                    )
                }
            }
        }
    }

    private static func render(
        source: CVPixelBuffer,
        geometry: PackedStereoGeometry,
        eyeOrder: PackedStereoEyeOrder,
        leftOutput: CVPixelBuffer,
        rightOutput: CVPixelBuffer
    ) throws {
        try validateSource(source, geometry: geometry)
        try validateOutput(leftOutput, geometry: geometry)
        try validateOutput(rightOutput, geometry: geometry)

        let sourceLockStatus = CVPixelBufferLockBaseAddress(source, .readOnly)
        guard sourceLockStatus == kCVReturnSuccess else {
            throw Error.bufferLockFailed(sourceLockStatus)
        }
        defer { CVPixelBufferUnlockBaseAddress(source, .readOnly) }

        let leftLockStatus = CVPixelBufferLockBaseAddress(leftOutput, [])
        guard leftLockStatus == kCVReturnSuccess else {
            throw Error.bufferLockFailed(leftLockStatus)
        }
        defer { CVPixelBufferUnlockBaseAddress(leftOutput, []) }

        let rightLockStatus = CVPixelBufferLockBaseAddress(rightOutput, [])
        guard rightLockStatus == kCVReturnSuccess else {
            throw Error.bufferLockFailed(rightLockStatus)
        }
        defer { CVPixelBufferUnlockBaseAddress(rightOutput, []) }

        try copy(
            source: source,
            region: geometry.sourceRegion(for: .left, eyeOrder: eyeOrder),
            destination: leftOutput
        )
        try copy(
            source: source,
            region: geometry.sourceRegion(for: .right, eyeOrder: eyeOrder),
            destination: rightOutput
        )
        CVBufferRemoveAllAttachments(leftOutput)
        CVBufferRemoveAllAttachments(rightOutput)
        CVBufferPropagateAttachments(source, leftOutput)
        CVBufferPropagateAttachments(source, rightOutput)
    }

    private static func validateSource(_ buffer: CVPixelBuffer, geometry: PackedStereoGeometry) throws {
        guard CVPixelBufferGetPixelFormatType(buffer) == pixelFormat,
              CVPixelBufferGetPlaneCount(buffer) == 2
        else {
            throw Error.invalidSourceFormat
        }
        guard CVPixelBufferGetWidth(buffer) == geometry.sourceWidth,
              CVPixelBufferGetHeight(buffer) == geometry.sourceHeight
        else {
            throw Error.invalidSourceDimensions
        }
    }

    private static func validateOutput(_ buffer: CVPixelBuffer, geometry: PackedStereoGeometry) throws {
        guard CVPixelBufferGetPixelFormatType(buffer) == pixelFormat,
              CVPixelBufferGetPlaneCount(buffer) == 2
        else {
            throw Error.invalidOutputFormat
        }
        guard CVPixelBufferGetWidth(buffer) == geometry.eyeWidth,
              CVPixelBufferGetHeight(buffer) == geometry.eyeHeight
        else {
            throw Error.invalidOutputDimensions
        }
    }

    private static func copy(
        source: CVPixelBuffer,
        region: PackedStereoRegion,
        destination: CVPixelBuffer
    ) throws {
        try copyPlane(
            source: source,
            sourcePlane: 0,
            sourceX: region.x,
            sourceY: region.y,
            rows: region.height,
            bytesPerRow: region.width,
            destination: destination,
            destinationPlane: 0
        )
        try copyPlane(
            source: source,
            sourcePlane: 1,
            sourceX: region.x,
            sourceY: region.y / 2,
            rows: region.height / 2,
            bytesPerRow: region.width,
            destination: destination,
            destinationPlane: 1
        )
    }

    private static func copyPlane(
        source: CVPixelBuffer,
        sourcePlane: Int,
        sourceX: Int,
        sourceY: Int,
        rows: Int,
        bytesPerRow: Int,
        destination: CVPixelBuffer,
        destinationPlane: Int
    ) throws {
        guard let sourceBaseAddress = CVPixelBufferGetBaseAddressOfPlane(source, sourcePlane),
              let destinationBaseAddress = CVPixelBufferGetBaseAddressOfPlane(destination, destinationPlane)
        else {
            throw Error.invalidSourceFormat
        }

        let sourceStride = CVPixelBufferGetBytesPerRowOfPlane(source, sourcePlane)
        let destinationStride = CVPixelBufferGetBytesPerRowOfPlane(destination, destinationPlane)
        guard sourceX + bytesPerRow <= sourceStride, bytesPerRow <= destinationStride else {
            throw Error.invalidOutputDimensions
        }

        for row in 0 ..< rows {
            let sourceRow = sourceBaseAddress.advanced(by: (sourceY + row) * sourceStride + sourceX)
            let destinationRow = destinationBaseAddress.advanced(by: row * destinationStride)
            memcpy(destinationRow, sourceRow, bytesPerRow)
        }
    }
}

final class PackedStereoVideoCompositor: NSObject, AVVideoCompositing, @unchecked Sendable {
    var sourcePixelBufferAttributes: [String: any Sendable]? {
        [kCVPixelBufferPixelFormatTypeKey as String: [PackedStereoFrameRenderer.pixelFormat]]
    }

    var requiredPixelBufferAttributesForRenderContext: [String: any Sendable] {
        [kCVPixelBufferPixelFormatTypeKey as String: PackedStereoFrameRenderer.pixelFormat]
    }

    func renderContextChanged(_ newRenderContext: AVVideoCompositionRenderContext) {}

    func cancelAllPendingVideoCompositionRequests() {}

    func startRequest(_ request: AVAsynchronousVideoCompositionRequest) {
        guard let instruction = request.videoCompositionInstruction as? PackedStereoCompositionInstruction,
              let sourceFrame = request.sourceReadOnlyPixelBuffer(byTrackID: instruction.sourceTrackID)
        else {
            request.finish(with: compositorError("The packed stereo source frame or instruction was unavailable."))
            return
        }

        do {
            var leftBuffer = try request.renderContext.makeMutablePixelBuffer()
            var rightBuffer = try request.renderContext.makeMutablePixelBuffer()
            try PackedStereoFrameRenderer.render(
                source: sourceFrame,
                geometry: instruction.geometry,
                eyeOrder: instruction.eyeOrder,
                leftOutput: &leftBuffer,
                rightOutput: &rightBuffer
            )

            request.finish(withComposedTaggedBuffers: [
                CMTaggedDynamicBuffer(
                    tags: PackedStereoComposition.outputBufferDescription[PackedStereoOutput.left.rawValue],
                    content: CVReadOnlyPixelBuffer(leftBuffer)
                ),
                CMTaggedDynamicBuffer(
                    tags: PackedStereoComposition.outputBufferDescription[PackedStereoOutput.right.rawValue],
                    content: CVReadOnlyPixelBuffer(rightBuffer)
                ),
            ])
        } catch {
            request.finish(with: error)
        }
    }

    private func compositorError(_ message: String) -> NSError {
        NSError(
            domain: "com.shinycomputers.bd-to-avp.player.packed-stereo",
            code: 1,
            userInfo: [NSLocalizedDescriptionKey: message]
        )
    }
}

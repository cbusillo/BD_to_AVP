import CoreGraphics
import CoreImage
import CoreVideo
import Foundation
@preconcurrency import Metal
@preconcurrency import MetalFX

enum MetalFXPrototypeContract {
    static let inputWidth = 1_920
    static let inputHeight = 1_080
    static let outputWidth = 3_840
    static let outputHeight = 2_160
    static let sourceBufferLimit = 2
    static let inputBufferLimit = 2
    static let outputBufferLimit = 8
}

enum MetalFXSpatialUpscalerFailure: Error, CustomStringConvertible {
    case unsupported(String)
    case sourcePool(String)
    case outputPoolExhausted
    case outputPool(String)
    case texture(String)
    case command(String)

    var description: String {
        switch self {
        case let .unsupported(message),
             let .sourcePool(message),
             let .outputPool(message),
             let .texture(message),
             let .command(message):
            return message
        case .outputPoolExhausted:
            return "The bounded MetalFX output pool is temporarily exhausted."
        }
    }
}

struct EyePixelBufferPair: ~Copyable {
    var left: CVMutablePixelBuffer
    var right: CVMutablePixelBuffer
}

final class MetalFXSpatialUpscaler {
    static func isSupported() -> Bool {
        guard let device = MTLCreateSystemDefaultDevice(),
              MTLFXSpatialScalerDescriptor.supportsDevice(device)
        else {
            return false
        }
        return makeScalerDescriptor().makeSpatialScaler(device: device) != nil
    }

    static func outputPixelBufferAttributes() -> CVPixelBufferCreationAttributes {
        var attributes = CVPixelBufferCreationAttributes(
            pixelFormatType: CVPixelFormatType(rawValue: kCVPixelFormatType_32BGRA),
            size: CVImageSize(
                width: MetalFXPrototypeContract.outputWidth,
                height: MetalFXPrototypeContract.outputHeight
            ),
            compatibility: [.metalTexture]
        )
        attributes.backing = .ioSurface
        return attributes
    }

    private let commandQueue: any MTLCommandQueue
    private let coreImageContext: CIContext
    private let colorSpace: CGColorSpace
    private let leftScaler: any MTLFXSpatialScaler
    private let rightScaler: any MTLFXSpatialScaler
    private let leftIntermediateOutputTexture: any MTLTexture
    private let rightIntermediateOutputTexture: any MTLTexture
    private let textureCache: CVMetalTextureCache
    private let sourcePool: CVMutablePixelBuffer.Pool
    private let inputPool: CVMutablePixelBuffer.Pool
    private let outputPool: CVMutablePixelBuffer.Pool

    init(outputPool: CVMutablePixelBuffer.Pool) throws {
        guard let device = MTLCreateSystemDefaultDevice() else {
            throw MetalFXSpatialUpscalerFailure.unsupported("Metal is unavailable on this Mac.")
        }
        guard MTLFXSpatialScalerDescriptor.supportsDevice(device) else {
            throw MetalFXSpatialUpscalerFailure.unsupported(
                "This Mac does not support MTLFXSpatialScaler."
            )
        }
        guard let commandQueue = device.makeCommandQueue() else {
            throw MetalFXSpatialUpscalerFailure.unsupported("Metal could not create a command queue.")
        }
        let descriptor = Self.makeScalerDescriptor()
        guard let leftScaler = descriptor.makeSpatialScaler(device: device),
              let rightScaler = descriptor.makeSpatialScaler(device: device)
        else {
            throw MetalFXSpatialUpscalerFailure.unsupported(
                "MetalFX could not create the paired 1080p-to-4K spatial scalers."
            )
        }

        let outputTextureDescriptor = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .bgra8Unorm,
            width: MetalFXPrototypeContract.outputWidth,
            height: MetalFXPrototypeContract.outputHeight,
            mipmapped: false
        )
        outputTextureDescriptor.storageMode = .private
        outputTextureDescriptor.usage = leftScaler.outputTextureUsage.union(rightScaler.outputTextureUsage)
        guard let leftIntermediateOutputTexture = device.makeTexture(descriptor: outputTextureDescriptor),
              let rightIntermediateOutputTexture = device.makeTexture(descriptor: outputTextureDescriptor)
        else {
            throw MetalFXSpatialUpscalerFailure.texture(
                "Metal could not allocate the fixed paired MetalFX output textures."
            )
        }

        var textureCache: CVMetalTextureCache?
        let textureCacheStatus = CVMetalTextureCacheCreate(nil, nil, device, nil, &textureCache)
        guard textureCacheStatus == kCVReturnSuccess, let textureCache else {
            throw MetalFXSpatialUpscalerFailure.texture(
                "Core Video could not create the Metal texture cache (status \(textureCacheStatus))."
            )
        }

        var sourceAttributes = CVPixelBufferCreationAttributes(
            pixelFormatType: CVPixelFormatType(rawValue: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange),
            size: CVImageSize(
                width: MetalFXPrototypeContract.inputWidth,
                height: MetalFXPrototypeContract.inputHeight
            )
        )
        sourceAttributes.backing = .ioSurface
        let sourcePool: CVMutablePixelBuffer.Pool
        do {
            sourcePool = try CVMutablePixelBuffer.Pool(
                pixelBufferAttributes: sourceAttributes,
                configuration: .init(
                    ageOutDuration: 60,
                    minimumBufferCount: MetalFXPrototypeContract.sourceBufferLimit
                )
            )
        } catch {
            throw MetalFXSpatialUpscalerFailure.sourcePool(
                "Core Video could not create the bounded 1080p source pool: \(error)"
            )
        }

        var inputAttributes = CVPixelBufferCreationAttributes(
            pixelFormatType: CVPixelFormatType(rawValue: kCVPixelFormatType_32BGRA),
            size: CVImageSize(
                width: MetalFXPrototypeContract.inputWidth,
                height: MetalFXPrototypeContract.inputHeight
            ),
            compatibility: [.metalTexture]
        )
        inputAttributes.backing = .ioSurface
        let inputPool: CVMutablePixelBuffer.Pool
        do {
            inputPool = try CVMutablePixelBuffer.Pool(
                pixelBufferAttributes: inputAttributes,
                configuration: .init(
                    ageOutDuration: 60,
                    minimumBufferCount: MetalFXPrototypeContract.inputBufferLimit
                )
            )
        } catch {
            throw MetalFXSpatialUpscalerFailure.sourcePool(
                "Core Video could not create the bounded BGRA conversion pool: \(error)"
            )
        }

        guard let colorSpace = CGColorSpace(name: CGColorSpace.itur_709) else {
            throw MetalFXSpatialUpscalerFailure.unsupported(
                "Core Graphics could not create the ITU-R BT.709 color space."
            )
        }

        self.commandQueue = commandQueue
        coreImageContext = CIContext(mtlCommandQueue: commandQueue)
        self.colorSpace = colorSpace
        self.leftScaler = leftScaler
        self.rightScaler = rightScaler
        self.leftIntermediateOutputTexture = leftIntermediateOutputTexture
        self.rightIntermediateOutputTexture = rightIntermediateOutputTexture
        self.textureCache = textureCache
        self.sourcePool = sourcePool
        self.inputPool = inputPool
        self.outputPool = outputPool
    }

    func makeSourcePair() throws -> EyePixelBufferPair {
        do {
            return EyePixelBufferPair(
                left: try sourcePool.makeMutablePixelBuffer(
                    .init(allocationThreshold: MetalFXPrototypeContract.sourceBufferLimit)
                ),
                right: try sourcePool.makeMutablePixelBuffer(
                    .init(allocationThreshold: MetalFXPrototypeContract.sourceBufferLimit)
                )
            )
        } catch {
            throw MetalFXSpatialUpscalerFailure.sourcePool(
                "The bounded MetalFX source pool could not vend both eye buffers: \(error)"
            )
        }
    }

    func upscalePair(
        _ sources: borrowing EyePixelBufferPair,
        chromaLocation: CFString
    ) throws -> EyePixelBufferPair {
        let outputs = try makeOutputPair()
        let inputs = try makeInputPair()
        let leftImage = sources.left.withUnsafeBuffer { CIImage(cvPixelBuffer: $0) }
        let rightImage = sources.right.withUnsafeBuffer { CIImage(cvPixelBuffer: $0) }

        let inputBounds = CGRect(
            x: 0,
            y: 0,
            width: MetalFXPrototypeContract.inputWidth,
            height: MetalFXPrototypeContract.inputHeight
        )
        inputs.left.withUnsafeBuffer { unsafeBuffer in
            coreImageContext.render(leftImage, to: unsafeBuffer, bounds: inputBounds, colorSpace: colorSpace)
        }
        inputs.right.withUnsafeBuffer { unsafeBuffer in
            coreImageContext.render(rightImage, to: unsafeBuffer, bounds: inputBounds, colorSpace: colorSpace)
        }
        let inputTextureUsage = leftScaler.colorTextureUsage.union(rightScaler.colorTextureUsage)
        let leftInputTexture = try makeTexture(
            for: inputs.left,
            width: MetalFXPrototypeContract.inputWidth,
            height: MetalFXPrototypeContract.inputHeight,
            usage: inputTextureUsage,
            role: "input"
        )
        let rightInputTexture = try makeTexture(
            for: inputs.right,
            width: MetalFXPrototypeContract.inputWidth,
            height: MetalFXPrototypeContract.inputHeight,
            usage: inputTextureUsage,
            role: "input"
        )
        let leftOutputTexture = try makeTexture(
            for: outputs.left,
            width: MetalFXPrototypeContract.outputWidth,
            height: MetalFXPrototypeContract.outputHeight,
            usage: .shaderRead,
            role: "output"
        )
        let rightOutputTexture = try makeTexture(
            for: outputs.right,
            width: MetalFXPrototypeContract.outputWidth,
            height: MetalFXPrototypeContract.outputHeight,
            usage: .shaderRead,
            role: "output"
        )

        guard let commandBuffer = commandQueue.makeCommandBuffer() else {
            throw MetalFXSpatialUpscalerFailure.command("Metal could not create an upscale command buffer.")
        }
        commandBuffer.label = "MV-HEVC paired-eye MetalFX upscale"

        leftScaler.inputContentWidth = MetalFXPrototypeContract.inputWidth
        leftScaler.inputContentHeight = MetalFXPrototypeContract.inputHeight
        leftScaler.colorTexture = leftInputTexture.texture
        leftScaler.outputTexture = leftIntermediateOutputTexture
        leftScaler.encode(commandBuffer: commandBuffer)
        try encodeOutputCopy(
            commandBuffer: commandBuffer,
            source: leftIntermediateOutputTexture,
            destination: leftOutputTexture.texture
        )

        rightScaler.inputContentWidth = MetalFXPrototypeContract.inputWidth
        rightScaler.inputContentHeight = MetalFXPrototypeContract.inputHeight
        rightScaler.colorTexture = rightInputTexture.texture
        rightScaler.outputTexture = rightIntermediateOutputTexture
        rightScaler.encode(commandBuffer: commandBuffer)
        try encodeOutputCopy(
            commandBuffer: commandBuffer,
            source: rightIntermediateOutputTexture,
            destination: rightOutputTexture.texture
        )

        withExtendedLifetime(
            (
                leftInputTexture.backing,
                rightInputTexture.backing,
                leftOutputTexture.backing,
                rightOutputTexture.backing
            )
        ) {
            commandBuffer.commit()
            commandBuffer.waitUntilCompleted()
        }
        guard commandBuffer.status == .completed, commandBuffer.error == nil else {
            let detail = commandBuffer.error?.localizedDescription ?? "unknown Metal command failure"
            throw MetalFXSpatialUpscalerFailure.command("MetalFX paired-eye upscale failed: \(detail)")
        }

        try validateOutput(outputs.left)
        try validateOutput(outputs.right)
        applyColorAttachments(to: outputs.left, chromaLocation: chromaLocation)
        applyColorAttachments(to: outputs.right, chromaLocation: chromaLocation)
        CVMetalTextureCacheFlush(textureCache, 0)
        return outputs
    }

    private static func makeScalerDescriptor() -> MTLFXSpatialScalerDescriptor {
        let descriptor = MTLFXSpatialScalerDescriptor()
        descriptor.inputWidth = MetalFXPrototypeContract.inputWidth
        descriptor.inputHeight = MetalFXPrototypeContract.inputHeight
        descriptor.outputWidth = MetalFXPrototypeContract.outputWidth
        descriptor.outputHeight = MetalFXPrototypeContract.outputHeight
        descriptor.colorTextureFormat = .bgra8Unorm
        descriptor.outputTextureFormat = .bgra8Unorm
        descriptor.colorProcessingMode = .perceptual
        return descriptor
    }

    private func makeInputPair() throws -> EyePixelBufferPair {
        do {
            return EyePixelBufferPair(
                left: try inputPool.makeMutablePixelBuffer(
                    .init(allocationThreshold: MetalFXPrototypeContract.inputBufferLimit)
                ),
                right: try inputPool.makeMutablePixelBuffer(
                    .init(allocationThreshold: MetalFXPrototypeContract.inputBufferLimit)
                )
            )
        } catch {
            throw MetalFXSpatialUpscalerFailure.sourcePool(
                "The bounded BGRA conversion pool could not vend both eye buffers: \(error)"
            )
        }
    }

    private func makeOutputPair() throws -> EyePixelBufferPair {
        do {
            return EyePixelBufferPair(
                left: try outputPool.makeMutablePixelBuffer(
                    .init(allocationThreshold: MetalFXPrototypeContract.outputBufferLimit)
                ),
                right: try outputPool.makeMutablePixelBuffer(
                    .init(allocationThreshold: MetalFXPrototypeContract.outputBufferLimit)
                )
            )
        } catch let error as CVError where error == .wouldExceedAllocationThreshold {
            throw MetalFXSpatialUpscalerFailure.outputPoolExhausted
        } catch {
            throw MetalFXSpatialUpscalerFailure.outputPool(
                "The bounded MetalFX output pool could not vend both eye buffers: \(error)"
            )
        }
    }

    private func makeTexture(
        for pixelBuffer: borrowing CVMutablePixelBuffer,
        width: Int,
        height: Int,
        usage: MTLTextureUsage,
        role: String
    ) throws -> (backing: CVMetalTexture, texture: any MTLTexture) {
        try pixelBuffer.withUnsafeBuffer { unsafeBuffer in
            var metalTexture: CVMetalTexture?
            let textureAttributes = [
                kCVMetalTextureUsage: NSNumber(value: usage.rawValue)
            ] as CFDictionary
            let status = CVMetalTextureCacheCreateTextureFromImage(
                nil,
                textureCache,
                unsafeBuffer,
                textureAttributes,
                .bgra8Unorm,
                width,
                height,
                0,
                &metalTexture
            )
            guard status == kCVReturnSuccess,
                  let metalTexture,
                  let texture = CVMetalTextureGetTexture(metalTexture)
            else {
                throw MetalFXSpatialUpscalerFailure.texture(
                    "Core Video could not map the MetalFX \(role) buffer as a texture (status \(status))."
                )
            }
            guard texture.width == width,
                  texture.height == height,
                  texture.pixelFormat == .bgra8Unorm
            else {
                throw MetalFXSpatialUpscalerFailure.texture(
                    "Core Video returned a MetalFX \(role) texture with unexpected dimensions or format."
                )
            }
            return (metalTexture, texture)
        }
    }

    private func encodeOutputCopy(
        commandBuffer: any MTLCommandBuffer,
        source: any MTLTexture,
        destination: any MTLTexture
    ) throws {
        guard let blitEncoder = commandBuffer.makeBlitCommandEncoder() else {
            throw MetalFXSpatialUpscalerFailure.command(
                "Metal could not create the MetalFX output copy encoder."
            )
        }
        blitEncoder.copy(from: source, to: destination)
        blitEncoder.endEncoding()
    }

    private func validateOutput(_ pixelBuffer: borrowing CVMutablePixelBuffer) throws {
        guard pixelBuffer.size.width == MetalFXPrototypeContract.outputWidth,
              pixelBuffer.size.height == MetalFXPrototypeContract.outputHeight,
              pixelBuffer.pixelFormatType.rawValue == kCVPixelFormatType_32BGRA
        else {
            throw MetalFXSpatialUpscalerFailure.outputPool(
                "MetalFX produced an output buffer with unexpected dimensions or pixel format."
            )
        }
    }

    private func applyColorAttachments(
        to pixelBuffer: borrowing CVMutablePixelBuffer,
        chromaLocation: CFString
    ) {
        pixelBuffer.withUnsafeBuffer { unsafeBuffer in
            CVBufferSetAttachment(
                unsafeBuffer,
                kCVImageBufferColorPrimariesKey,
                kCVImageBufferColorPrimaries_ITU_R_709_2,
                .shouldPropagate
            )
            CVBufferSetAttachment(
                unsafeBuffer,
                kCVImageBufferTransferFunctionKey,
                kCVImageBufferTransferFunction_ITU_R_709_2,
                .shouldPropagate
            )
            CVBufferSetAttachment(
                unsafeBuffer,
                kCVImageBufferYCbCrMatrixKey,
                kCVImageBufferYCbCrMatrix_ITU_R_709_2,
                .shouldPropagate
            )
            CVBufferSetAttachment(
                unsafeBuffer,
                kCVImageBufferChromaLocationTopFieldKey,
                chromaLocation,
                .shouldPropagate
            )
        }
    }
}

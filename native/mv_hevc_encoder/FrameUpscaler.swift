import Foundation
import CoreVideo
import Metal
import MetalFX
import VideoToolbox

enum FrameUpscaleMode: String {
    case metalFX = "metalfx"
    case pixelTransfer = "pixel-transfer"
}

enum FrameUpscaleContract {
    static let inputWidth = 1_920
    static let inputHeight = 1_080
    static let outputWidth = 3_840
    static let outputHeight = 2_160
    static let sourceBufferLimit = 4
    static let convertedInputBufferLimit = 2
    static let outputBufferLimit = 4
}

enum FrameUpscalerFailure: Error, CustomStringConvertible {
    case invalidDimensions(String)
    case unsupported(String)
    case allocation(String)
    case pixelTransfer(String)
    case metal(String)
    case cancelled

    var description: String {
        switch self {
        case let .invalidDimensions(message),
             let .unsupported(message),
             let .allocation(message),
             let .pixelTransfer(message),
             let .metal(message):
            return message
        case .cancelled:
            return "Frame upscaling cancelled."
        }
    }
}

struct FrameUpscaleMetrics {
    let metalDeviceFinalAllocatedSizeBytes: UInt64
    let metalDeviceInitialAllocatedSizeBytes: UInt64
    let metalDevicePeakAllocatedSizeBytes: UInt64

    var metalDevicePeakDeltaBytes: UInt64 {
        metalDevicePeakAllocatedSizeBytes - metalDeviceInitialAllocatedSizeBytes
    }
}

private final class BoundedPixelBufferPool {
    private let pool: CVMutablePixelBuffer.Pool
    private let allocationAttributes: CVMutablePixelBuffer.Pool.AllocationAttributes
    private let label: String

    init(
        pixelFormat: OSType,
        width: Int,
        height: Int,
        minimumBufferCount: Int,
        allocationThreshold: Int,
        metalCompatible: Bool,
        label: String
    ) throws {
        self.label = label
        var pixelBufferAttributes = CVPixelBufferCreationAttributes(
            pixelFormatType: CVPixelFormatType(rawValue: pixelFormat),
            size: CVImageSize(width: width, height: height),
            compatibility: metalCompatible ? [.metalTexture] : []
        )
        pixelBufferAttributes.backing = .ioSurface
        do {
            pool = try CVMutablePixelBuffer.Pool(
                pixelBufferAttributes: pixelBufferAttributes,
                configuration: .init(
                    ageOutDuration: 0,
                    minimumBufferCount: minimumBufferCount
                )
            )
        } catch {
            throw FrameUpscalerFailure.allocation(
                "Could not create \(label) pixel-buffer pool: \(error)"
            )
        }
        allocationAttributes = .init(allocationThreshold: allocationThreshold)
    }

    func makePixelBuffer(
        waitForAvailability: Bool = false,
        isCancellationRequested: () -> Bool = { false }
    ) throws -> CVMutablePixelBuffer {
        let deadline = ContinuousClock.now.advanced(by: .seconds(30))
        while true {
            if isCancellationRequested() {
                throw FrameUpscalerFailure.cancelled
            }
            do {
                return try pool.makeMutablePixelBuffer(allocationAttributes)
            } catch {
                let errorCode = (error as NSError).code
                guard waitForAvailability,
                      errorCode == Int(kCVReturnWouldExceedAllocationThreshold),
                      ContinuousClock.now < deadline
                else {
                    throw FrameUpscalerFailure.allocation(
                        "Could not allocate bounded \(label) pixel buffer: \(error)"
                    )
                }
                Thread.sleep(forTimeInterval: 0.002)
            }
        }
    }
}

final class SourcePixelBufferPool {
    private let pool: BoundedPixelBufferPool

    init(width: Int, height: Int) throws {
        pool = try BoundedPixelBufferPool(
            pixelFormat: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
            width: width,
            height: height,
            minimumBufferCount: 2,
            allocationThreshold: FrameUpscaleContract.sourceBufferLimit,
            metalCompatible: false,
            label: "source NV12"
        )
    }

    func makePixelBuffer() throws -> CVMutablePixelBuffer {
        try pool.makePixelBuffer()
    }
}

private final class EyeFrameUpscaler {
    private let mode: FrameUpscaleMode
    private let inputWidth: Int
    private let inputHeight: Int
    private let outputWidth: Int
    private let outputHeight: Int
    private let transferSession: VTPixelTransferSession
    private let convertedInputPool: BoundedPixelBufferPool?
    private let outputPool: BoundedPixelBufferPool
    private let commandQueue: MTLCommandQueue?
    private let spatialScaler: MTLFXSpatialScaler?
    private let intermediateOutputTexture: MTLTexture?
    private let textureCache: CVMetalTextureCache?
    private var processedFrames = 0

    init(
        mode: FrameUpscaleMode,
        inputWidth: Int,
        inputHeight: Int,
        outputWidth: Int,
        outputHeight: Int,
        metalDevice: MTLDevice?
    ) throws {
        guard inputWidth > 0, inputHeight > 0, outputWidth > inputWidth, outputHeight > inputHeight else {
            throw FrameUpscalerFailure.invalidDimensions(
                "Upscaling requires positive output dimensions larger than the input."
            )
        }
        guard Double(outputWidth) / Double(inputWidth) == Double(outputHeight) / Double(inputHeight) else {
            throw FrameUpscalerFailure.invalidDimensions(
                "Upscaling requires the same horizontal and vertical scale factor."
            )
        }

        self.mode = mode
        self.inputWidth = inputWidth
        self.inputHeight = inputHeight
        self.outputWidth = outputWidth
        self.outputHeight = outputHeight

        var createdTransferSession: VTPixelTransferSession?
        let transferStatus = VTPixelTransferSessionCreate(
            allocator: nil,
            pixelTransferSessionOut: &createdTransferSession
        )
        guard transferStatus == noErr, let createdTransferSession else {
            throw FrameUpscalerFailure.unsupported(
                "Could not create VideoToolbox pixel-transfer session (status \(transferStatus))."
            )
        }
        transferSession = createdTransferSession
        try Self.setTransferProperty(
            transferSession,
            key: kVTPixelTransferPropertyKey_ScalingMode,
            value: kVTScalingMode_Normal
        )
        try Self.setTransferProperty(
            transferSession,
            key: kVTPixelTransferPropertyKey_RealTime,
            value: kCFBooleanFalse
        )
        try Self.setTransferProperty(
            transferSession,
            key: kVTPixelTransferPropertyKey_DestinationColorPrimaries,
            value: kCVImageBufferColorPrimaries_ITU_R_709_2
        )
        try Self.setTransferProperty(
            transferSession,
            key: kVTPixelTransferPropertyKey_DestinationTransferFunction,
            value: kCVImageBufferTransferFunction_ITU_R_709_2
        )
        try Self.setTransferProperty(
            transferSession,
            key: kVTPixelTransferPropertyKey_DestinationYCbCrMatrix,
            value: kCVImageBufferYCbCrMatrix_ITU_R_709_2
        )

        outputPool = try BoundedPixelBufferPool(
            pixelFormat: kCVPixelFormatType_32BGRA,
            width: outputWidth,
            height: outputHeight,
            minimumBufferCount: 2,
            allocationThreshold: FrameUpscaleContract.outputBufferLimit,
            metalCompatible: true,
            label: "4K BGRA output"
        )

        if mode == .metalFX {
            convertedInputPool = try BoundedPixelBufferPool(
                pixelFormat: kCVPixelFormatType_32BGRA,
                width: inputWidth,
                height: inputHeight,
                minimumBufferCount: 1,
                allocationThreshold: FrameUpscaleContract.convertedInputBufferLimit,
                metalCompatible: true,
                label: "MetalFX BGRA input"
            )
            guard let device = metalDevice else {
                throw FrameUpscalerFailure.unsupported("MetalFX upscaling requires a Metal device.")
            }
            guard MTLFXSpatialScalerDescriptor.supportsDevice(device) else {
                throw FrameUpscalerFailure.unsupported("The default Metal device does not support MetalFX spatial scaling.")
            }
            guard let createdCommandQueue = device.makeCommandQueue() else {
                throw FrameUpscalerFailure.metal("Could not create Metal command queue.")
            }
            commandQueue = createdCommandQueue

            let descriptor = MTLFXSpatialScalerDescriptor()
            descriptor.colorTextureFormat = .bgra8Unorm
            descriptor.outputTextureFormat = .bgra8Unorm
            descriptor.inputWidth = inputWidth
            descriptor.inputHeight = inputHeight
            descriptor.outputWidth = outputWidth
            descriptor.outputHeight = outputHeight
            descriptor.colorProcessingMode = .perceptual
            guard let createdSpatialScaler = descriptor.makeSpatialScaler(device: device) else {
                throw FrameUpscalerFailure.unsupported("Could not create MetalFX spatial scaler.")
            }
            createdSpatialScaler.inputContentWidth = inputWidth
            createdSpatialScaler.inputContentHeight = inputHeight
            spatialScaler = createdSpatialScaler

            let outputDescriptor = MTLTextureDescriptor.texture2DDescriptor(
                pixelFormat: .bgra8Unorm,
                width: outputWidth,
                height: outputHeight,
                mipmapped: false
            )
            outputDescriptor.storageMode = .private
            outputDescriptor.usage = createdSpatialScaler.outputTextureUsage.union([.shaderRead, .renderTarget])
            guard let createdIntermediateTexture = device.makeTexture(descriptor: outputDescriptor) else {
                throw FrameUpscalerFailure.metal("Could not create private MetalFX output texture.")
            }
            guard createdIntermediateTexture.storageMode == .private else {
                throw FrameUpscalerFailure.metal("MetalFX output texture is not private.")
            }
            intermediateOutputTexture = createdIntermediateTexture

            var createdTextureCache: CVMetalTextureCache?
            let cacheStatus = CVMetalTextureCacheCreate(nil, nil, device, nil, &createdTextureCache)
            guard cacheStatus == kCVReturnSuccess, let createdTextureCache else {
                throw FrameUpscalerFailure.metal(
                    "Could not create CoreVideo Metal texture cache (status \(cacheStatus))."
                )
            }
            textureCache = createdTextureCache
        } else {
            convertedInputPool = nil
            commandQueue = nil
            spatialScaler = nil
            intermediateOutputTexture = nil
            textureCache = nil
        }
    }

    func upscale(
        _ source: CVReadOnlyPixelBuffer,
        isCancellationRequested: () -> Bool
    ) throws -> CVReadOnlyPixelBuffer {
        try source.withUnsafeBuffer { unsafeSource in
            guard CVPixelBufferGetPixelFormatType(unsafeSource)
                == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
            else {
                throw FrameUpscalerFailure.unsupported(
                    "Frame upscaling requires video-range NV12 source buffers."
                )
            }
            guard CVPixelBufferGetWidth(unsafeSource) == inputWidth,
                  CVPixelBufferGetHeight(unsafeSource) == inputHeight
            else {
                throw FrameUpscalerFailure.invalidDimensions(
                    "Source pixel-buffer dimensions do not match the configured scaler input."
                )
            }

            let output = try outputPool.makePixelBuffer(
                waitForAvailability: true,
                isCancellationRequested: isCancellationRequested
            )
            try output.withUnsafeBuffer { unsafeOutput in
                switch mode {
                case .pixelTransfer:
                    try transfer(unsafeSource, to: unsafeOutput)
                case .metalFX:
                    guard let convertedInputPool else {
                        throw FrameUpscalerFailure.metal("MetalFX input pool is unavailable.")
                    }
                    let convertedInput = try convertedInputPool.makePixelBuffer()
                    try convertedInput.withUnsafeBuffer { unsafeConvertedInput in
                        try transfer(unsafeSource, to: unsafeConvertedInput)
                        try encodeMetalFX(input: unsafeConvertedInput, output: unsafeOutput)
                    }
                }
                CVBufferPropagateAttachments(unsafeSource, unsafeOutput)
            }
            processedFrames += 1
            if processedFrames.isMultiple(of: 120), let textureCache {
                CVMetalTextureCacheFlush(textureCache, 0)
            }
            return CVReadOnlyPixelBuffer(consume output)
        }
    }

    private static func setTransferProperty(
        _ session: VTPixelTransferSession,
        key: CFString,
        value: CFTypeRef
    ) throws {
        let status = VTSessionSetProperty(session, key: key, value: value)
        guard status == noErr else {
            throw FrameUpscalerFailure.pixelTransfer(
                "Could not configure VideoToolbox pixel transfer (status \(status))."
            )
        }
    }

    private func transfer(_ source: CVPixelBuffer, to destination: CVPixelBuffer) throws {
        let status = VTPixelTransferSessionTransferImage(transferSession, from: source, to: destination)
        guard status == noErr else {
            throw FrameUpscalerFailure.pixelTransfer(
                "VideoToolbox pixel transfer failed (status \(status))."
            )
        }
    }

    private func encodeMetalFX(input: CVPixelBuffer, output: CVPixelBuffer) throws {
        guard let commandQueue, let spatialScaler, let intermediateOutputTexture, let textureCache else {
            throw FrameUpscalerFailure.metal("MetalFX scaler state is unavailable.")
        }
        var inputTextureReference: CVMetalTexture?
        let inputStatus = CVMetalTextureCacheCreateTextureFromImage(
            nil,
            textureCache,
            input,
            nil,
            .bgra8Unorm,
            inputWidth,
            inputHeight,
            0,
            &inputTextureReference
        )
        guard inputStatus == kCVReturnSuccess,
              let inputTextureReference,
              let inputTexture = CVMetalTextureGetTexture(inputTextureReference)
        else {
            throw FrameUpscalerFailure.metal(
                "Could not create MetalFX input texture (CoreVideo status \(inputStatus))."
            )
        }

        var outputTextureReference: CVMetalTexture?
        let outputStatus = CVMetalTextureCacheCreateTextureFromImage(
            nil,
            textureCache,
            output,
            nil,
            .bgra8Unorm,
            outputWidth,
            outputHeight,
            0,
            &outputTextureReference
        )
        guard outputStatus == kCVReturnSuccess,
              let outputTextureReference,
              let outputTexture = CVMetalTextureGetTexture(outputTextureReference)
        else {
            throw FrameUpscalerFailure.metal(
                "Could not create MetalFX output texture (CoreVideo status \(outputStatus))."
            )
        }

        guard let commandBuffer = commandQueue.makeCommandBuffer() else {
            throw FrameUpscalerFailure.metal("Could not create Metal command buffer.")
        }
        spatialScaler.colorTexture = inputTexture
        spatialScaler.outputTexture = intermediateOutputTexture
        spatialScaler.encode(commandBuffer: commandBuffer)
        guard let blitEncoder = commandBuffer.makeBlitCommandEncoder() else {
            throw FrameUpscalerFailure.metal("Could not create Metal blit encoder.")
        }
        blitEncoder.copy(from: intermediateOutputTexture, to: outputTexture)
        blitEncoder.endEncoding()
        commandBuffer.commit()
        commandBuffer.waitUntilCompleted()
        if let error = commandBuffer.error {
            throw FrameUpscalerFailure.metal("MetalFX command failed: \(error.localizedDescription)")
        }
        _ = inputTextureReference
        _ = outputTextureReference
    }
}

final class StereoFrameUpscaler {
    let outputWidth: Int
    let outputHeight: Int
    private let left: EyeFrameUpscaler
    private let metalDevice: MTLDevice?
    private let metalDeviceInitialAllocatedSizeBytes: UInt64
    private var metalDevicePeakAllocatedSizeBytes: UInt64
    private let right: EyeFrameUpscaler

    init(mode: FrameUpscaleMode, inputWidth: Int, inputHeight: Int) throws {
        guard inputWidth == FrameUpscaleContract.inputWidth,
              inputHeight == FrameUpscaleContract.inputHeight
        else {
            throw FrameUpscalerFailure.unsupported(
                "2x frame upscaling requires each SBS eye to be "
                    + "\(FrameUpscaleContract.inputWidth)x\(FrameUpscaleContract.inputHeight); "
                    + "received \(inputWidth)x\(inputHeight)."
            )
        }
        outputWidth = FrameUpscaleContract.outputWidth
        outputHeight = FrameUpscaleContract.outputHeight
        let sharedMetalDevice: MTLDevice?
        if mode == .metalFX {
            guard let device = MTLCreateSystemDefaultDevice() else {
                throw FrameUpscalerFailure.unsupported("MetalFX upscaling requires a Metal device.")
            }
            guard MTLFXSpatialScalerDescriptor.supportsDevice(device) else {
                throw FrameUpscalerFailure.unsupported(
                    "The default Metal device does not support MetalFX spatial scaling."
                )
            }
            sharedMetalDevice = device
        } else {
            sharedMetalDevice = nil
        }
        metalDevice = sharedMetalDevice
        metalDeviceInitialAllocatedSizeBytes = UInt64(sharedMetalDevice?.currentAllocatedSize ?? 0)
        left = try EyeFrameUpscaler(
            mode: mode,
            inputWidth: inputWidth,
            inputHeight: inputHeight,
            outputWidth: outputWidth,
            outputHeight: outputHeight,
            metalDevice: sharedMetalDevice
        )
        right = try EyeFrameUpscaler(
            mode: mode,
            inputWidth: inputWidth,
            inputHeight: inputHeight,
            outputWidth: outputWidth,
            outputHeight: outputHeight,
            metalDevice: sharedMetalDevice
        )
        metalDevicePeakAllocatedSizeBytes = max(
            metalDeviceInitialAllocatedSizeBytes,
            UInt64(sharedMetalDevice?.currentAllocatedSize ?? 0)
        )
    }

    func upscale(
        _ source: [CVReadOnlyPixelBuffer],
        isCancellationRequested: () -> Bool
    ) throws -> [CVReadOnlyPixelBuffer] {
        guard source.count == 2 else {
            throw FrameUpscalerFailure.invalidDimensions("Stereo upscaling requires exactly two eye buffers.")
        }
        recordMetalAllocation()
        let leftOutput = try left.upscale(source[0], isCancellationRequested: isCancellationRequested)
        recordMetalAllocation()
        let rightOutput = try right.upscale(source[1], isCancellationRequested: isCancellationRequested)
        recordMetalAllocation()
        return [leftOutput, rightOutput]
    }

    var metrics: FrameUpscaleMetrics? {
        guard let metalDevice else {
            return nil
        }
        let finalAllocatedSize = UInt64(metalDevice.currentAllocatedSize)
        return FrameUpscaleMetrics(
            metalDeviceFinalAllocatedSizeBytes: finalAllocatedSize,
            metalDeviceInitialAllocatedSizeBytes: metalDeviceInitialAllocatedSizeBytes,
            metalDevicePeakAllocatedSizeBytes: max(metalDevicePeakAllocatedSizeBytes, finalAllocatedSize)
        )
    }

    private func recordMetalAllocation() {
        guard let metalDevice else {
            return
        }
        metalDevicePeakAllocatedSizeBytes = max(
            metalDevicePeakAllocatedSizeBytes,
            UInt64(metalDevice.currentAllocatedSize)
        )
    }
}

func metalFXSpatialScalingSupported() -> Bool {
    guard let device = MTLCreateSystemDefaultDevice() else {
        return false
    }
    return MTLFXSpatialScalerDescriptor.supportsDevice(device)
}

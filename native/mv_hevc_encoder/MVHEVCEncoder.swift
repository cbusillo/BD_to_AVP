import Foundation
@preconcurrency import AVFoundation
import CoreMedia
import CoreVideo
import Darwin
import VideoToolbox

private let videoLayerIDs = [0, 1]
private let viewIDs = [0, 1]
private let leftAndRightViewIDs = [0, 1]

private enum EncoderFailure: Error, CustomStringConvertible {
    case invalidArguments(String)
    case invalidInput(String)
    case unsupported(String)
    case writer(String)
    case cancelled

    var description: String {
        switch self {
        case let .invalidArguments(message), let .invalidInput(message), let .unsupported(message), let .writer(message):
            return message
        case .cancelled:
            return "Encoding cancelled."
        }
    }
}

private struct EncoderOptions {
    let outputURL: URL
    let bitrateMbps: Double
    let quality: Double?
    let fieldOfViewDegrees: Double
    let baselineMillimeters: Double?
    let disparityAdjustment: Double
    let expectedFrameCount: Int?
    let swapEyes: Bool
    let overwrite: Bool

    static func parse(arguments: [String]) throws -> EncoderOptions {
        var outputPath: String?
        var bitrateMbps = 8.0
        var bitrateWasSpecified = false
        var quality: Double?
        var fieldOfViewDegrees = 90.0
        var baselineMillimeters: Double?
        var disparityAdjustment = 0.0
        var expectedFrameCount: Int?
        var swapEyes = false
        var overwrite = false

        var index = 0
        while index < arguments.count {
            let argument = arguments[index]
            switch argument {
            case "--output":
                outputPath = try value(after: argument, arguments: arguments, index: &index)
            case "--bitrate-mbps":
                bitrateMbps = try doubleValue(after: argument, arguments: arguments, index: &index)
                bitrateWasSpecified = true
            case "--quality":
                quality = try doubleValue(after: argument, arguments: arguments, index: &index)
            case "--fov":
                fieldOfViewDegrees = try doubleValue(after: argument, arguments: arguments, index: &index)
            case "--baseline-mm":
                baselineMillimeters = try doubleValue(after: argument, arguments: arguments, index: &index)
            case "--disparity-adjustment":
                disparityAdjustment = try doubleValue(after: argument, arguments: arguments, index: &index)
            case "--expected-frames":
                expectedFrameCount = try integerValue(after: argument, arguments: arguments, index: &index)
            case "--swap-eyes":
                swapEyes = true
            case "--overwrite":
                overwrite = true
            case "--help", "-h":
                printUsage()
                exit(0)
            default:
                throw EncoderFailure.invalidArguments("Unknown argument: \(argument)")
            }
            index += 1
        }

        guard let outputPath, !outputPath.isEmpty else {
            throw EncoderFailure.invalidArguments("--output is required.")
        }
        guard bitrateMbps.isFinite, bitrateMbps > 0, bitrateMbps <= 500 else {
            throw EncoderFailure.invalidArguments("--bitrate-mbps must be greater than 0 and at most 500.")
        }
        if bitrateWasSpecified, quality != nil {
            throw EncoderFailure.invalidArguments("--bitrate-mbps and --quality are mutually exclusive.")
        }
        if let quality, (!quality.isFinite || !(0 ... 1).contains(quality)) {
            throw EncoderFailure.invalidArguments("--quality must be between 0 and 1.")
        }
        guard fieldOfViewDegrees.isFinite, fieldOfViewDegrees > 0, fieldOfViewDegrees <= 180 else {
            throw EncoderFailure.invalidArguments("--fov must be greater than 0 and at most 180 degrees.")
        }
        if let baselineMillimeters,
           (!baselineMillimeters.isFinite || baselineMillimeters <= 0 || baselineMillimeters > 1_000)
        {
            throw EncoderFailure.invalidArguments("--baseline-mm must be greater than 0 and at most 1000.")
        }
        guard disparityAdjustment.isFinite, (-1 ... 1).contains(disparityAdjustment) else {
            throw EncoderFailure.invalidArguments("--disparity-adjustment must be between -1 and 1.")
        }
        if let expectedFrameCount, expectedFrameCount <= 0 {
            throw EncoderFailure.invalidArguments("--expected-frames must be greater than 0.")
        }

        return EncoderOptions(
            outputURL: URL(fileURLWithPath: outputPath).standardizedFileURL,
            bitrateMbps: bitrateMbps,
            quality: quality,
            fieldOfViewDegrees: fieldOfViewDegrees,
            baselineMillimeters: baselineMillimeters,
            disparityAdjustment: disparityAdjustment,
            expectedFrameCount: expectedFrameCount,
            swapEyes: swapEyes,
            overwrite: overwrite
        )
    }

    private static func value(
        after option: String,
        arguments: [String],
        index: inout Int
    ) throws -> String {
        index += 1
        guard index < arguments.count else {
            throw EncoderFailure.invalidArguments("\(option) requires a value.")
        }
        return arguments[index]
    }

    private static func doubleValue(
        after option: String,
        arguments: [String],
        index: inout Int
    ) throws -> Double {
        let rawValue = try value(after: option, arguments: arguments, index: &index)
        guard let parsed = Double(rawValue) else {
            throw EncoderFailure.invalidArguments("\(option) requires a number.")
        }
        return parsed
    }

    private static func integerValue(
        after option: String,
        arguments: [String],
        index: inout Int
    ) throws -> Int {
        let rawValue = try value(after: option, arguments: arguments, index: &index)
        guard let parsed = Int(rawValue) else {
            throw EncoderFailure.invalidArguments("\(option) requires an integer.")
        }
        return parsed
    }

    private static func printUsage() {
        print(
            """
            Usage: mv-hevc-encoder --output FILE [options]

            Reads progressive, 8-bit 4:2:0 side-by-side Y4M from standard input and writes MV-HEVC MOV.

              --output FILE                  Required output MOV path.
              --bitrate-mbps VALUE           Final MV-HEVC average bitrate (default: 8).
              --quality VALUE                Compression quality from 0 through 1; exclusive with bitrate.
              --fov DEGREES                   Horizontal field of view (default: 90).
              --baseline-mm VALUE             Optional constant camera baseline.
              --disparity-adjustment VALUE    Fraction of image width, -1 through 1 (default: 0).
              --expected-frames COUNT         Fail unless exactly this many frames are received.
              --swap-eyes                     Treat the right half as the left eye.
              --overwrite                     Replace an existing output file.
              --capability-probe              Report stereo MV-HEVC encode support and exit.
            """
        )
    }
}

private struct Y4MHeader {
    let frameWidth: Int
    let frameHeight: Int
    let frameRateNumerator: Int32
    let frameRateDenominator: Int32
    let chromaLocation: CFString

    var eyeWidth: Int { frameWidth / 2 }
    var lumaBytes: Int { frameWidth * frameHeight }
    var chromaWidth: Int { frameWidth / 2 }
    var chromaHeight: Int { frameHeight / 2 }
    var chromaBytes: Int { chromaWidth * chromaHeight }
    var frameBytes: Int { lumaBytes + (2 * chromaBytes) }
    var frameRate: Double { Double(frameRateNumerator) / Double(frameRateDenominator) }

    static func parse(_ line: String) throws -> Y4MHeader {
        let fields = line.split(separator: " ")
        guard fields.first == "YUV4MPEG2" else {
            throw EncoderFailure.invalidInput("Input is not a YUV4MPEG2 stream.")
        }

        var width: Int?
        var height: Int?
        var frameRate: (Int32, Int32)?
        var interlace = "?"
        var chroma = ""

        for field in fields.dropFirst() {
            guard let prefix = field.first else { continue }
            let value = String(field.dropFirst())
            switch prefix {
            case "W":
                width = Int(value)
            case "H":
                height = Int(value)
            case "F":
                let components = value.split(separator: ":", maxSplits: 1)
                if components.count == 2,
                   let numerator = Int32(components[0]),
                   let denominator = Int32(components[1])
                {
                    frameRate = (numerator, denominator)
                }
            case "I":
                interlace = value
            case "C":
                chroma = value.lowercased()
            default:
                continue
            }
        }

        guard let width, let height, width > 0, height > 0 else {
            throw EncoderFailure.invalidInput("Y4M width and height are required.")
        }
        guard width.isMultiple(of: 4), height.isMultiple(of: 2) else {
            throw EncoderFailure.invalidInput("Side-by-side Y4M dimensions must be divisible by 4x2.")
        }
        guard let frameRate, frameRate.0 > 0, frameRate.1 > 0 else {
            throw EncoderFailure.invalidInput("Y4M frame rate is required.")
        }
        guard interlace == "p" else {
            throw EncoderFailure.unsupported("Interlaced Y4M must be deinterlaced before direct MV-HEVC encoding.")
        }
        let supportedChromaModes = ["420", "420jpeg", "420mpeg2", "420paldv"]
        guard supportedChromaModes.contains(chroma) else {
            throw EncoderFailure.unsupported("Direct MV-HEVC encoding currently requires 8-bit 4:2:0 Y4M.")
        }
        let chromaLocation = chroma.hasPrefix("420mpeg2")
            ? kCVImageBufferChromaLocation_Left
            : kCVImageBufferChromaLocation_Center

        return Y4MHeader(
            frameWidth: width,
            frameHeight: height,
            frameRateNumerator: frameRate.0,
            frameRateDenominator: frameRate.1,
            chromaLocation: chromaLocation
        )
    }
}

private final class BufferedStandardInput {
    private var buffer = Data()
    private var offset = 0
    private var reachedEOF = false

    func readLine(maximumBytes: Int = 4_096) throws -> String? {
        while true {
            if let newlineIndex = buffer[offset...].firstIndex(of: 0x0A) {
                let lineData = buffer[offset ..< newlineIndex]
                offset = buffer.index(after: newlineIndex)
                compactIfNeeded()
                guard let line = String(data: lineData, encoding: .utf8) else {
                    throw EncoderFailure.invalidInput("Y4M header is not valid UTF-8.")
                }
                return line
            }
            if buffer.count - offset > maximumBytes {
                throw EncoderFailure.invalidInput("Y4M header line exceeds the supported limit.")
            }
            if reachedEOF {
                if buffer.count == offset {
                    return nil
                }
                throw EncoderFailure.invalidInput("Y4M stream ended in an incomplete header line.")
            }
            try readMore()
        }
    }

    func readExactly(_ count: Int) throws -> Data {
        while buffer.count - offset < count, !reachedEOF {
            try readMore()
        }
        guard buffer.count - offset >= count else {
            throw EncoderFailure.invalidInput("Y4M stream ended in an incomplete frame.")
        }
        let end = offset + count
        let data = Data(buffer[offset ..< end])
        offset = end
        compactIfNeeded()
        return data
    }

    private func readMore() throws {
        var data = Data(count: 65_536)
        while true {
            let bytesRead = data.withUnsafeMutableBytes { bytes in
                Darwin.read(STDIN_FILENO, bytes.baseAddress, bytes.count)
            }
            if bytesRead == 0 {
                reachedEOF = true
                return
            }
            if bytesRead > 0 {
                data.removeSubrange(bytesRead ..< data.count)
                buffer.append(data)
                return
            }
            if errno != EINTR {
                throw EncoderFailure.invalidInput("Failed to read Y4M input from standard input.")
            }
        }
    }

    private func compactIfNeeded() {
        if offset > 1_048_576 || offset == buffer.count {
            buffer.removeSubrange(0 ..< offset)
            offset = 0
        }
    }
}

private final class CancellationFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false

    func request() {
        lock.lock()
        cancelled = true
        lock.unlock()
    }

    var isRequested: Bool {
        lock.lock()
        defer { lock.unlock() }
        return cancelled
    }
}

private final class SignalCancellation {
    private let sources: [DispatchSourceSignal]

    init(flag: CancellationFlag) {
        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        sources = [SIGINT, SIGTERM].map { signalNumber in
            let source = DispatchSource.makeSignalSource(signal: signalNumber, queue: .global())
            source.setEventHandler {
                flag.request()
                emitStatus(["event": "encoder.cancellation_requested", "schema_version": 1])
            }
            source.resume()
            return source
        }
    }
}

private func makeCompressionProperties(options: EncoderOptions, header: Y4MHeader) throws -> [String: Any] {
    var properties: [String: Any] = [
        kVTCompressionPropertyKey_MVHEVCVideoLayerIDs as String: videoLayerIDs,
        kVTCompressionPropertyKey_MVHEVCViewIDs as String: viewIDs,
        kVTCompressionPropertyKey_MVHEVCLeftAndRightViewIDs as String: leftAndRightViewIDs,
        kVTCompressionPropertyKey_HasLeftStereoEyeView as String: true,
        kVTCompressionPropertyKey_HasRightStereoEyeView as String: true,
        kVTCompressionPropertyKey_HeroEye as String: kVTHeroEye_Left,
        kVTCompressionPropertyKey_ProjectionKind as String: kCMFormatDescriptionProjectionKind_Rectilinear,
        kVTCompressionPropertyKey_HorizontalFieldOfView as String: UInt32((options.fieldOfViewDegrees * 1_000).rounded()),
        kVTCompressionPropertyKey_HorizontalDisparityAdjustment as String: Int32(
            (options.disparityAdjustment * 10_000).rounded()
        ),
        kVTCompressionPropertyKey_ExpectedFrameRate as String: header.frameRate,
        kVTCompressionPropertyKey_ProfileLevel as String: kVTProfileLevel_HEVC_Main_AutoLevel,
        kVTCompressionPropertyKey_AllowFrameReordering as String: true,
    ]
    if let quality = options.quality {
        properties[kVTCompressionPropertyKey_Quality as String] = quality
    } else {
        properties[kVTCompressionPropertyKey_AverageBitRate as String] = Int(
            (options.bitrateMbps * 1_000_000).rounded()
        )
    }
    if let baselineMillimeters = options.baselineMillimeters {
        properties[kVTCompressionPropertyKey_StereoCameraBaseline as String] = UInt32(
            (baselineMillimeters * 1_000).rounded()
        )
    }
    return properties
}

private func makeOutputSettings(options: EncoderOptions, header: Y4MHeader) throws -> [String: Any] {
    [
        AVVideoCodecKey: AVVideoCodecType.hevc,
        AVVideoWidthKey: header.eyeWidth,
        AVVideoHeightKey: header.frameHeight,
        AVVideoCompressionPropertiesKey: try makeCompressionProperties(options: options, header: header),
        AVVideoColorPropertiesKey: [
            AVVideoColorPrimariesKey: AVVideoColorPrimaries_ITU_R_709_2,
            AVVideoTransferFunctionKey: AVVideoTransferFunction_ITU_R_709_2,
            AVVideoYCbCrMatrixKey: AVVideoYCbCrMatrix_ITU_R_709_2,
        ],
    ]
}

private func isStereoMVHEVCOutputConfigurationSupported() throws -> Bool {
    guard VTIsStereoMVHEVCEncodeSupported() else {
        return false
    }
    let header = Y4MHeader(
        frameWidth: 3_840,
        frameHeight: 1_080,
        frameRateNumerator: 24_000,
        frameRateDenominator: 1_001,
        chromaLocation: kCVImageBufferChromaLocation_Center
    )
    for quality in [Double?.none, 0.7] {
        let probeURL = FileManager.default.temporaryDirectory.appendingPathComponent(
            ".mv-hevc-capability-\(UUID().uuidString).mov"
        )
        defer { try? FileManager.default.removeItem(at: probeURL) }
        let options = EncoderOptions(
            outputURL: probeURL,
            bitrateMbps: 8.0,
            quality: quality,
            fieldOfViewDegrees: 90.0,
            baselineMillimeters: nil,
            disparityAdjustment: 0.0,
            expectedFrameCount: nil,
            swapEyes: false,
            overwrite: true
        )
        let outputSettings = try makeOutputSettings(options: options, header: header)
        let writer = try AVAssetWriter(outputURL: probeURL, fileType: .mov)
        if !writer.canApply(outputSettings: outputSettings, forMediaType: .video) {
            return false
        }
    }
    return true
}

private func fillPixelBuffer(
    _ pixelBuffer: inout CVMutablePixelBuffer,
    header: Y4MHeader,
    frame: Data,
    eyeIndex: Int
) throws {
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
            header.chromaLocation,
            .shouldPropagate
        )
    }

    let lumaEyeOffset = eyeIndex * header.eyeWidth
    let chromaEyeOffset = eyeIndex * (header.eyeWidth / 2)
    try pixelBuffer.accessUnsafeMutableRawPlaneBytes { planes in
        guard planes.count == 2 else {
            throw EncoderFailure.writer("Allocated pixel buffer has an unexpected plane layout.")
        }
        let lumaDestination = planes[0].bytes.baseAddress!.assumingMemoryBound(to: UInt8.self)
        let chromaDestination = planes[1].bytes.baseAddress!.assumingMemoryBound(to: UInt8.self)
        let lumaDestinationStride = planes[0].properties.bytesPerRow
        let chromaDestinationStride = planes[1].properties.bytesPerRow

        try frame.withUnsafeBytes { rawFrame in
            guard let frameBase = rawFrame.baseAddress?.assumingMemoryBound(to: UInt8.self) else {
                throw EncoderFailure.invalidInput("Y4M frame has no readable bytes.")
            }
            let lumaSource = frameBase
            let uSource = frameBase.advanced(by: header.lumaBytes)
            let vSource = uSource.advanced(by: header.chromaBytes)

            for row in 0 ..< header.frameHeight {
                memcpy(
                    lumaDestination.advanced(by: row * lumaDestinationStride),
                    lumaSource.advanced(by: (row * header.frameWidth) + lumaEyeOffset),
                    header.eyeWidth
                )
            }
            let eyeChromaWidth = header.eyeWidth / 2
            for row in 0 ..< header.chromaHeight {
                let destinationRow = chromaDestination.advanced(by: row * chromaDestinationStride)
                let sourceRowOffset = (row * header.chromaWidth) + chromaEyeOffset
                for column in 0 ..< eyeChromaWidth {
                    destinationRow[column * 2] = uSource[sourceRowOffset + column]
                    destinationRow[(column * 2) + 1] = vSource[sourceRowOffset + column]
                }
            }
        }
    }
}

private func makePixelBuffer(
    header: Y4MHeader,
    frame: Data,
    eyeIndex: Int
) throws -> CVReadOnlyPixelBuffer {
    var attributes = CVPixelBufferCreationAttributes(
        pixelFormatType: CVPixelFormatType(rawValue: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange),
        size: CVImageSize(width: header.eyeWidth, height: header.frameHeight)
    )
    attributes.backing = .ioSurface
    var pixelBuffer = try CVMutablePixelBuffer(attributes)
    try fillPixelBuffer(&pixelBuffer, header: header, frame: frame, eyeIndex: eyeIndex)
    return CVReadOnlyPixelBuffer(pixelBuffer)
}

private func makeTaggedBuffers(
    header: Y4MHeader,
    frame: Data,
    swapEyes: Bool
) throws -> [CMTaggedDynamicBuffer] {
    let sourceIndices = swapEyes ? [1, 0] : [0, 1]
    let eyes: [CMStereoViewComponents] = [.leftEye, .rightEye]
    return try zip(videoLayerIDs, zip(eyes, sourceIndices)).map { layerID, eyeAndIndex in
        let (eye, sourceIndex) = eyeAndIndex
        let pixelBuffer = try makePixelBuffer(header: header, frame: frame, eyeIndex: sourceIndex)
        return CMTaggedDynamicBuffer(
            tags: [.videoLayerID(Int64(layerID)), .stereoView(eye)],
            content: pixelBuffer
        )
    }
}

private func writerFailure(_ writer: AVAssetWriter, fallback: String) -> EncoderFailure {
    .writer(writer.error?.localizedDescription ?? fallback)
}

private func emitStatus(_ values: [String: Any]) {
    guard var data = try? JSONSerialization.data(withJSONObject: values, options: [.sortedKeys]) else {
        return
    }
    data.append(0x0A)
    try? FileHandle.standardError.write(contentsOf: data)
}

private func encode(options: EncoderOptions, cancellationFlag: CancellationFlag) async throws -> (Y4MHeader, Int) {
    guard VTIsStereoMVHEVCEncodeSupported() else {
        throw EncoderFailure.unsupported("This Mac does not report stereo MV-HEVC encode support.")
    }
    if cancellationFlag.isRequested || Task.isCancelled {
        throw EncoderFailure.cancelled
    }

    let fileManager = FileManager.default
    let outputExists = fileManager.fileExists(atPath: options.outputURL.path)
    if outputExists {
        guard options.overwrite else {
            throw EncoderFailure.invalidArguments("Output already exists; pass --overwrite to replace it.")
        }
    }
    try fileManager.createDirectory(
        at: options.outputURL.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )

    let partialURL = options.outputURL.deletingLastPathComponent().appendingPathComponent(
        ".\(options.outputURL.lastPathComponent).partial-\(UUID().uuidString)"
    )
    var completed = false
    defer {
        if !completed {
            try? fileManager.removeItem(at: partialURL)
        }
    }

    let input = BufferedStandardInput()
    guard let headerLine = try input.readLine() else {
        throw EncoderFailure.invalidInput("Y4M input is empty.")
    }
    let header = try Y4MHeader.parse(headerLine)
    let outputSettings = try makeOutputSettings(options: options, header: header)
    let writer = try AVAssetWriter(outputURL: partialURL, fileType: .mov)
    guard writer.canApply(outputSettings: outputSettings, forMediaType: .video) else {
        throw EncoderFailure.unsupported("The MV-HEVC output settings are not supported on this Mac.")
    }
    let writerInput = AVAssetWriterInput(mediaType: .video, outputSettings: outputSettings)
    writerInput.expectsMediaDataInRealTime = false
    let receiver = writer.inputTaggedPixelBufferGroupReceiver(for: writerInput, pixelBufferAttributes: nil)

    do {
        try writer.start()
        writer.startSession(atSourceTime: .zero)
        emitStatus(["event": "encoder.ready", "schema_version": 1])
        var frameCount = 0
        while let frameHeader = try input.readLine() {
            guard frameHeader == "FRAME" || frameHeader.hasPrefix("FRAME ") else {
                throw EncoderFailure.invalidInput("Expected a Y4M FRAME header.")
            }
            if cancellationFlag.isRequested || Task.isCancelled {
                throw EncoderFailure.cancelled
            }
            if let expectedFrameCount = options.expectedFrameCount, frameCount >= expectedFrameCount {
                throw EncoderFailure.invalidInput(
                    "Expected \(expectedFrameCount) frames but received more."
                )
            }
            let frame = try input.readExactly(header.frameBytes)
            let taggedBuffers = try makeTaggedBuffers(header: header, frame: frame, swapEyes: options.swapEyes)
            let presentationTime = CMTime(
                value: CMTimeValue(frameCount) * CMTimeValue(header.frameRateDenominator),
                timescale: CMTimeScale(header.frameRateNumerator)
            )

            while try !receiver.appendImmediately(taggedBuffers, with: presentationTime) {
                if cancellationFlag.isRequested || Task.isCancelled {
                    throw EncoderFailure.cancelled
                }
                if writer.status == .failed {
                    throw writerFailure(writer, fallback: "MV-HEVC writer failed while waiting for input capacity.")
                }
                try await Task.sleep(for: .milliseconds(2))
            }
            frameCount += 1
            if frameCount == 1 || frameCount.isMultiple(of: 120) {
                emitStatus([
                    "event": "encoder.progress",
                    "frame_count": frameCount,
                    "schema_version": 1,
                ])
            }
        }

        if let expectedFrameCount = options.expectedFrameCount, frameCount != expectedFrameCount {
            throw EncoderFailure.invalidInput(
                "Expected \(expectedFrameCount) frames but received \(frameCount)."
            )
        }
        guard frameCount > 0 else {
            throw EncoderFailure.invalidInput("Y4M input contains no frames.")
        }

        receiver.finish()
        await writer.finishWriting()
        guard writer.status == .completed else {
            throw writerFailure(writer, fallback: "MV-HEVC writer did not complete successfully.")
        }
        if !options.overwrite, fileManager.fileExists(atPath: options.outputURL.path) {
            throw EncoderFailure.writer("Output appeared while encoding; refusing to replace it without --overwrite.")
        }
        let renameStatus = partialURL.path.withCString { sourcePath in
            options.outputURL.path.withCString { destinationPath in
                Darwin.rename(sourcePath, destinationPath)
            }
        }
        guard renameStatus == 0 else {
            throw EncoderFailure.writer("Failed to atomically finalize the MV-HEVC output.")
        }
        completed = true
        return (header, frameCount)
    } catch {
        writer.cancelWriting()
        throw error
    }
}

@main
private struct MVHEVCEncoder {
    static func main() async {
        let cancellationFlag = CancellationFlag()
        let signalCancellation = SignalCancellation(flag: cancellationFlag)
        defer { _ = signalCancellation }
        do {
            let arguments = Array(CommandLine.arguments.dropFirst())
            if arguments == ["--capability-probe"] {
                let supported = try isStereoMVHEVCOutputConfigurationSupported()
                let data = try JSONSerialization.data(
                    withJSONObject: [
                        "schema_version": 1,
                        "stereo_mv_hevc_encode_supported": supported,
                    ],
                    options: [.sortedKeys]
                )
                print(String(decoding: data, as: UTF8.self))
                if !supported {
                    exit(2)
                }
                return
            }
            let options = try EncoderOptions.parse(arguments: arguments)
            let (header, frameCount) = try await encode(
                options: options,
                cancellationFlag: cancellationFlag
            )
            var summary: [String: Any] = [
                "eye_height": header.frameHeight,
                "eye_width": header.eyeWidth,
                "field_of_view_degrees": options.fieldOfViewDegrees,
                "frame_count": frameCount,
                "frame_rate_denominator": header.frameRateDenominator,
                "frame_rate_numerator": header.frameRateNumerator,
                "has_camera_baseline": options.baselineMillimeters != nil,
                "schema_version": 1,
                "swapped_eyes": options.swapEyes,
            ]
            if let quality = options.quality {
                summary["quality"] = quality
                summary["rate_control"] = "quality"
            } else {
                summary["bitrate_mbps"] = options.bitrateMbps
                summary["rate_control"] = "average_bitrate"
            }
            let data = try JSONSerialization.data(withJSONObject: summary, options: [.sortedKeys])
            print(String(decoding: data, as: UTF8.self))
        } catch EncoderFailure.cancelled {
            fputs("error: Encoding cancelled.\n", stderr)
            exit(130)
        } catch {
            fputs("error: \(error)\n", stderr)
            exit(1)
        }
    }
}

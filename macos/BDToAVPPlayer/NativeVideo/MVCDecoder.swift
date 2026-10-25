import Edge264MVC
import Foundation

enum MVCDecoderError: Error, Equatable {
    case openFailed
    case unsupportedFormat
    case outOfMemory
    case stalled
    case failed(Int32)

    init(code: Int32) {
        switch code {
        case Int32(BDAVP_MVC_UNSUPPORTED_FORMAT): self = .unsupportedFormat
        case Int32(BDAVP_MVC_NO_MEMORY): self = .outOfMemory
        case Int32(BDAVP_MVC_STALLED): self = .stalled
        default: self = .failed(code)
        }
    }
}

/// Decodes Blu-ray 3D MVC access units with edge264-mvc (Edge264MVC.framework). Not thread-safe;
/// one thread owns it. Frames arrive in display order, each carrying the `ticks` value of the
/// access unit that coded it, and their planes are valid only inside the callback.
final class MVCDecoder {
    /// Returns false to stop decoding.
    typealias FrameHandler = (UnsafePointer<BDAVPMVCFrame>) -> Bool

    private let decoder: OpaquePointer

    /// `threads` follows edge264: 0 lets it choose from the CPU count.
    init(threads: Int32 = 0) throws {
        guard let decoder = bdavp_mvc_open(threads) else { throw MVCDecoderError.openFailed }
        self.decoder = decoder
    }

    deinit {
        bdavp_mvc_close(decoder)
    }

    static var version: String { String(cString: bdavp_mvc_version()) }

    var statistics: BDAVPMVCStatistics { bdavp_mvc_statistics(decoder) }

    /// Decodes one access unit. Returns false when the handler stopped decoding.
    @discardableResult
    func decode(_ unit: Data, ticks: Int64, onFrame: FrameHandler) throws -> Bool {
        try Self.deliver(onFrame) { emit, context in
            unit.withUnsafeBytes { bytes in
                bdavp_mvc_decode(decoder, bytes.bindMemory(to: UInt8.self).baseAddress, bytes.count, ticks, emit, context)
            }
        }
    }

    /// Ends the stream and delivers every frame still held.
    @discardableResult
    func finish(onFrame: FrameHandler) throws -> Bool {
        try Self.deliver(onFrame) { emit, context in bdavp_mvc_finish(decoder, emit, context) }
    }

    /// Drops held pictures; decoding resumes at the next IDR.
    func flush() {
        bdavp_mvc_flush(decoder)
    }

    private static func deliver(
        _ handler: FrameHandler,
        _ call: (bdavp_mvc_frame_fn, UnsafeMutableRawPointer) -> Int32
    ) throws -> Bool {
        try withoutActuallyEscaping(handler) { handler in
            var box = handler
            let result = withUnsafeMutablePointer(to: &box) { pointer in
                call({ context, frame in
                    guard let context, let frame else { return 1 }
                    return context.assumingMemoryBound(to: FrameHandler.self).pointee(frame) ? 0 : 1
                }, UnsafeMutableRawPointer(pointer))
            }
            switch result {
            case Int32(BDAVP_MVC_OK): return true
            case Int32(BDAVP_MVC_STOPPED): return false
            default: throw MVCDecoderError(code: result)
            }
        }
    }
}

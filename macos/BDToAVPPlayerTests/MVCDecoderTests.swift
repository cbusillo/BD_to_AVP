import CoreMedia
import Edge264MVC
import XCTest
@testable import BDToAVPPlayer

/// The fixture is edge264-mvc's synthetic conformance stream `mvc_display_ordering.264` (BSD,
/// from tests/conformance/mvc-synthetic at the pinned revision): a 16x16 Stereo High stream
/// whose base view is coded in decode order POC 0, 2, 6, 4, twice. Every base-view picture is a
/// flat luma of 16 + 8 * POC, and every dependent-view picture 24 + 8 * POC.
final class MVCDecoderTests: XCTestCase {
    private struct Output: Equatable {
        let ticks: Int64
        let baseLuma: UInt8
        let dependentLuma: UInt8
    }

    func testDecodesBothViewsInDisplayOrderWithEachUnitsTicks() throws {
        let units = try Self.accessUnits()
        XCTAssertEqual(units.count, 8)
        let decodeOrderPOC: [Int64] = [0, 2, 6, 4, 0, 2, 6, 4]
        let decoder = try MVCDecoder(threads: 1)
        var outputs: [Output] = []
        let collect: MVCDecoder.FrameHandler = { frame in
            outputs.append(Output(
                ticks: frame.pointee.pts,
                baseLuma: frame.pointee.planes.0.0![0],
                dependentLuma: frame.pointee.planes.1.0![0]
            ))
            XCTAssertEqual(frame.pointee.flags & UInt32(BDAVP_MVC_DEPENDENT_MISSING), 0)
            return true
        }
        for (index, unit) in units.enumerated() {
            // Ticks name the picture: 100 per IDR sequence plus its POC.
            try decoder.decode(unit, ticks: Int64(index / 4) * 100 + decodeOrderPOC[index], onFrame: collect)
        }
        try decoder.finish(onFrame: collect)

        var expected: [Output] = []
        for sequence: Int64 in [0, 100] {
            for poc: Int64 in [0, 2, 4, 6] {
                expected.append(Output(ticks: sequence + poc, baseLuma: UInt8(16 + 8 * poc), dependentLuma: UInt8(24 + 8 * poc)))
            }
        }
        XCTAssertEqual(outputs, expected)
        XCTAssertEqual(decoder.statistics.frames, 8)
        XCTAssertEqual(decoder.statistics.frames_without_dependent, 0)
    }

    func testHandlerCanStopDecoding() throws {
        let decoder = try MVCDecoder(threads: 1)
        var delivered = 0
        var completed = true
        for unit in try Self.accessUnits() where completed {
            completed = try decoder.decode(unit, ticks: 0) { _ in
                delivered += 1
                return false
            }
        }
        XCTAssertFalse(completed)
        XCTAssertEqual(delivered, 1)
    }

    func testRejects422BeforeDeliveringFramesToTheNV12Consumer() throws {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "edge264-unsupported-422", withExtension: "264"))
        let stream = try Data(contentsOf: url)
        let decoder = try MVCDecoder(threads: 1)
        var delivered = 0
        let collect: MVCDecoder.FrameHandler = { _ in
            delivered += 1
            return true
        }
        XCTAssertThrowsError(try {
            try decoder.decode(stream, ticks: 0, onFrame: collect)
            try decoder.finish(onFrame: collect)
        }()) { error in
            XCTAssertEqual(error as? MVCDecoderError, .unsupportedFormat)
        }
        XCTAssertEqual(delivered, 0)
    }

    func testSampleBufferTagsEachEyeAndHonorsARightEyeBaseView() throws {
        for baseViewIsRightEye in [false, true] {
            let factory = StereoSampleBufferFactory(baseViewIsRightEye: baseViewIsRightEye)
            let decoder = try MVCDecoder(threads: 1)
            var sample: CMSampleBuffer?
            for unit in try Self.accessUnits() where sample == nil {
                try decoder.decode(unit, ticks: 0) { frame in
                    sample = try? factory.sampleBuffer(for: frame, presentationTime: CMTime(value: 3, timescale: 2))
                    return false
                }
            }
            let buffer = try XCTUnwrap(sample)
            XCTAssertEqual(buffer.presentationTimeStamp, CMTime(value: 3, timescale: 2))
            let eyes = try Dictionary(uniqueKeysWithValues: XCTUnwrap(buffer.taggedBuffers).map { tagged -> (String, UInt8) in
                guard case let .pixelBuffer(pixels) = tagged.buffer else { throw XCTSkip("not a pixel buffer") }
                let eye = tagged.tags.contains(.stereoView(.leftEye)) ? "left" : "right"
                return (eye, Self.firstLuma(pixels))
            })
            // The first frame shown is POC 0: base view luma 16, dependent view luma 24.
            XCTAssertEqual(eyes, baseViewIsRightEye ? ["left": 24, "right": 16] : ["left": 16, "right": 24])
        }
    }

    private static func firstLuma(_ buffer: CVPixelBuffer) -> UInt8 {
        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        return CVPixelBufferGetBaseAddressOfPlane(buffer, 0)!.assumingMemoryBound(to: UInt8.self).pointee
    }

    /// Splits the fixture into access units: each starts at an MVC prefix NAL (type 14) once the
    /// unit before it holds a slice, and the parameter sets travel with the first unit.
    static func accessUnits() throws -> [Data] {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "edge264-mvc-display-ordering", withExtension: "264"))
        let bytes = [UInt8](try Data(contentsOf: url))
        var starts: [(offset: Int, type: UInt8)] = []
        var index = 0
        while index + 3 < bytes.count {
            if bytes[index] == 0, bytes[index + 1] == 0, bytes[index + 2] == 1 {
                starts.append((index, bytes[index + 3] & 0x1F))
                index += 3
            } else {
                index += 1
            }
        }
        var units: [Data] = []
        var unitStart = 0
        var hasSlice = false
        for start in starts {
            if start.type == 14, hasSlice {
                units.append(Data(bytes[unitStart..<start.offset]))
                unitStart = start.offset
                hasSlice = false
            }
            if [1, 5, 20].contains(start.type) { hasSlice = true }
        }
        units.append(Data(bytes[unitStart...]))
        return units
    }
}

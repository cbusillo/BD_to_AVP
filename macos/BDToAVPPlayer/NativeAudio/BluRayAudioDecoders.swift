import AudioToolbox
import CoreMedia
import FFmpegDCA
import Foundation

/// Decoded PCM for one or more frames: interleaved 32-bit float, timed on the presentation
/// timeline in 90 kHz ticks.
struct DecodedAudioChunk {
    let startTicks: Int64
    let sampleRate: Int
    let speakers: [AudioSpeaker]
    let samples: [Float]

    var channels: Int { speakers.count }
    var frameCount: Int { channels == 0 ? 0 : samples.count / channels }
    var durationTicks: Int64 {
        Int64(frameCount) * BluRayPresentationTimeline.ticksPerSecond / Int64(max(sampleRate, 1))
    }
    var endTicks: Int64 { startTicks + durationTicks }
}

enum BluRayAudioDecodingError: Error, Equatable {
    case unsupportedCoding(BluRayAudioCoding)
    case decoderUnavailable(String)
    case decodeFailed(String, Int32)
}

/// Decodes one audio track's PES packets to PCM. Not thread-safe; the audio player calls it
/// from its own queue.
protocol BluRayAudioTrackDecoder: AnyObject {
    /// Decodes a PES packet whose first frame starts at `ticks` on the timeline, or right after
    /// the previous packet when `ticks` is nil.
    func decode(_ packet: BluRayPESPacket, ticks: Int64?) throws -> [DecodedAudioChunk]
    func reset()
}

/// What the headset actually plays for a Blu-ray audio coding, under the no-license-fee rule
/// (https://github.com/cbusillo/BD_to_AVP/issues/1112#issuecomment-6391365574).
enum HeadsetAudioPlayback: Equatable, Sendable {
    /// AC-3, or the AC-3 core that every Blu-ray TrueHD track carries; Apple's decoder.
    case dolbyDigital(isTrueHDCore: Bool)
    /// E-AC-3, with its dependent substream; Apple's decoder.
    case dolbyDigitalPlus
    /// The DTS core, through FFmpeg's LGPL `dca` decoder in FFmpegDCA.framework.
    case dtsCore(isHDTrack: Bool)
    case lpcm

    init?(coding: BluRayAudioCoding) {
        switch coding {
        case .lpcm: self = .lpcm
        case .ac3: self = .dolbyDigital(isTrueHDCore: false)
        case .trueHD: self = .dolbyDigital(isTrueHDCore: true)
        case .eac3, .eac3Secondary: self = .dolbyDigitalPlus
        case .dts: self = .dtsCore(isHDTrack: false)
        case .dtsHDHighResolution, .dtsHDMaster: self = .dtsCore(isHDTrack: true)
        // DTS-HD secondary audio is DTS Express, which has no core to decode.
        case .dtsHDSecondary: return nil
        }
    }

    /// The format name in the audio list, describing what plays rather than what is on disc.
    var formatName: String {
        switch self {
        case .dolbyDigital(let isTrueHDCore): isTrueHDCore ? "Dolby Digital core" : "Dolby Digital"
        case .dolbyDigitalPlus: "Dolby Digital Plus"
        case .dtsCore(let isHDTrack): isHDTrack ? "DTS core" : "DTS"
        case .lpcm: "PCM"
        }
    }

    func makeDecoder() throws -> BluRayAudioTrackDecoder {
        switch self {
        case .dolbyDigital(let isTrueHDCore):
            DolbyAudioDecoder(mode: isTrueHDCore ? .trueHDCore : .ac3)
        case .dolbyDigitalPlus:
            DolbyAudioDecoder(mode: .eac3)
        case .dtsCore:
            try DTSCoreDecoder()
        case .lpcm:
            LPCMAudioDecoder()
        }
    }
}

/// Keeps a running presentation time across packets, so frames after the first in a PES
/// packet, and packets without a PTS, get exact times.
struct AudioFrameClock {
    private(set) var nextTicks: Int64?

    mutating func start(for packetTicks: Int64?) -> Int64? {
        if let packetTicks {
            nextTicks = packetTicks
        }
        return nextTicks
    }

    mutating func advance(samples: Int, sampleRate: Int) {
        guard let current = nextTicks, sampleRate > 0 else {
            return
        }
        nextTicks = current + Int64(samples) * BluRayPresentationTimeline.ticksPerSecond / Int64(sampleRate)
    }

    mutating func reset() {
        nextTicks = nil
    }
}

final class LPCMAudioDecoder: BluRayAudioTrackDecoder {
    private var clock = AudioFrameClock()

    func decode(_ packet: BluRayPESPacket, ticks: Int64?) throws -> [DecodedAudioChunk] {
        guard let start = clock.start(for: ticks), let frame = BluRayLPCMFrame(payload: packet.payload),
              frame.samplesPerChannel > 0 else {
            return []
        }
        clock.advance(samples: frame.samplesPerChannel, sampleRate: frame.sampleRate)
        return [DecodedAudioChunk(startTicks: start, sampleRate: frame.sampleRate, speakers: frame.speakers, samples: frame.samples)]
    }

    func reset() {
        clock.reset()
    }
}

final class DTSCoreDecoder: BluRayAudioTrackDecoder {
    private let decoder: OpaquePointer
    private var clock = AudioFrameClock()
    private var output = [Float](repeating: 0, count: 8 * 4096)

    init() throws {
        guard let decoder = bdavp_dca_create() else {
            throw BluRayAudioDecodingError.decoderUnavailable("FFmpeg dca")
        }
        self.decoder = decoder
    }

    deinit {
        bdavp_dca_destroy(decoder)
    }

    /// libavcodec's own license string, from the linked FFmpegDCA.framework.
    static var ffmpegLicense: String { String(cString: bdavp_dca_license()) }
    static var ffmpegVersion: String { String(cString: bdavp_dca_ffmpeg_version()) }

    func decode(_ packet: BluRayPESPacket, ticks: Int64?) throws -> [DecodedAudioChunk] {
        // Blu-ray carries the DTS-HD extension substream in its own PES packets (extension 0x72).
        if let streamIDExtension = packet.streamIDExtension, streamIDExtension != 0x71 {
            return []
        }
        let frames = DTSCoreFrameParser.frames(in: packet.payload)
        guard !frames.isEmpty, var start = clock.start(for: ticks) else {
            return []
        }
        var chunks: [DecodedAudioChunk] = []
        for frame in frames {
            var info = BDAVPDCAFrameInfo()
            let capacity = Int32(output.count)
            let result = frame.bytes.withUnsafeBytes { raw in
                output.withUnsafeMutableBufferPointer { buffer in
                    bdavp_dca_decode(
                        decoder,
                        raw.bindMemory(to: UInt8.self).baseAddress,
                        Int32(raw.count),
                        buffer.baseAddress,
                        capacity,
                        &info
                    )
                }
            }
            if result < 0 {
                throw BluRayAudioDecodingError.decodeFailed("DTS core", result)
            }
            guard result == 0 else {
                continue
            }
            let count = Int(info.samples_per_channel * info.channels)
            chunks.append(
                DecodedAudioChunk(
                    startTicks: start,
                    sampleRate: Int(info.sample_rate),
                    speakers: Self.speakers(mask: info.channel_mask, channels: Int(info.channels)),
                    samples: Array(output[0..<count])
                )
            )
            clock.advance(samples: Int(info.samples_per_channel), sampleRate: Int(info.sample_rate))
            start = clock.nextTicks ?? start
        }
        return chunks
    }

    func reset() {
        bdavp_dca_flush(decoder)
        clock.reset()
    }

    /// FFmpeg's AV_CH_* bits, in FFmpeg's native order.
    static func speakers(mask: UInt64, channels: Int) -> [AudioSpeaker] {
        let bits: [(UInt64, AudioSpeaker)] = [
            (1 << 0, .left), (1 << 1, .right), (1 << 2, .center), (1 << 3, .lfe),
            (1 << 4, .rearLeft), (1 << 5, .rearRight), (1 << 6, .leftCenter), (1 << 7, .rightCenter),
            (1 << 8, .rearCenter), (1 << 9, .leftSurround), (1 << 10, .rightSurround), (1 << 11, .topCenter),
            (1 << 12, .leftTopFront), (1 << 13, .centerTopFront), (1 << 14, .rightTopFront),
            (1 << 15, .leftTopRear), (1 << 16, .centerTopRear), (1 << 17, .rightTopRear),
            (1 << 31, .leftWide), (1 << 32, .rightWide), (1 << 35, .lfe2),
        ]
        let speakers = bits.filter { mask & $0.0 != 0 }.map(\.1)
        if speakers.count == channels {
            return speakers
        }
        return channels == 1 ? [.mono] : Array([.left, .right, .center, .lfe, .leftSurround, .rightSurround].prefix(channels))
    }
}

/// Decodes AC-3 and E-AC-3 with Apple's AudioToolbox decoders.
final class DolbyAudioDecoder: BluRayAudioTrackDecoder {
    enum Mode {
        case ac3
        /// TrueHD tracks: only the AC-3 core, carried in PES packets with extension 0x76.
        case trueHDCore
        /// E-AC-3: an independent substream (often AC-3 itself, PES extension 0x71) plus
        /// dependent E-AC-3 substreams (extension 0x72), decoded together.
        case eac3
    }

    private struct Unit {
        var isEnhanced: Bool
        var startTicks: Int64
        var sampleRate: Int
        var samplesPerFrame: Int
        var speakers: Set<AudioSpeaker>
        var bytes: Data
        var hasDependent: Bool
    }

    private let mode: Mode
    private var clock = AudioFrameClock()
    private var pendingUnit: Unit?
    private var converter: AudioToolboxDolbyConverter?
    /// Where the next decoded sample belongs. AudioToolbox trims its decoder delay from the
    /// first output (288 samples for AC-3), so output runs that far behind the input frames;
    /// timing output by its own count keeps it exact.
    private var outputTicks: Int64?
    private var delayTicks: Int64 = 0

    init(mode: Mode) {
        self.mode = mode
    }

    func decode(_ packet: BluRayPESPacket, ticks: Int64?) throws -> [DecodedAudioChunk] {
        switch mode {
        case .trueHDCore:
            if let streamIDExtension = packet.streamIDExtension, streamIDExtension != 0x76 {
                return []
            }
        case .ac3, .eac3:
            break
        }
        let frames = DolbySyncFrameParser.frames(in: packet.payload)
        guard !frames.isEmpty else {
            return []
        }
        var chunks: [DecodedAudioChunk] = []
        var packetTicks = ticks
        for frame in frames {
            switch frame.kind {
            case .eac3Dependent:
                guard mode == .eac3, pendingUnit != nil else {
                    continue
                }
                pendingUnit?.bytes.append(frame.bytes)
                pendingUnit?.speakers.formUnion(frame.speakers)
                pendingUnit?.hasDependent = true
            case .ac3, .eac3Independent:
                if let unit = pendingUnit {
                    chunks += try decode(unit)
                    pendingUnit = nil
                }
                guard let start = clock.start(for: packetTicks) else {
                    continue
                }
                packetTicks = nil
                clock.advance(samples: frame.samplesPerFrame, sampleRate: frame.sampleRate)
                let unit = Unit(
                    isEnhanced: frame.kind == .eac3Independent,
                    startTicks: start,
                    sampleRate: frame.sampleRate,
                    samplesPerFrame: frame.samplesPerFrame,
                    speakers: frame.speakers,
                    bytes: frame.bytes,
                    hasDependent: false
                )
                if mode == .eac3 {
                    pendingUnit = unit
                } else {
                    chunks += try decode(unit)
                }
            }
        }
        return chunks
    }

    private func decode(_ unit: Unit) throws -> [DecodedAudioChunk] {
        let formatID = unit.isEnhanced || unit.hasDependent ? kAudioFormatEnhancedAC3 : kAudioFormatAC3
        let channels = unit.speakers.count
        let unitTicks = Int64(unit.samplesPerFrame) * BluRayPresentationTimeline.ticksPerSecond / Int64(unit.sampleRate)
        if converter?.matches(formatID: formatID, sampleRate: unit.sampleRate, channels: channels) != true {
            converter = try AudioToolboxDolbyConverter(
                formatID: formatID,
                sampleRate: unit.sampleRate,
                channels: channels,
                framesPerPacket: unit.samplesPerFrame
            )
            outputTicks = nil
        }
        guard let converter else {
            return []
        }
        if let expected = outputTicks.map({ $0 + delayTicks }), abs(unit.startTicks - expected) > unitTicks / 2 {
            // The stream jumped: start over from this frame.
            converter.reset()
            outputTicks = nil
        }
        if outputTicks == nil {
            outputTicks = unit.startTicks
            delayTicks = 0
        }
        guard let decoded = try converter.decode(unit.bytes), let start = outputTicks else {
            delayTicks += unitTicks
            return []
        }
        let chunk = DecodedAudioChunk(startTicks: start, sampleRate: unit.sampleRate, speakers: decoded.speakers, samples: decoded.samples)
        outputTicks = start + chunk.durationTicks
        delayTicks += unitTicks - chunk.durationTicks
        return [chunk]
    }

    func reset() {
        pendingUnit = nil
        clock.reset()
        converter?.reset()
        outputTicks = nil
    }
}

/// AudioConverter from AC-3 or E-AC-3 packets to interleaved Float32.
final class AudioToolboxDolbyConverter {
    private let formatID: AudioFormatID
    private let sampleRate: Int
    private let channels: Int
    private let framesPerPacket: Int
    private let converter: AudioConverterRef
    private var outputSpeakers: [AudioSpeaker]?

    init(formatID: AudioFormatID, sampleRate: Int, channels: Int, framesPerPacket: Int) throws {
        self.formatID = formatID
        self.sampleRate = sampleRate
        self.channels = channels
        self.framesPerPacket = framesPerPacket
        var input = AudioStreamBasicDescription(
            mSampleRate: Float64(sampleRate),
            mFormatID: formatID,
            mFormatFlags: 0,
            mBytesPerPacket: 0,
            mFramesPerPacket: UInt32(framesPerPacket),
            mBytesPerFrame: 0,
            mChannelsPerFrame: UInt32(channels),
            mBitsPerChannel: 0,
            mReserved: 0
        )
        var output = AudioStreamBasicDescription(
            mSampleRate: Float64(sampleRate),
            mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
            mBytesPerPacket: UInt32(4 * channels),
            mFramesPerPacket: 1,
            mBytesPerFrame: UInt32(4 * channels),
            mChannelsPerFrame: UInt32(channels),
            mBitsPerChannel: 32,
            mReserved: 0
        )
        var converter: AudioConverterRef?
        let status = AudioConverterNew(&input, &output, &converter)
        guard status == noErr, let converter else {
            throw BluRayAudioDecodingError.decoderUnavailable(formatID == kAudioFormatEnhancedAC3 ? "E-AC-3 (\(status))" : "AC-3 (\(status))")
        }
        self.converter = converter
    }

    deinit {
        AudioConverterDispose(converter)
    }

    func matches(formatID: AudioFormatID, sampleRate: Int, channels: Int) -> Bool {
        self.formatID == formatID && self.sampleRate == sampleRate && self.channels == channels
    }

    func reset() {
        AudioConverterReset(converter)
    }

    func decode(_ packet: Data) throws -> (speakers: [AudioSpeaker], samples: [Float])? {
        var samples = [Float](repeating: 0, count: framesPerPacket * channels)
        var frames = UInt32(framesPerPacket)
        let input = AudioConverterInput(packet: packet)
        let status = samples.withUnsafeMutableBytes { raw -> OSStatus in
            var bufferList = AudioBufferList(
                mNumberBuffers: 1,
                mBuffers: AudioBuffer(mNumberChannels: UInt32(channels), mDataByteSize: UInt32(raw.count), mData: raw.baseAddress)
            )
            return withExtendedLifetime(input) {
                AudioConverterFillComplexBuffer(
                    converter,
                    audioConverterInputProc,
                    Unmanaged.passUnretained(input).toOpaque(),
                    &frames,
                    &bufferList,
                    nil
                )
            }
        }
        if status != noErr, status != AudioConverterInput.drainedStatus {
            throw BluRayAudioDecodingError.decodeFailed("Dolby", status)
        }
        guard frames > 0 else {
            return nil
        }
        return (speakers: speakers(), samples: Array(samples.prefix(Int(frames) * channels)))
    }

    private func speakers() -> [AudioSpeaker] {
        if let outputSpeakers {
            return outputSpeakers
        }
        var size: UInt32 = 0
        var resolved: [AudioSpeaker] = []
        if AudioConverterGetPropertyInfo(converter, kAudioConverterOutputChannelLayout, &size, nil) == noErr, size > 0 {
            let layout = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: 8)
            defer { layout.deallocate() }
            if AudioConverterGetProperty(converter, kAudioConverterOutputChannelLayout, &size, layout) == noErr {
                resolved = CoreAudioChannelLayout.speakers(layout.assumingMemoryBound(to: AudioChannelLayout.self), size: Int(size))
            }
        }
        if resolved.count != channels {
            resolved = channels == 1 ? [.mono] : Array([AudioSpeaker.left, .right, .center, .lfe, .leftSurround, .rightSurround, .rearLeft, .rearRight].prefix(channels))
        }
        outputSpeakers = resolved
        return resolved
    }
}

private final class AudioConverterInput {
    static let drainedStatus: OSStatus = 0x6472_6169 // 'drai'

    private let packet: Data
    private var consumed = false
    private var storage: UnsafeMutableRawPointer?
    private let description = UnsafeMutablePointer<AudioStreamPacketDescription>.allocate(capacity: 1)

    init(packet: Data) {
        self.packet = packet
    }

    deinit {
        storage?.deallocate()
        description.deallocate()
    }

    func provide(
        _ packetCount: UnsafeMutablePointer<UInt32>,
        _ bufferList: UnsafeMutablePointer<AudioBufferList>,
        _ descriptions: UnsafeMutablePointer<UnsafeMutablePointer<AudioStreamPacketDescription>?>?
    ) -> OSStatus {
        guard !consumed else {
            packetCount.pointee = 0
            return Self.drainedStatus
        }
        consumed = true
        let count = packet.count
        let storage = UnsafeMutableRawPointer.allocate(byteCount: count, alignment: 16)
        packet.copyBytes(to: storage.assumingMemoryBound(to: UInt8.self), count: count)
        self.storage = storage
        bufferList.pointee.mNumberBuffers = 1
        bufferList.pointee.mBuffers.mData = storage
        bufferList.pointee.mBuffers.mDataByteSize = UInt32(count)
        bufferList.pointee.mBuffers.mNumberChannels = 0
        description.pointee = AudioStreamPacketDescription(mStartOffset: 0, mVariableFramesInPacket: 0, mDataByteSize: UInt32(count))
        descriptions?.pointee = description
        packetCount.pointee = 1
        return noErr
    }
}

private func audioConverterInputProc(
    _: AudioConverterRef,
    _ packetCount: UnsafeMutablePointer<UInt32>,
    _ bufferList: UnsafeMutablePointer<AudioBufferList>,
    _ descriptions: UnsafeMutablePointer<UnsafeMutablePointer<AudioStreamPacketDescription>?>?,
    _ userData: UnsafeMutableRawPointer?
) -> OSStatus {
    guard let userData else {
        packetCount.pointee = 0
        return AudioConverterInput.drainedStatus
    }
    return Unmanaged<AudioConverterInput>.fromOpaque(userData).takeUnretainedValue()
        .provide(packetCount, bufferList, descriptions)
}

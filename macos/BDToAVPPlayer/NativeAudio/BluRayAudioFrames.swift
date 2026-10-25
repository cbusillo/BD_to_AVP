import Foundation

/// Speaker positions used to describe decoded audio, independent of any one framework.
enum AudioSpeaker: Hashable, Sendable, CaseIterable {
    case left, right, center, lfe
    case leftSurround, rightSurround
    case rearLeft, rearRight, rearCenter
    case leftCenter, rightCenter
    case leftWide, rightWide
    case topCenter, leftTopFront, centerTopFront, rightTopFront
    case leftTopRear, rightTopRear, centerTopRear
    case lfe2
    case mono
}

/// One AC-3 or E-AC-3 sync frame.
struct DolbySyncFrame: Equatable {
    enum Kind: Equatable {
        case ac3
        case eac3Independent
        case eac3Dependent
    }

    let kind: Kind
    let sampleRate: Int
    let samplesPerFrame: Int
    let speakers: Set<AudioSpeaker>
    let bytes: Data
}

enum DolbySyncFrameParser {
    private static let ac3WordsPerFrame48k: [Int] = [
        64, 64, 80, 80, 96, 96, 112, 112, 128, 128, 160, 160, 192, 192, 224, 224, 256, 256,
        320, 320, 384, 384, 448, 448, 512, 512, 640, 640, 768, 768, 896, 896, 1024, 1024,
        1152, 1152, 1280, 1280,
    ]
    private static let ac3WordsPerFrame44k: [Int] = [
        69, 70, 87, 88, 104, 105, 121, 122, 139, 140, 174, 175, 208, 209, 243, 244, 278, 279,
        348, 349, 417, 418, 487, 488, 557, 558, 696, 697, 835, 836, 975, 976, 1114, 1115,
        1253, 1254, 1393, 1394,
    ]
    private static let ac3WordsPerFrame32k: [Int] = [
        96, 96, 120, 120, 144, 144, 168, 168, 192, 192, 240, 240, 288, 288, 336, 336, 384, 384,
        480, 480, 576, 576, 672, 672, 768, 768, 960, 960, 1152, 1152, 1344, 1344, 1536, 1536,
        1728, 1728, 1920, 1920,
    ]

    /// Splits a PES payload into whole sync frames. Bytes before the first sync word, and a
    /// trailing partial frame, are dropped.
    static func frames(in payload: Data) -> [DolbySyncFrame] {
        let bytes = [UInt8](payload)
        var frames: [DolbySyncFrame] = []
        var offset = 0
        while offset + 7 <= bytes.count {
            guard bytes[offset] == 0x0B, bytes[offset + 1] == 0x77 else {
                offset += 1
                continue
            }
            guard let frame = frame(bytes, at: offset), offset + frame.bytes.count <= bytes.count else {
                offset += 1
                continue
            }
            frames.append(frame)
            offset += frame.bytes.count
        }
        return frames
    }

    private static func frame(_ bytes: [UInt8], at offset: Int) -> DolbySyncFrame? {
        var reader = BitReader(bytes, byteOffset: offset + 2)
        let bsid = Int(bytes[offset + 5] >> 3)
        if bsid <= 8 {
            reader.skip(16) // crc1
            let fscod = reader.read(2)
            let frmsizecod = reader.read(6)
            guard frmsizecod < 38 else {
                return nil
            }
            let sampleRate: Int
            let words: Int
            switch fscod {
            case 0: sampleRate = 48_000; words = ac3WordsPerFrame48k[frmsizecod]
            case 1: sampleRate = 44_100; words = ac3WordsPerFrame44k[frmsizecod]
            case 2: sampleRate = 32_000; words = ac3WordsPerFrame32k[frmsizecod]
            default: return nil
            }
            reader.skip(5 + 3) // bsid, bsmod
            let acmod = reader.read(3)
            if acmod & 0x1 != 0, acmod != 1 { reader.skip(2) } // cmixlev
            if acmod & 0x4 != 0 { reader.skip(2) } // surmixlev
            if acmod == 2 { reader.skip(2) } // dsurmod
            let lfeon = reader.read(1) == 1
            let size = words * 2
            guard offset + size <= bytes.count else {
                return nil
            }
            return DolbySyncFrame(
                kind: .ac3,
                sampleRate: sampleRate,
                samplesPerFrame: 1536,
                speakers: speakers(acmod: acmod, lfe: lfeon),
                bytes: Data(bytes[offset..<(offset + size)])
            )
        }
        guard bsid > 10, bsid <= 16 else {
            return nil
        }
        let strmtyp = reader.read(2)
        reader.skip(3) // substreamid
        let size = (reader.read(11) + 1) * 2
        let fscod = reader.read(2)
        let sampleRate: Int
        let blocks: Int
        if fscod == 3 {
            let fscod2 = reader.read(2)
            guard fscod2 < 3 else {
                return nil
            }
            sampleRate = [24_000, 22_050, 16_000][fscod2]
            blocks = 6
        } else {
            sampleRate = [48_000, 44_100, 32_000][fscod]
            blocks = [1, 2, 3, 6][reader.read(2)]
        }
        let acmod = reader.read(3)
        let lfeon = reader.read(1) == 1
        reader.skip(5) // bsid
        reader.skip(5) // dialnorm
        if reader.read(1) == 1 { reader.skip(8) } // compr
        if acmod == 0 {
            reader.skip(5)
            if reader.read(1) == 1 { reader.skip(8) }
        }
        var frameSpeakers = speakers(acmod: acmod, lfe: lfeon)
        if strmtyp == 1, reader.read(1) == 1 {
            frameSpeakers = chanmapSpeakers(reader.read(16))
        }
        guard strmtyp <= 1 || strmtyp == 2, offset + size <= bytes.count else {
            return nil
        }
        return DolbySyncFrame(
            kind: strmtyp == 1 ? .eac3Dependent : .eac3Independent,
            sampleRate: sampleRate,
            samplesPerFrame: blocks * 256,
            speakers: frameSpeakers,
            bytes: Data(bytes[offset..<(offset + size)])
        )
    }

    static func speakers(acmod: Int, lfe: Bool) -> Set<AudioSpeaker> {
        let table: [[AudioSpeaker]] = [
            [.left, .right], // 1+1 dual mono
            [.center],
            [.left, .right],
            [.left, .center, .right],
            [.left, .right, .rearCenter],
            [.left, .center, .right, .rearCenter],
            [.left, .right, .leftSurround, .rightSurround],
            [.left, .center, .right, .leftSurround, .rightSurround],
        ]
        var result = Set(table[acmod])
        if lfe {
            result.insert(.lfe)
        }
        return result
    }

    private static func chanmapSpeakers(_ chanmap: Int) -> Set<AudioSpeaker> {
        let locations: [[AudioSpeaker]] = [
            [.left], [.center], [.right], [.leftSurround], [.rightSurround],
            [.leftCenter, .rightCenter], [.rearLeft, .rearRight], [.rearCenter], [.topCenter],
            [.leftSurround, .rightSurround], [.leftWide, .rightWide], [.leftTopFront, .rightTopFront],
            [.centerTopFront], [.leftTopRear, .rightTopRear], [.lfe2], [.lfe],
        ]
        var result = Set<AudioSpeaker>()
        for (bit, speakers) in locations.enumerated() where chanmap & (0x8000 >> bit) != 0 {
            result.formUnion(speakers)
        }
        return result
    }
}

/// One DTS core frame, cut from a DTS, DTS-HD High Resolution or DTS-HD Master Audio stream.
/// The DTS-HD extension substream that follows it is not part of the frame.
struct DTSCoreFrame: Equatable {
    let sampleRate: Int
    let samplesPerFrame: Int
    let bytes: Data
}

enum DTSCoreFrameParser {
    static let coreSync: [UInt8] = [0x7F, 0xFE, 0x80, 0x01]
    private static let sampleRates: [Int: Int] = [
        1: 8_000, 2: 16_000, 3: 32_000, 6: 11_025, 7: 22_050, 8: 44_100, 11: 12_000, 12: 24_000, 13: 48_000,
    ]

    /// Returns the core frames in a PES payload. Extension substreams (sync 0x64582025) and
    /// anything else without a core sync word are skipped.
    static func frames(in payload: Data) -> [DTSCoreFrame] {
        let bytes = [UInt8](payload)
        var frames: [DTSCoreFrame] = []
        var offset = 0
        while offset + 10 <= bytes.count {
            guard bytes[offset..<(offset + 4)].elementsEqual(coreSync) else {
                offset += 1
                continue
            }
            var reader = BitReader(bytes, byteOffset: offset + 4)
            reader.skip(1 + 5 + 1) // FTYPE, SHORT, CPF
            let blocks = reader.read(7) + 1
            let size = reader.read(14) + 1
            reader.skip(6) // AMODE
            let frequency = reader.read(4)
            guard size >= 96, offset + size <= bytes.count, let sampleRate = sampleRates[frequency] else {
                offset += 1
                continue
            }
            frames.append(
                DTSCoreFrame(
                    sampleRate: sampleRate,
                    samplesPerFrame: blocks * 32,
                    bytes: Data(bytes[offset..<(offset + size)])
                )
            )
            offset += size
        }
        return frames
    }
}

/// Blu-ray LPCM: a 4-byte header, then big-endian samples. Odd channel counts carry one
/// padding channel.
struct BluRayLPCMFrame: Equatable {
    let sampleRate: Int
    let speakers: [AudioSpeaker]
    /// Interleaved samples in `speakers` order, scaled to -1...1.
    let samples: [Float]

    var samplesPerChannel: Int { speakers.isEmpty ? 0 : samples.count / speakers.count }

    /// Source channel order for each `channel_assignment`, with nil for the padding channel.
    static let layouts: [Int: [AudioSpeaker?]] = [
        1: [.mono, nil],
        3: [.left, .right],
        4: [.left, .right, .center, nil],
        5: [.left, .right, .rearCenter, nil],
        6: [.left, .right, .center, .rearCenter],
        7: [.left, .right, .leftSurround, .rightSurround],
        8: [.left, .right, .center, .leftSurround, .rightSurround, nil],
        9: [.left, .right, .center, .leftSurround, .rightSurround, .lfe],
        10: [.left, .right, .center, .leftSurround, .rearLeft, .rearRight, .rightSurround, nil],
        11: [.left, .right, .center, .leftSurround, .rearLeft, .rearRight, .rightSurround, .lfe],
    ]

    init?(payload: Data) {
        let bytes = [UInt8](payload)
        guard bytes.count >= 4, let layout = Self.layouts[Int(bytes[2] >> 4)] else {
            return nil
        }
        switch bytes[2] & 0x0F {
        case 1: sampleRate = 48_000
        case 4: sampleRate = 96_000
        case 5: sampleRate = 192_000
        default: return nil
        }
        let bits = [0, 16, 20, 24][Int(bytes[3] >> 6)]
        guard bits == 16 || bits == 24 else {
            return nil
        }
        let bytesPerSample = bits / 8
        let frameBytes = bytesPerSample * layout.count
        let frameCount = (bytes.count - 4) / frameBytes
        speakers = layout.compactMap { $0 }
        var output: [Float] = []
        output.reserveCapacity(frameCount * speakers.count)
        for frame in 0..<frameCount {
            let base = 4 + frame * frameBytes
            for (channel, speaker) in layout.enumerated() where speaker != nil {
                let index = base + channel * bytesPerSample
                if bytesPerSample == 2 {
                    let value = Int16(bitPattern: UInt16(bytes[index]) << 8 | UInt16(bytes[index + 1]))
                    output.append(Float(value) / 32_768)
                } else {
                    let unsigned = UInt32(bytes[index]) << 24 | UInt32(bytes[index + 1]) << 16
                        | UInt32(bytes[index + 2]) << 8
                    output.append(Float(Int32(bitPattern: unsigned)) / 2_147_483_648)
                }
            }
        }
        samples = output
    }
}

struct BitReader {
    private let bytes: [UInt8]
    private var bitOffset: Int

    init(_ bytes: [UInt8], byteOffset: Int) {
        self.bytes = bytes
        bitOffset = byteOffset * 8
    }

    mutating func skip(_ count: Int) {
        bitOffset += count
    }

    mutating func read(_ count: Int) -> Int {
        var value = 0
        for _ in 0..<count {
            let byteIndex = bitOffset >> 3
            let bit = byteIndex < bytes.count ? (bytes[byteIndex] >> (7 - UInt8(bitOffset & 7))) & 1 : 0
            value = value << 1 | Int(bit)
            bitOffset += 1
        }
        return value
    }
}

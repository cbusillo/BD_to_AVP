import Foundation

/// Blu-ray `stream_coding_type` values for audio, as listed in a playlist's STN table and
/// a clip's program map.
enum BluRayAudioCoding: UInt8, Sendable, CaseIterable {
    case lpcm = 0x80
    case ac3 = 0x81
    case dts = 0x82
    case trueHD = 0x83
    case eac3 = 0x84
    case dtsHDHighResolution = 0x85
    case dtsHDMaster = 0x86
    case eac3Secondary = 0xA1
    case dtsHDSecondary = 0xA2
}

/// One audio stream of the selected title, as the source reader (#1110) describes it from the
/// playlist: its transport-stream PID, its coding type, and its ISO 639-2 language code.
struct BluRayAudioStreamDescriptor: Equatable, Hashable, Sendable {
    let pid: UInt16
    let coding: BluRayAudioCoding
    let languageCode: String?

    init(pid: UInt16, coding: BluRayAudioCoding, languageCode: String?) {
        self.pid = pid
        self.coding = coding
        self.languageCode = languageCode
    }
}

/// One complete PES packet from the title's transport stream.
///
/// This is the hand-off between the Blu-ray source reader and the headset decoders. The reader
/// delivers every PES packet of every audio PID in the title's play order; the audio player
/// keeps the ones it needs. `pts` is the 33-bit, 90 kHz presentation timestamp from the PES
/// header, when the header carries one. `payload` is the PES payload after the PES header.
struct BluRayPESPacket: Equatable, Sendable {
    let pid: UInt16
    let streamID: UInt8
    /// `stream_id_extension` for `stream_id` 0xFD. A TrueHD PID carries its TrueHD access units
    /// with 0x72 and the embedded AC-3 core with 0x76.
    let streamIDExtension: UInt8?
    let pts: UInt64?
    let payload: Data
}

/// Something that consumes PES packets in stream order: the headset audio player, and the
/// video decoder in #1111. Calls may come from any thread but never concurrently.
protocol BluRayPESPacketSink: AnyObject {
    func receive(_ packet: BluRayPESPacket)
    /// The source jumped, for a seek or a playlist clip change with a timestamp discontinuity.
    /// Drop queued data; packets after this call belong to the new position.
    func discontinuity()
}

/// The presentation clock shared by audio and video. Both map 33-bit PTS values to the same
/// `AVSampleBufferRenderSynchronizer` timebase: time zero is the title's first video PTS.
struct BluRayPresentationTimeline: Equatable, Sendable {
    static let ticksPerSecond: Int64 = 90_000
    static let ptsModulus: Int64 = 1 << 33

    /// The PTS that maps to playback time zero.
    let originPTS: UInt64

    init(originPTS: UInt64) {
        self.originPTS = originPTS & UInt64(Self.ptsModulus - 1)
    }

    /// Ticks from the origin, unwrapping the 33-bit counter to the nearest value around
    /// `nearTicks` so a title that crosses the wrap point keeps counting forward.
    func ticks(for pts: UInt64, near nearTicks: Int64 = 0) -> Int64 {
        let modulus = Self.ptsModulus
        let raw = (Int64(pts & UInt64(modulus - 1)) - Int64(originPTS)) & (modulus - 1)
        let cycles = ((nearTicks - raw) + modulus / 2).floorDivided(by: modulus)
        return raw + cycles * modulus
    }

    func seconds(for pts: UInt64, near nearTicks: Int64 = 0) -> Double {
        Double(ticks(for: pts, near: nearTicks)) / Double(Self.ticksPerSecond)
    }
}

private extension Int64 {
    func floorDivided(by divisor: Int64) -> Int64 {
        let quotient = self / divisor
        return (self % divisor != 0 && (self < 0) != (divisor < 0)) ? quotient - 1 : quotient
    }
}

import Foundation

/// Reassembles PES packets for chosen PIDs from MPEG transport stream bytes. It accepts the
/// 192-byte packets of a Blu-ray M2TS clip, or plain 188-byte packets.
///
/// Bytes may arrive in any chunk size. A PES packet is complete when its declared length is
/// reached, or, for unbounded packets, when the next packet on the same PID starts.
struct TransportStreamPESReader {
    enum PacketSize: Int {
        case transportStream = 188
        case blurayM2TS = 192
    }

    private let packetSize: PacketSize
    private let pids: Set<UInt16>
    private var pending = Data()
    private var assemblies: [UInt16: Data] = [:]

    init(pids: Set<UInt16>, packetSize: PacketSize = .blurayM2TS) {
        self.pids = pids
        self.packetSize = packetSize
    }

    /// Feeds stream bytes and returns the PES packets they complete, in stream order.
    mutating func append(_ bytes: Data) -> [BluRayPESPacket] {
        pending.append(bytes)
        var output: [BluRayPESPacket] = []
        let size = packetSize.rawValue
        let headerOffset = size - 188
        var offset = pending.startIndex
        while pending.endIndex - offset >= size {
            let tsStart = offset + headerOffset
            defer { offset += size }
            guard pending[tsStart] == 0x47 else {
                continue
            }
            let pid = (UInt16(pending[tsStart + 1] & 0x1F) << 8) | UInt16(pending[tsStart + 2])
            guard pids.contains(pid) else {
                continue
            }
            let payloadUnitStart = pending[tsStart + 1] & 0x40 != 0
            let adaptation = (pending[tsStart + 3] >> 4) & 0x3
            var payloadStart = tsStart + 4
            if adaptation & 0x2 != 0 {
                payloadStart += 1 + Int(pending[tsStart + 4])
            }
            let packetEnd = tsStart + 188
            guard adaptation & 0x1 != 0, payloadStart < packetEnd else {
                continue
            }
            let payload = pending[payloadStart..<packetEnd]
            if payloadUnitStart {
                if let previous = assemblies.removeValue(forKey: pid), let packet = Self.parse(pid: pid, previous) {
                    output.append(packet)
                }
                assemblies[pid] = Data(payload)
            } else if assemblies[pid] != nil {
                assemblies[pid]?.append(payload)
            }
            if let assembly = assemblies[pid], let declared = Self.declaredLength(assembly),
               assembly.count >= declared {
                assemblies[pid] = nil
                if let packet = Self.parse(pid: pid, assembly.prefix(declared)) {
                    output.append(packet)
                }
            }
        }
        pending.removeSubrange(pending.startIndex..<offset)
        return output
    }

    /// Returns any unbounded PES packets still being assembled, for the end of the stream.
    mutating func finish() -> [BluRayPESPacket] {
        let packets = assemblies.keys.sorted().compactMap { pid in
            assemblies[pid].flatMap { Self.parse(pid: pid, $0) }
        }
        assemblies.removeAll()
        pending.removeAll()
        return packets
    }

    /// Drops partial data, after a seek.
    mutating func reset() {
        assemblies.removeAll()
        pending.removeAll()
    }

    private static func declaredLength(_ data: Data) -> Int? {
        guard data.count >= 6 else {
            return nil
        }
        let base = data.startIndex
        let length = Int(data[base + 4]) << 8 | Int(data[base + 5])
        return length == 0 ? nil : length + 6
    }

    static func parse(pid: UInt16, _ data: Data) -> BluRayPESPacket? {
        let bytes = [UInt8](data)
        guard bytes.count >= 9, bytes[0] == 0, bytes[1] == 0, bytes[2] == 1 else {
            return nil
        }
        let streamID = bytes[3]
        let flags = bytes[7]
        let headerLength = Int(bytes[8])
        let payloadStart = 9 + headerLength
        guard payloadStart <= bytes.count else {
            return nil
        }
        var cursor = 9
        var pts: UInt64?
        if flags & 0x80 != 0, cursor + 5 <= payloadStart {
            pts = timestamp(bytes, at: cursor)
            cursor += 5
        }
        if flags & 0x40 != 0 { cursor += 5 } // DTS
        if flags & 0x20 != 0 { cursor += 6 } // ESCR
        if flags & 0x10 != 0 { cursor += 3 } // ES rate
        if flags & 0x08 != 0 { cursor += 1 } // trick mode
        if flags & 0x04 != 0 { cursor += 1 } // additional copy info
        if flags & 0x02 != 0 { cursor += 2 } // PES CRC
        var streamIDExtension: UInt8?
        if flags & 0x01 != 0, cursor < payloadStart {
            let extensionFlags = bytes[cursor]
            cursor += 1
            if extensionFlags & 0x80 != 0 { cursor += 16 } // private data
            if extensionFlags & 0x40 != 0, cursor < payloadStart { cursor += 1 + Int(bytes[cursor]) } // pack header
            if extensionFlags & 0x20 != 0 { cursor += 2 } // sequence counter
            if extensionFlags & 0x10 != 0 { cursor += 2 } // P-STD buffer
            if extensionFlags & 0x01 != 0, cursor + 1 < payloadStart {
                let fieldLength = Int(bytes[cursor] & 0x7F)
                if fieldLength > 0, bytes[cursor + 1] & 0x80 == 0 {
                    streamIDExtension = bytes[cursor + 1] & 0x7F
                }
            }
        }
        return BluRayPESPacket(
            pid: pid,
            streamID: streamID,
            streamIDExtension: streamIDExtension,
            pts: pts,
            payload: Data(bytes[payloadStart...])
        )
    }

    private static func timestamp(_ bytes: [UInt8], at index: Int) -> UInt64 {
        (UInt64(bytes[index] >> 1) & 0x7) << 30
            | UInt64(bytes[index + 1]) << 22
            | (UInt64(bytes[index + 2]) >> 1) << 15
            | UInt64(bytes[index + 3]) << 7
            | UInt64(bytes[index + 4]) >> 1
    }
}

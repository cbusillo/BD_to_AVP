import Foundation

/// Random-access bytes, such as a file opened through a security-scoped URL.
protocol ByteSource: AnyObject {
    var size: Int64 { get }
    func read(at offset: Int64, count: Int) throws -> Data
}

final class FileByteSource: ByteSource {
    let size: Int64
    private let handle: FileHandle

    init(url: URL) throws {
        handle = try FileHandle(forReadingFrom: url)
        size = Int64(try handle.seekToEnd())
    }

    deinit {
        try? handle.close()
    }

    func read(at offset: Int64, count: Int) throws -> Data {
        try handle.seek(toOffset: UInt64(offset))
        return try handle.read(upToCount: count) ?? Data()
    }
}

final class DataByteSource: ByteSource {
    let data: Data
    var size: Int64 { Int64(data.count) }

    init(_ data: Data) { self.data = data }

    func read(at offset: Int64, count: Int) throws -> Data {
        let start = Int(min(offset, size))
        return data.subdata(in: start..<min(start + count, data.count))
    }
}

/// A Matroska file whose video track is H.264 MVC, as MakeMKV and 3D remuxes write it: each
/// block holds a frame's base and dependent view NAL units, and the track's `mvcC` block
/// addition mapping carries the MVC parameter sets.
final class MatroskaMVCSource: Bluray3DSource {
    struct Track: Equatable {
        let number: Int
        let type: Int
        let codecID: String
        let codecPrivate: Data
        let language: String?
        let name: String?
        let mvcConfiguration: Data?
    }

    let info: Bluray3DSourceInfo
    let tracks: [Track]

    private let source: ByteSource
    private let timestampScale: Int64
    private let firstCluster: Int64
    private let segmentDataStart: Int64
    private let cuesPosition: Int64?
    private let cueLock = NSLock()
    /// The video track's cue points, read on the first seek: cluster timestamp and the cluster's
    /// offset in the file.
    private var videoCues: [(timestamp: Int64, cluster: Int64)]?

    convenience init(url: URL) throws {
        try self.init(source: FileByteSource(url: url))
    }

    init(source: ByteSource) throws {
        self.source = source
        let header = try MatroskaHeader(source: source)
        tracks = header.tracks
        timestampScale = header.timestampScale
        firstCluster = header.firstCluster
        segmentDataStart = header.segmentDataStart
        cuesPosition = header.cuesPosition
        let video = header.tracks.first { $0.type == 1 && $0.mvcConfiguration != nil }
        let title = Bluray3DTitle(
            id: "matroska",
            playlist: nil,
            durationSeconds: header.durationSeconds,
            isMainFeature: true,
            baseViewIsRightEye: false,
            unsupportedReason: video == nil ? .notStereo3D : nil,
            audioTracks: header.tracks.filter { $0.type == 2 }.map { Self.track($0, kind: .audio) },
            subtitleTracks: header.tracks.filter { $0.type == 17 }.map { Self.track($0, kind: .subtitle) }
        )
        info = Bluray3DSourceInfo(container: .matroska, titles: [title])
    }

    func read(
        title: Bluray3DTitle,
        from startTicks: Int64,
        audioTrackIDs: [Int],
        subtitleTrackIDs: [Int],
        into sink: Bluray3DPacketSink
    ) throws -> Bluray3DReadStatistics {
        if let reason = title.unsupportedReason { throw Bluray3DSourceError.unsupported(reason) }
        guard title.id == "matroska",
              let video = tracks.first(where: { $0.type == 1 && $0.mvcConfiguration != nil })
        else { throw Bluray3DSourceError.titleUnavailable }
        let converter = try MVCAnnexBConverter(avcConfiguration: video.codecPrivate, mvcConfiguration: video.mvcConfiguration)
        let audio = Set(audioTrackIDs)
        let subtitles = Set(subtitleTrackIDs)
        var statistics = Bluray3DReadStatistics()
        let reader = EBMLReader(source: source, offset: startTicks > 0 ? try clusterOffset(at: startTicks, video: video.number) : firstCluster)
        var clusterTimestamp: Int64 = 0
        // Decoding starts at a keyframe; a cluster found by its cue normally starts with one.
        var awaitingKeyframe = startTicks > 0

        func deliver(_ block: MatroskaBlock) throws -> Bool {
            let (timestamp, overflow) = clusterTimestamp.addingReportingOverflow(Int64(block.relativeTimestamp))
            guard !overflow else { throw Bluray3DSourceError.invalidStream("block timestamp out of range") }
            let pts = try Self.pts(timestamp, scale: timestampScale)
            let packet: Bluray3DPacket
            if block.track == video.number {
                guard let frame = block.frames.first else { return true }
                if awaitingKeyframe {
                    guard block.isKeyframe else { return true }
                    awaitingKeyframe = false
                }
                packet = .video(
                    MVCAccessUnit(pts: pts, dts: nil, isRandomAccess: block.isKeyframe, data: try converter.convert(frame))
                )
                statistics.videoAccessUnits += 1
            } else if audio.contains(block.track) || subtitles.contains(block.track) {
                let isAudio = audio.contains(block.track)
                for (index, frame) in block.frames.enumerated() {
                    // Matroska blocks carry bare codec frames; wrap them as private-stream PES packets.
                    // A laced block has one timestamp, so only its first frame carries it, and the
                    // decoder times the rest from the frames before them.
                    let pes = BluRayPESPacket(
                        pid: UInt16(clamping: block.track), streamID: 0xBD, streamIDExtension: nil,
                        pts: index == 0 ? pts : nil, payload: frame
                    )
                    if isAudio { statistics.audioPackets += 1 } else { statistics.subtitlePackets += 1 }
                    if !sink.receive(isAudio ? .audio(pes) : .subtitle(pes)) { return false }
                }
                return true
            } else {
                return true
            }
            return sink.receive(packet)
        }

        while let element = try reader.nextElement() {
            guard element.id == MatroskaID.cluster else {
                try reader.skip(element)
                continue
            }
            let end = element.end
            while let child = try reader.nextElement(endingBefore: end, insideUnknownSize: end == .max) {
                switch child.id {
                case MatroskaID.timestamp:
                    guard let timestamp = Int64(exactly: try reader.unsigned(child)) else {
                        throw Bluray3DSourceError.invalidStream("cluster timestamp out of range")
                    }
                    clusterTimestamp = timestamp
                case MatroskaID.simpleBlock:
                    let block = try MatroskaBlock(try reader.payload(child, limit: EBMLReader.maximumBlockSize), simple: true)
                    if !(try deliver(block)) { return statistics.reading(reader) }
                case MatroskaID.blockGroup:
                    if let block = try reader.blockGroup(child), !(try deliver(block)) {
                        return statistics.reading(reader)
                    }
                default:
                    try reader.skip(child)
                }
            }
        }
        return statistics.reading(reader)
    }

    /// Where to start reading for `ticks`: the last cluster that the video track's cues place at
    /// or before it, or, without cues, the last cluster whose timestamp is at or before it.
    private func clusterOffset(at ticks: Int64, video: Int) throws -> Int64 {
        // Matroska timestamps count `timestampScale` nanoseconds; 90 kHz ticks are 100,000 / 9 ns.
        let target = Int64((Double(ticks) * 100_000 / 9 / Double(timestampScale)).rounded(.down))
        let cues = try cueLock.withLock {
            if videoCues == nil { videoCues = try readCues(video: video) }
            return videoCues ?? []
        }
        if let cue = cues.last(where: { $0.timestamp <= target }) {
            return cue.cluster
        }
        if !cues.isEmpty { return firstCluster }
        return try scanClusters(through: target)
    }

    private func readCues(video: Int) throws -> [(timestamp: Int64, cluster: Int64)] {
        guard let cuesPosition else { return [] }
        let reader = EBMLReader(source: source, offset: cuesPosition)
        guard let cues = try reader.nextElement(), cues.id == MatroskaID.cues, cues.end != .max else { return [] }
        var points: [(timestamp: Int64, cluster: Int64)] = []
        while let point = try reader.nextElement(endingBefore: cues.end) {
            guard point.id == MatroskaID.cuePoint else {
                try reader.skip(point)
                continue
            }
            var time: Int64?
            var cluster: Int64?
            for child in try reader.children(of: point) {
                if child.id == MatroskaID.cueTime { time = Int64(exactly: try reader.unsigned(child)) }
                guard child.id == MatroskaID.cueTrackPositions else { continue }
                let positions = try reader.children(of: child)
                let track = try positions.first { $0.id == MatroskaID.cueTrack }.map { try reader.integer($0) }
                if track == video, let position = try positions.first(where: { $0.id == MatroskaID.cueClusterPosition }) {
                    cluster = Int64(exactly: try reader.unsigned(position)).map { segmentDataStart + $0 }
                }
            }
            reader.offset = point.end
            if let time, let cluster, cluster >= firstCluster, cluster < source.size {
                points.append((time, cluster))
            }
        }
        return points.sorted { $0.timestamp < $1.timestamp }
    }

    /// Reads cluster headers from the first cluster on, for files without cues.
    private func scanClusters(through target: Int64) throws -> Int64 {
        let reader = EBMLReader(source: source, offset: firstCluster)
        var best = firstCluster
        while let element = try reader.nextElement(), element.id == MatroskaID.cluster, element.end != .max {
            let timestamp = try reader.nextElement(endingBefore: element.end)
            guard let timestamp, timestamp.id == MatroskaID.timestamp,
                  let value = Int64(exactly: try reader.unsigned(timestamp)), value <= target
            else { break }
            best = element.start
            reader.offset = element.end
        }
        return best
    }

    /// A Matroska timestamp, scaled to nanoseconds by `scale`, as a 33-bit 90 kHz PTS.
    static func pts(_ timestamp: Int64, scale: Int64) throws -> UInt64 {
        let (nanoseconds, overflow) = timestamp.multipliedReportingOverflow(by: scale)
        guard !overflow else { throw Bluray3DSourceError.invalidStream("block timestamp out of range") }
        // 90 kHz ticks are nanoseconds times 9 / 100,000; split the division so it cannot overflow.
        let ticks = nanoseconds / 100_000 * 9 + nanoseconds % 100_000 * 9 / 100_000
        return UInt64(truncatingIfNeeded: ticks) & ((1 << 33) - 1)
    }

    static func track(_ track: Track, kind: Bluray3DTrackKind) -> Bluray3DTrack {
        Bluray3DTrack(
            id: track.number, kind: kind, codingType: codingType(track.codecID), language: track.language, name: track.name
        )
    }

    /// The Blu-ray coding type whose frames a Matroska codec stores unchanged, or 0. Matroska
    /// PCM has no Blu-ray LPCM header, and Matroska TrueHD has no AC-3 core for the headset to
    /// decode, so both map to 0.
    static func codingType(_ codecID: String) -> UInt8 {
        switch codecID {
        case "A_AC3": return 0x81
        case "A_DTS": return 0x82
        case "A_EAC3": return 0x84
        case "S_HDMV/PGS": return 0x90
        default: return 0
        }
    }
}

private extension Bluray3DReadStatistics {
    func reading(_ reader: EBMLReader) -> Bluray3DReadStatistics {
        var statistics = self
        statistics.bytes = reader.bytesRead
        return statistics
    }
}

enum MatroskaID {
    static let ebml: UInt32 = 0x1A45_DFA3
    static let docType: UInt32 = 0x4282
    static let segment: UInt32 = 0x1853_8067
    static let info: UInt32 = 0x1549_A966
    static let timestampScale: UInt32 = 0x2AD7B1
    static let duration: UInt32 = 0x4489
    static let tracks: UInt32 = 0x1654_AE6B
    static let trackEntry: UInt32 = 0xAE
    static let trackNumber: UInt32 = 0xD7
    static let trackType: UInt32 = 0x83
    static let codecID: UInt32 = 0x86
    static let codecPrivate: UInt32 = 0x63A2
    static let language: UInt32 = 0x22B59C
    static let name: UInt32 = 0x536E
    static let blockAdditionMapping: UInt32 = 0x41E4
    static let blockAddIDType: UInt32 = 0x41E7
    static let blockAddIDExtraData: UInt32 = 0x41ED
    static let cluster: UInt32 = 0x1F43_B675
    static let timestamp: UInt32 = 0xE7
    static let simpleBlock: UInt32 = 0xA3
    static let blockGroup: UInt32 = 0xA0
    static let block: UInt32 = 0xA1
    static let referenceBlock: UInt32 = 0xFB
    static let seekHead: UInt32 = 0x114D_9B74
    static let seek: UInt32 = 0x4DBB
    static let seekID: UInt32 = 0x53AB
    static let seekPosition: UInt32 = 0x53AC
    static let cues: UInt32 = 0x1C53_BB6B
    static let cuePoint: UInt32 = 0xBB
    static let cueTime: UInt32 = 0xB3
    static let cueTrackPositions: UInt32 = 0xB7
    static let cueTrack: UInt32 = 0xF7
    static let cueClusterPosition: UInt32 = 0xF1
    /// `mvcC`, the MVC decoder configuration block addition type.
    static let mvcConfigurationType: UInt64 = 0x6D76_6343
    /// Top-level Segment children; one of these ends a cluster of unknown size.
    static let topLevel: Set<UInt32> = [
        0x114D_9B74, info, tracks, cluster, 0x1C53_BB6B, 0x1043_A770, 0x1254_C367, 0x1941_A469,
    ]
}

/// The Matroska segment header up to its first cluster.
struct MatroskaHeader {
    let tracks: [MatroskaMVCSource.Track]
    let timestampScale: Int64
    let durationSeconds: Double
    let firstCluster: Int64
    /// Where the Segment's data starts; seek and cue positions count from here.
    let segmentDataStart: Int64
    /// The Cues element, from the SeekHead or found before the first cluster.
    let cuesPosition: Int64?

    init(source: ByteSource) throws {
        let reader = EBMLReader(source: source, offset: 0)
        guard let ebml = try? reader.nextElement(), ebml.id == MatroskaID.ebml else {
            throw Bluray3DSourceError.unrecognized
        }
        let docType = try reader.children(of: ebml).first { $0.id == MatroskaID.docType }.map { try reader.string($0) }
        guard docType == "matroska" || docType == "webm" else { throw Bluray3DSourceError.unrecognized }
        reader.offset = ebml.end
        guard let segment = try reader.nextElement(), segment.id == MatroskaID.segment else {
            throw Bluray3DSourceError.unrecognized
        }
        var tracks: [MatroskaMVCSource.Track]?
        var scale: Int64 = 1_000_000
        var duration: Double = 0
        var cues: Int64?
        while let element = try reader.nextElement(endingBefore: segment.end) {
            switch element.id {
            case MatroskaID.cluster:
                guard let tracks else { throw Bluray3DSourceError.invalidStream("tracks follow the first cluster") }
                self.tracks = tracks
                timestampScale = scale
                durationSeconds = duration * Double(scale) / 1_000_000_000
                firstCluster = element.start
                segmentDataStart = segment.dataStart
                cuesPosition = cues
                return
            case MatroskaID.cues:
                cues = element.start
                try reader.skip(element)
            case MatroskaID.seekHead:
                for seek in try reader.children(of: element) where seek.id == MatroskaID.seek {
                    let fields = try reader.children(of: seek)
                    let id = try fields.first { $0.id == MatroskaID.seekID }.map { try reader.unsigned($0) }
                    if id == UInt64(MatroskaID.cues),
                       let position = try fields.first(where: { $0.id == MatroskaID.seekPosition }).map({ try reader.unsigned($0) }),
                       let offset = Int64(exactly: position) {
                        cues = cues ?? segment.dataStart + offset
                    }
                }
                reader.offset = element.end
            case MatroskaID.info:
                for child in try reader.children(of: element) {
                    if child.id == MatroskaID.timestampScale {
                        guard let value = Int64(exactly: try reader.unsigned(child)), value > 0 else {
                            throw Bluray3DSourceError.invalidStream("invalid timestamp scale")
                        }
                        scale = value
                    }
                    if child.id == MatroskaID.duration { duration = try reader.float(child) }
                }
                reader.offset = element.end
            case MatroskaID.tracks:
                tracks = try reader.children(of: element)
                    .filter { $0.id == MatroskaID.trackEntry }
                    .map { try Self.track(reader, $0) }
                reader.offset = element.end
            default:
                try reader.skip(element)
            }
        }
        throw Bluray3DSourceError.invalidStream("no clusters")
    }

    private static func track(_ reader: EBMLReader, _ entry: EBMLElement) throws -> MatroskaMVCSource.Track {
        var number = 0
        var type = 0
        var codecID = ""
        var codecPrivate = Data()
        var language: String? = "eng"
        var name: String?
        var mvcConfiguration: Data?
        for child in try reader.children(of: entry) {
            switch child.id {
            case MatroskaID.trackNumber: number = try reader.integer(child)
            case MatroskaID.trackType: type = try reader.integer(child)
            case MatroskaID.codecID: codecID = try reader.string(child)
            case MatroskaID.codecPrivate: codecPrivate = try reader.payload(child)
            case MatroskaID.language:
                let value = try reader.string(child)
                language = value == "und" ? nil : value
            case MatroskaID.name: name = try reader.string(child)
            case MatroskaID.blockAdditionMapping:
                let mapping = try reader.children(of: child)
                let typeElement = mapping.first { $0.id == MatroskaID.blockAddIDType }
                if try typeElement.map({ try reader.unsigned($0) }) == MatroskaID.mvcConfigurationType,
                   let extra = mapping.first(where: { $0.id == MatroskaID.blockAddIDExtraData }) {
                    mvcConfiguration = try reader.payload(extra)
                }
            default:
                break
            }
        }
        return MatroskaMVCSource.Track(
            number: number, type: type, codecID: codecID, codecPrivate: codecPrivate,
            language: language, name: name, mvcConfiguration: mvcConfiguration
        )
    }
}

struct EBMLElement {
    let id: UInt32
    let start: Int64
    let dataStart: Int64
    /// The end offset, or `Int64.max` for an element of unknown size.
    let end: Int64
}

/// Reads EBML elements through a 4 MiB window, so sequential reads cost one request each.
final class EBMLReader {
    private let source: ByteSource
    private var window = Data()
    private var windowStart: Int64 = 0
    private static let windowSize = 4 * 1024 * 1024
    /// The largest metadata payload, such as CodecPrivate or an MVC configuration, that is read.
    static let maximumMetadataSize = 1024 * 1024
    /// The largest block that is read; Blu-ray 3D frames are far smaller.
    static let maximumBlockSize = 32 * 1024 * 1024
    var offset: Int64
    private(set) var bytesRead: UInt64 = 0

    init(source: ByteSource, offset: Int64) {
        self.source = source
        self.offset = offset
    }

    private func bytes(_ count: Int) throws -> Data {
        guard count >= 0, Int64(count) <= source.size - offset else {
            throw Bluray3DSourceError.invalidStream("truncated element")
        }
        let windowEnd = windowStart + Int64(window.count)
        if offset < windowStart || offset + Int64(count) > windowEnd {
            window = try source.read(at: offset, count: max(count, Self.windowSize))
            windowStart = offset
            bytesRead += UInt64(window.count)
        }
        let start = Int(offset - windowStart)
        guard start + count <= window.count else { throw Bluray3DSourceError.invalidStream("truncated element") }
        offset += Int64(count)
        let base = window.startIndex
        return window.subdata(in: (base + start)..<(base + start + count))
    }

    private func byte() throws -> UInt8 { try bytes(1)[0] }

    /// An EBML variable-length integer of at most `maximumLength` bytes: 4 for IDs, 8 for sizes.
    private func variableLength(stripMarker: Bool, maximumLength: Int) throws -> (value: UInt64, length: Int) {
        let first = try byte()
        guard first != 0 else { throw Bluray3DSourceError.invalidStream("invalid EBML length") }
        let length = first.leadingZeroBitCount + 1
        guard length <= maximumLength else { throw Bluray3DSourceError.invalidStream("EBML number too long") }
        var value = UInt64(stripMarker ? first & (0xFF >> length) : first)
        for extra in try bytes(length - 1) { value = value << 8 | UInt64(extra) }
        return (value, length)
    }

    /// The next element header, or nil at the end of the source or of `end`. Inside an element of
    /// unknown size, a top-level element ID also ends it, and is left unread. An element must end
    /// within `end` and the source; only a Segment may run past the end of a truncated file.
    func nextElement(endingBefore end: Int64 = .max, insideUnknownSize: Bool = false) throws -> EBMLElement? {
        guard offset < min(end, source.size) else { return nil }
        let start = offset
        let id = UInt32(try variableLength(stripMarker: false, maximumLength: 4).value)
        if insideUnknownSize, MatroskaID.topLevel.contains(id) {
            offset = start
            return nil
        }
        let (size, length) = try variableLength(stripMarker: true, maximumLength: 8)
        let unknown = size == (UInt64(1) << (7 * length)) - 1
        let dataStart = offset
        if unknown {
            guard id == MatroskaID.segment || id == MatroskaID.cluster else {
                throw Bluray3DSourceError.invalidStream("unknown-size element")
            }
            return EBMLElement(id: id, start: start, dataStart: dataStart, end: .max)
        }
        let available = min(end, source.size) - dataStart
        guard size <= UInt64(max(0, available)) else {
            if id == MatroskaID.segment, end == .max {
                return EBMLElement(id: id, start: start, dataStart: dataStart, end: source.size)
            }
            throw Bluray3DSourceError.invalidStream("element extends past its parent or the file")
        }
        return EBMLElement(id: id, start: start, dataStart: dataStart, end: dataStart + Int64(size))
    }

    func skip(_ element: EBMLElement) throws {
        guard element.end != .max else { throw Bluray3DSourceError.invalidStream("cannot skip unknown-size element") }
        offset = element.end
    }

    func children(of element: EBMLElement) throws -> [EBMLElement] {
        offset = element.dataStart
        var children: [EBMLElement] = []
        while let child = try nextElement(endingBefore: element.end) {
            children.append(child)
            try skip(child)
        }
        return children
    }

    /// The element's bytes, refused before reading when they exceed `limit`.
    func payload(_ element: EBMLElement, limit: Int = EBMLReader.maximumMetadataSize) throws -> Data {
        guard element.end != .max else { throw Bluray3DSourceError.invalidStream("unknown-size payload") }
        let size = element.end - element.dataStart
        guard size <= Int64(limit) else { throw Bluray3DSourceError.invalidStream("element too large") }
        offset = element.dataStart
        return try bytes(Int(size))
    }

    func unsigned(_ element: EBMLElement) throws -> UInt64 {
        guard element.end - element.dataStart <= 8 else { throw Bluray3DSourceError.invalidStream("integer too long") }
        return try payload(element).reduce(0) { $0 << 8 | UInt64($1) }
    }

    /// An unsigned integer that must fit in `Int`, such as a track number.
    func integer(_ element: EBMLElement) throws -> Int {
        guard let value = Int(exactly: try unsigned(element)) else {
            throw Bluray3DSourceError.invalidStream("integer out of range")
        }
        return value
    }

    func float(_ element: EBMLElement) throws -> Double {
        let bits = try unsigned(element)
        switch element.end - element.dataStart {
        case 4: return Double(Float(bitPattern: UInt32(bits)))
        case 8: return Double(bitPattern: bits)
        default: return 0
        }
    }

    func string(_ element: EBMLElement) throws -> String {
        String(decoding: try payload(element).prefix { $0 != 0 }, as: UTF8.self)
    }

    func blockGroup(_ element: EBMLElement) throws -> MatroskaBlock? {
        var block: Data?
        var referenced = false
        for child in try children(of: element) {
            if child.id == MatroskaID.block { block = try payload(child, limit: Self.maximumBlockSize) }
            if child.id == MatroskaID.referenceBlock { referenced = true }
        }
        offset = element.end
        guard let block else { return nil }
        var parsed = try MatroskaBlock(block, simple: false)
        parsed.isKeyframe = !referenced
        return parsed
    }
}

/// One Matroska block, split into its laced frames.
struct MatroskaBlock {
    let track: Int
    let relativeTimestamp: Int16
    var isKeyframe: Bool
    let frames: [Data]

    init(_ data: Data, simple: Bool) throws {
        let bytes = [UInt8](data)
        guard let first = bytes.first, first != 0 else { throw Bluray3DSourceError.invalidStream("invalid block") }
        let trackLength = first.leadingZeroBitCount + 1
        guard bytes.count >= trackLength + 3 else { throw Bluray3DSourceError.invalidStream("short block") }
        var track = Int(first & (0xFF >> trackLength))
        for byte in bytes[1..<trackLength] { track = track << 8 | Int(byte) }
        self.track = track
        relativeTimestamp = Int16(bitPattern: UInt16(bytes[trackLength]) << 8 | UInt16(bytes[trackLength + 1]))
        let flags = bytes[trackLength + 2]
        isKeyframe = simple && flags & 0x80 != 0
        let body = trackLength + 3
        frames = try Self.frames(bytes, from: body, lacing: (flags >> 1) & 0x03)
    }

    private static func frames(_ bytes: [UInt8], from start: Int, lacing: UInt8) throws -> [Data] {
        guard lacing != 0 else { return [Data(bytes[start...])] }
        guard start < bytes.count else { throw Bluray3DSourceError.invalidStream("short laced block") }
        let count = Int(bytes[start]) + 1
        var cursor = start + 1
        var sizes: [Int] = []
        func next() throws -> UInt8 {
            guard cursor < bytes.count else { throw Bluray3DSourceError.invalidStream("short lacing header") }
            defer { cursor += 1 }
            return bytes[cursor]
        }
        func ebmlNumber(signed: Bool) throws -> Int {
            let first = try next()
            guard first != 0 else { throw Bluray3DSourceError.invalidStream("invalid lace size") }
            let length = first.leadingZeroBitCount + 1
            var value = Int(first & (0xFF >> length))
            for _ in 1..<length { value = value << 8 | Int(try next()) }
            return signed ? value - ((1 << (7 * length - 1)) - 1) : value
        }
        switch lacing {
        case 1:
            for _ in 0..<(count - 1) {
                var size = 0
                var byte: UInt8 = 255
                while byte == 255 {
                    byte = try next()
                    size += Int(byte)
                }
                sizes.append(size)
            }
        case 3:
            if count > 1 {
                var size = try ebmlNumber(signed: false)
                sizes.append(size)
                for _ in 0..<(count - 2) {
                    let (next, overflow) = size.addingReportingOverflow(try ebmlNumber(signed: true))
                    guard !overflow else { throw Bluray3DSourceError.invalidStream("lace sizes exceed block") }
                    size = next
                    sizes.append(size)
                }
            }
        default:
            let total = bytes.count - cursor
            guard total % count == 0 else { throw Bluray3DSourceError.invalidStream("uneven fixed lacing") }
            sizes = Array(repeating: total / count, count: count - 1)
        }
        // Each size is checked against the block before summing, so the sum cannot overflow.
        guard sizes.allSatisfy({ (0...bytes.count).contains($0) }) else {
            throw Bluray3DSourceError.invalidStream("lace sizes exceed block")
        }
        let remaining = bytes.count - cursor - sizes.reduce(0, +)
        guard remaining >= 0 else { throw Bluray3DSourceError.invalidStream("lace sizes exceed block") }
        sizes.append(remaining)
        return sizes.map { size in
            defer { cursor += size }
            return Data(bytes[cursor..<(cursor + size)])
        }
    }
}

/// Turns Matroska's length-prefixed H.264 MVC frames into Annex B, with the MVC parameter sets
/// before the first frame so a decoder can start there.
final class MVCAnnexBConverter {
    private let lengthSize: Int
    private var parameterSets: Data?

    init(avcConfiguration: Data, mvcConfiguration: Data?) throws {
        let avc = [UInt8](avcConfiguration)
        guard avc.count >= 7, avc[0] == 1 else { throw Bluray3DSourceError.invalidStream("invalid avcC") }
        lengthSize = Int(avc[4] & 0x03) + 1
        let configuration = mvcConfiguration.map { [UInt8]($0) }.flatMap { $0.count >= 7 && $0[0] == 1 ? $0 : nil }
        parameterSets = try Self.parameterSets(configuration ?? avc)
    }

    /// The SPS and PPS NAL units of an avcC-shaped record, as Annex B.
    static func parameterSets(_ record: [UInt8]) throws -> Data {
        var output = Data()
        var cursor = 6
        func copy(count: Int) throws {
            for _ in 0..<count {
                guard cursor + 2 <= record.count else { throw Bluray3DSourceError.invalidStream("short parameter set") }
                let length = Int(record[cursor]) << 8 | Int(record[cursor + 1])
                cursor += 2
                guard cursor + length <= record.count else { throw Bluray3DSourceError.invalidStream("short parameter set") }
                output.append(contentsOf: [0, 0, 0, 1])
                output.append(contentsOf: record[cursor..<(cursor + length)])
                cursor += length
            }
        }
        try copy(count: Int(record[5] & 0x1F))
        guard cursor < record.count else { throw Bluray3DSourceError.invalidStream("missing PPS count") }
        let pictureCount = Int(record[cursor])
        cursor += 1
        try copy(count: pictureCount)
        return output
    }

    func convert(_ frame: Data) throws -> Data {
        let bytes = [UInt8](frame)
        var output = parameterSets ?? Data()
        parameterSets = nil
        output.reserveCapacity(output.count + bytes.count + 16)
        var cursor = 0
        while cursor < bytes.count {
            guard cursor + lengthSize <= bytes.count else { throw Bluray3DSourceError.invalidStream("short NAL length") }
            let length = bytes[cursor..<(cursor + lengthSize)].reduce(0) { $0 << 8 | Int($1) }
            cursor += lengthSize
            guard length > 0, cursor + length <= bytes.count else { throw Bluray3DSourceError.invalidStream("NAL exceeds frame") }
            output.append(contentsOf: [0, 0, 0, 1])
            output.append(contentsOf: bytes[cursor..<(cursor + length)])
            cursor += length
        }
        return output
    }
}

import Foundation

/// One entry of a PGS palette, as the disc stores it: limited-range BT.709 Y′CbCr, the same
/// coding as Blu-ray video, so subtitles blend into the decoded frames without a color conversion.
struct PGSPaletteEntry: Equatable, Sendable {
    let y: UInt8
    let cb: UInt8
    let cr: UInt8
    let alpha: UInt8

    static let transparent = PGSPaletteEntry(y: 16, cb: 128, cr: 128, alpha: 0)
}

/// One object of a display, placed on the video frame, with its run-length coded pixels still
/// compressed; `PGSRunLength.decode` expands them when the display is first drawn.
struct PGSDisplayObject: Equatable, Sendable {
    /// Where the object's visible part starts on the frame.
    let x: Int
    let y: Int
    /// The object's full size, as coded.
    let width: Int
    let height: Int
    /// The part of the object shown, in its own coordinates; the whole object when not cropped.
    let crop: PGSRect
    /// The disc shows a forced object even when the wearer has subtitles off, for dialogue in
    /// another language. The player keeps one rule for every track: Off shows nothing, and a
    /// chosen track shows everything in it, forced or not.
    let isForced: Bool
    let runLengthData: Data
}

struct PGSRect: Equatable, Sendable {
    let x: Int
    let y: Int
    let width: Int
    let height: Int
}

/// What a PGS track shows from `ticks` until the next display: its objects and the palette they
/// use. A display without objects clears the screen.
struct PGSDisplay: Equatable, Sendable {
    /// When it appears, on the title's timeline (90 kHz, zero at the title's start).
    let ticks: Int64
    /// The frame size the coordinates are on, normally the video's 1920x1080.
    let videoWidth: Int
    let videoHeight: Int
    let objects: [PGSDisplayObject]
    /// 256 entries; indices the disc does not define are transparent.
    let palette: [PGSPaletteEntry]

    var isEmpty: Bool { objects.isEmpty }
}

enum PGSSegmentType: UInt8 {
    case palette = 0x14
    case object = 0x15
    case presentationComposition = 0x16
    case windowDefinition = 0x17
    case end = 0x80
}

/// Decodes one Presentation Graphic Stream (Blu-ray PGS subtitles) into displays. Segments arrive
/// one per PES packet from a disc and a whole display set per block from Matroska; both are a
/// run of segments, each a type byte, a 16-bit length and its data. A display set runs from its
/// presentation composition segment (PCS), which carries the presentation time, to its end
/// segment, which completes the display.
///
/// After `reset()`, as after a seek starts a new read, objects and palettes from before are gone:
/// a display set that only updates an earlier one shows what it can until the next epoch start or
/// acquisition point, which carries everything again.
struct PGSDecoder {
    private struct Composition {
        struct Object {
            let objectID: UInt16
            let x: Int
            let y: Int
            let crop: PGSRect?
            let isForced: Bool
        }

        let ticks: Int64
        let videoWidth: Int
        let videoHeight: Int
        let paletteID: UInt8
        let objects: [Object]
    }

    private struct ObjectData {
        var width: Int
        var height: Int
        var data: Data
        var isComplete: Bool
    }

    private var composition: Composition?
    private var objects: [UInt16: ObjectData] = [:]
    private var palettes: [UInt8: [PGSPaletteEntry]] = [:]

    /// Forgets every object and palette, for a read that starts somewhere else.
    mutating func reset() {
        composition = nil
        objects = [:]
        palettes = [:]
    }

    /// Takes one packet's payload, timed `ticks` on the title's timeline (nil when the packet
    /// carries no timestamp), and returns the displays it completes. Malformed segments are
    /// skipped, as a damaged stretch of a disc would be.
    mutating func receive(_ payload: Data, ticks: Int64?) -> [PGSDisplay] {
        let bytes = [UInt8](payload)
        var displays: [PGSDisplay] = []
        var offset = 0
        while offset + 3 <= bytes.count {
            let length = Int(bytes[offset + 1]) << 8 | Int(bytes[offset + 2])
            let start = offset + 3
            guard start + length <= bytes.count else { break }
            let segment = bytes[start..<(start + length)]
            switch PGSSegmentType(rawValue: bytes[offset]) {
            case .presentationComposition:
                if let ticks { readComposition(segment, ticks: ticks) }
            case .palette:
                readPalette(segment)
            case .object:
                readObject(segment)
            case .end:
                if let display = completeDisplay() { displays.append(display) }
            case .windowDefinition, nil:
                break
            }
            offset = start + length
        }
        return displays
    }

    private mutating func readComposition(_ segment: ArraySlice<UInt8>, ticks: Int64) {
        let bytes = Array(segment)
        guard bytes.count >= 11 else { return }
        let state = bytes[7]
        if state & 0x80 != 0 {
            // An epoch start drops every object and palette of the epoch before.
            objects = [:]
            palettes = [:]
        }
        var placed: [Composition.Object] = []
        var offset = 11
        for _ in 0..<Int(bytes[10]) {
            guard offset + 8 <= bytes.count else { break }
            let flags = bytes[offset + 3]
            let isCropped = flags & 0x80 != 0
            var crop: PGSRect?
            if isCropped {
                guard offset + 16 <= bytes.count else { break }
                crop = PGSRect(
                    x: Self.word(bytes, offset + 8), y: Self.word(bytes, offset + 10),
                    width: Self.word(bytes, offset + 12), height: Self.word(bytes, offset + 14)
                )
            }
            placed.append(Composition.Object(
                objectID: UInt16(Self.word(bytes, offset)),
                x: Self.word(bytes, offset + 4),
                y: Self.word(bytes, offset + 6),
                crop: crop,
                isForced: flags & 0x40 != 0
            ))
            offset += isCropped ? 16 : 8
        }
        composition = Composition(
            ticks: ticks,
            videoWidth: Self.word(bytes, 0),
            videoHeight: Self.word(bytes, 2),
            paletteID: bytes[9],
            objects: placed
        )
    }

    private mutating func readPalette(_ segment: ArraySlice<UInt8>) {
        let bytes = Array(segment)
        guard bytes.count >= 2 else { return }
        var palette = palettes[bytes[0]] ?? Array(repeating: .transparent, count: 256)
        var offset = 2
        while offset + 5 <= bytes.count {
            // Each entry is the index, then Y, Cr, Cb and alpha.
            palette[Int(bytes[offset])] = PGSPaletteEntry(
                y: bytes[offset + 1], cb: bytes[offset + 3], cr: bytes[offset + 2], alpha: bytes[offset + 4]
            )
            offset += 5
        }
        palettes[bytes[0]] = palette
    }

    private mutating func readObject(_ segment: ArraySlice<UInt8>) {
        let bytes = Array(segment)
        guard bytes.count >= 4 else { return }
        let objectID = UInt16(Self.word(bytes, 0))
        let sequence = bytes[3]
        let isFirst = sequence & 0x80 != 0
        let isLast = sequence & 0x40 != 0
        if isFirst {
            // The first fragment gives the data length (3 bytes), then the width and height.
            guard bytes.count >= 11 else { return }
            objects[objectID] = ObjectData(
                width: Self.word(bytes, 7), height: Self.word(bytes, 9), data: Data(bytes[11...]), isComplete: isLast
            )
        } else if var object = objects[objectID], !object.isComplete {
            object.data.append(contentsOf: bytes[4...])
            object.isComplete = isLast
            objects[objectID] = object
        }
    }

    private mutating func completeDisplay() -> PGSDisplay? {
        guard let composition else { return nil }
        self.composition = nil
        let palette = palettes[composition.paletteID] ?? Array(repeating: .transparent, count: 256)
        // Objects missing since a reset, or still incomplete, are left out.
        let placed = composition.objects.compactMap { placement -> PGSDisplayObject? in
            guard let object = objects[placement.objectID], object.isComplete, object.width > 0, object.height > 0 else {
                return nil
            }
            let full = PGSRect(x: 0, y: 0, width: object.width, height: object.height)
            let crop = placement.crop.flatMap { $0.clipped(to: full) } ?? full
            return PGSDisplayObject(
                x: placement.x, y: placement.y, width: object.width, height: object.height, crop: crop,
                isForced: placement.isForced, runLengthData: object.data
            )
        }
        return PGSDisplay(
            ticks: composition.ticks,
            videoWidth: composition.videoWidth,
            videoHeight: composition.videoHeight,
            objects: placed,
            palette: palette
        )
    }

    private static func word(_ bytes: [UInt8], _ offset: Int) -> Int {
        Int(bytes[offset]) << 8 | Int(bytes[offset + 1])
    }
}

extension PGSRect {
    /// The part of this rectangle inside `bounds`, or nil when nothing is.
    func clipped(to bounds: PGSRect) -> PGSRect? {
        let left = max(x, bounds.x)
        let top = max(y, bounds.y)
        let right = min(x + width, bounds.x + bounds.width)
        let bottom = min(y + height, bounds.y + bounds.height)
        guard right > left, bottom > top else { return nil }
        return PGSRect(x: left, y: top, width: right - left, height: bottom - top)
    }
}

/// The PGS run-length coding: a nonzero byte is one pixel of that palette index; a zero byte
/// starts a run, or ends the line when the next byte is zero too.
enum PGSRunLength {
    /// Palette indices, `width` x `height`, row by row. Lines that run long are cut at `width`
    /// and short ones padded with index 0, so damaged data still draws in its own rectangle.
    static func decode(_ data: Data, width: Int, height: Int) -> [UInt8] {
        guard width > 0, height > 0 else { return [] }
        var pixels = [UInt8](repeating: 0, count: width * height)
        data.withUnsafeBytes { raw in
            let bytes = raw.bindMemory(to: UInt8.self)
            var offset = 0
            var row = 0
            var column = 0
            func put(_ index: UInt8, count: Int) {
                guard row < height, column < width else {
                    column += count
                    return
                }
                let run = min(count, width - column)
                if index != 0 {
                    let start = row * width + column
                    for position in start..<(start + run) { pixels[position] = index }
                }
                column += count
            }
            while offset < bytes.count, row < height {
                let first = bytes[offset]
                offset += 1
                if first != 0 {
                    put(first, count: 1)
                    continue
                }
                guard offset < bytes.count else { break }
                let flags = bytes[offset]
                offset += 1
                if flags == 0 {
                    row += 1
                    column = 0
                    continue
                }
                var count = Int(flags & 0x3F)
                if flags & 0x40 != 0 {
                    guard offset < bytes.count else { break }
                    count = count << 8 | Int(bytes[offset])
                    offset += 1
                }
                var index: UInt8 = 0
                if flags & 0x80 != 0 {
                    guard offset < bytes.count else { break }
                    index = bytes[offset]
                    offset += 1
                }
                put(index, count: count)
            }
        }
        return pixels
    }
}

/// A PGS subtitle track the wearer can choose, labeled like the audio tracks: the language, with
/// a Matroska track's own name and its track number when two would read the same.
struct PGSSubtitleTrack: Identifiable, Equatable, Sendable {
    /// The transport PID on a disc, the track number in a Matroska file.
    let id: UInt16
    let label: String

    var mediaOption: PlaybackMediaOption {
        PlaybackMediaOption(id: "subtitle-pid-\(id)", displayName: label)
    }

    /// The title's PGS tracks in disc order. Other subtitle formats, such as Blu-ray text
    /// subtitles, are left out.
    static func tracks(for tracks: [Bluray3DTrack], locale: Locale = .current) -> [PGSSubtitleTrack] {
        let pgs = tracks.filter { $0.kind == .subtitle && $0.codingType == 0x90 }
        let labels = PlaybackAudioOptionLabelPolicy.labels(
            for: pgs.enumerated().map { index, track in
                PlaybackAudioOptionLabelMetadata(
                    baseName: HeadsetAudioTrack.languageName(track.language, locale: locale), role: track.name, index: index
                )
            }
        )
        return zip(pgs, labels).map { PGSSubtitleTrack(id: UInt16(clamping: $0.id), label: $1) }
    }
}

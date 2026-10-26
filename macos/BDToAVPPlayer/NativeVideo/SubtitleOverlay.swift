import CoreVideo
import Foundation

/// A PGS display drawn into each eye's video frame, so the subtitle is part of the picture the
/// stereo renderer shows rather than a separate layer that visionOS can place behind the video.
///
/// Depth comes from disparity: the left eye's copy sits `eyeShift` pixels right of where the disc
/// places it and the right eye's copy the same distance left. That crossed disparity puts the
/// subtitle in front of the screen plane. Blu-ray 3D discs can carry per-scene depths for
/// subtitles (the MVC offset sequences); the player does not read them yet and uses one fixed
/// depth instead.
struct SubtitleOverlay {
    /// Pixels each eye's copy moves, on a 1920-pixel-wide frame: 12 pixels of disparity in all,
    /// about 0.6% of the width, which places the subtitle a little in front of the screen.
    static let eyeShift = 6

    enum Eye {
        case left
        case right

        /// Where this eye's copy moves horizontally: right for the left eye, left for the right.
        var horizontalOffset: Int { self == .left ? SubtitleOverlay.eyeShift : -SubtitleOverlay.eyeShift }
    }

    /// The area the display covers, on the frame.
    let bounds: PGSRect
    /// Per pixel of `bounds`, row by row: the palette color and its opacity.
    private let alpha: [UInt8]
    private let luma: [UInt8]
    private let cb: [UInt8]
    private let cr: [UInt8]

    /// Nil for a display that shows nothing. Parts outside the display's frame are cut first.
    init?(_ display: PGSDisplay) {
        let canvas = PGSRect(x: 0, y: 0, width: display.videoWidth, height: display.videoHeight)
        // Each object's visible part on the frame, with the object it comes from.
        let visible = display.objects.compactMap { object -> (object: PGSDisplayObject, rect: PGSRect)? in
            PGSRect(x: object.x, y: object.y, width: object.crop.width, height: object.crop.height)
                .clipped(to: canvas)
                .map { (object, $0) }
        }
        guard let first = visible.first?.rect else { return nil }
        let left = visible.map(\.rect.x).min() ?? first.x
        let top = visible.map(\.rect.y).min() ?? first.y
        let right = visible.map { $0.rect.x + $0.rect.width }.max() ?? first.x
        let bottom = visible.map { $0.rect.y + $0.rect.height }.max() ?? first.y
        let bounds = PGSRect(x: left, y: top, width: right - left, height: bottom - top)
        var alpha = [UInt8](repeating: 0, count: bounds.width * bounds.height)
        var luma = alpha
        var cb = alpha
        var cr = alpha
        var isVisible = false
        for (object, rect) in visible {
            let indices = PGSRunLength.decode(object.runLengthData, width: object.width, height: object.height)
            // Where the visible part starts inside the object.
            let sourceX = object.crop.x + rect.x - object.x
            let sourceY = object.crop.y + rect.y - object.y
            for row in 0..<rect.height {
                let source = (sourceY + row) * object.width + sourceX
                let target = (rect.y - top + row) * bounds.width + (rect.x - left)
                for column in 0..<rect.width {
                    let entry = display.palette[Int(indices[source + column])]
                    // Objects do not overlap on a disc; a later one covers an earlier one.
                    guard entry.alpha > 0 else { continue }
                    let position = target + column
                    alpha[position] = entry.alpha
                    luma[position] = entry.y
                    cb[position] = entry.cb
                    cr[position] = entry.cr
                    isVisible = true
                }
            }
        }
        guard isVisible else { return nil }
        self.bounds = bounds
        self.alpha = alpha
        self.luma = luma
        self.cb = cb
        self.cr = cr
    }

    /// Blends the subtitle into an NV12 (4:2:0, limited-range) frame for `eye`. The caller holds
    /// the buffer's base address lock. Parts that fall off the frame are cut.
    func draw(into buffer: CVPixelBuffer, eye: Eye) {
        alpha.withUnsafeBufferPointer { alpha in
            luma.withUnsafeBufferPointer { luma in
                cb.withUnsafeBufferPointer { cb in
                    cr.withUnsafeBufferPointer { cr in
                        draw(into: buffer, eye: eye, alpha: alpha, luma: luma, cb: cb, cr: cr)
                    }
                }
            }
        }
    }

    private func draw(
        into buffer: CVPixelBuffer,
        eye: Eye,
        alpha: UnsafeBufferPointer<UInt8>,
        luma: UnsafeBufferPointer<UInt8>,
        cb: UnsafeBufferPointer<UInt8>,
        cr: UnsafeBufferPointer<UInt8>
    ) {
        guard let lumaBase = CVPixelBufferGetBaseAddressOfPlane(buffer, 0),
              let chromaBase = CVPixelBufferGetBaseAddressOfPlane(buffer, 1)
        else { return }
        let lumaPlane = lumaBase.assumingMemoryBound(to: UInt8.self)
        let chromaPlane = chromaBase.assumingMemoryBound(to: UInt8.self)
        let frameWidth = CVPixelBufferGetWidthOfPlane(buffer, 0)
        let frameHeight = CVPixelBufferGetHeightOfPlane(buffer, 0)
        let lumaStride = CVPixelBufferGetBytesPerRowOfPlane(buffer, 0)
        let chromaWidth = CVPixelBufferGetWidthOfPlane(buffer, 1)
        let chromaHeight = CVPixelBufferGetHeightOfPlane(buffer, 1)
        let chromaStride = CVPixelBufferGetBytesPerRowOfPlane(buffer, 1)
        let originX = bounds.x + eye.horizontalOffset
        let originY = bounds.y

        let firstColumn = max(0, -originX)
        let lastColumn = min(bounds.width, frameWidth - originX)
        let firstRow = max(0, -originY)
        let lastRow = min(bounds.height, frameHeight - originY)
        guard firstColumn < lastColumn, firstRow < lastRow else { return }
        for row in firstRow..<lastRow {
            let line = lumaPlane + (originY + row) * lumaStride + originX
            let source = row * bounds.width
            for column in firstColumn..<lastColumn {
                let opacity = Int(alpha[source + column])
                guard opacity > 0 else { continue }
                line[column] = UInt8((Int(line[column]) * (255 - opacity) + Int(luma[source + column]) * opacity + 127) / 255)
            }
        }

        // Each chroma sample covers 2x2 pixels: blend it by the covered pixels' summed opacity.
        let firstChromaColumn = (originX + firstColumn) / 2
        let lastChromaColumn = min(chromaWidth, (originX + lastColumn + 1) / 2)
        let firstChromaRow = (originY + firstRow) / 2
        let lastChromaRow = min(chromaHeight, (originY + lastRow + 1) / 2)
        guard firstChromaColumn < lastChromaColumn, firstChromaRow < lastChromaRow else { return }
        for chromaRow in firstChromaRow..<lastChromaRow {
            let line = chromaPlane + chromaRow * chromaStride
            for chromaColumn in firstChromaColumn..<lastChromaColumn {
                var opacity = 0
                var weightedCb = 0
                var weightedCr = 0
                for dy in 0..<2 {
                    let row = chromaRow * 2 + dy - originY
                    guard row >= 0, row < bounds.height else { continue }
                    for dx in 0..<2 {
                        let column = chromaColumn * 2 + dx - originX
                        guard column >= 0, column < bounds.width else { continue }
                        let position = row * bounds.width + column
                        let pixelOpacity = Int(alpha[position])
                        opacity += pixelOpacity
                        weightedCb += pixelOpacity * Int(cb[position])
                        weightedCr += pixelOpacity * Int(cr[position])
                    }
                }
                guard opacity > 0 else { continue }
                let cbPosition = chromaColumn * 2
                line[cbPosition] = UInt8((Int(line[cbPosition]) * (1020 - opacity) + weightedCb + 510) / 1020)
                line[cbPosition + 1] = UInt8((Int(line[cbPosition + 1]) * (1020 - opacity) + weightedCr + 510) / 1020)
            }
        }
    }
}

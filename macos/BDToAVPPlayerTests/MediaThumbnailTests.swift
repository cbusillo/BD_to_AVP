import AVFoundation
import XCTest
@testable import BDToAVPPlayer

final class MediaThumbnailTests: XCTestCase {
    func testThumbnailCacheUsesABoundedDefaultLimit() {
        XCTAssertEqual(MediaThumbnailCache.defaultCountLimit, 48)
    }

    func testThumbnailCacheRoundTripsAFrameByKey() throws {
        let cache = MediaThumbnailCache(countLimit: 2)
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        let image = try XCTUnwrap(
            CGContext(
                data: nil,
                width: 2,
                height: 2,
                bitsPerComponent: 8,
                bytesPerRow: 0,
                space: colorSpace,
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            )?.makeImage()
        )

        cache.insert(image, for: "movie")

        XCTAssertNotNil(cache.image(for: "movie"))
        XCTAssertNil(cache.image(for: "other"))
    }

    func testThumbnailCacheKeyChangesWhenSourceFilenameChanges() {
        let first = MediaItem(id: "movie", title: "Movie", fileName: "first.mov", format: .mvHEVC)
        let second = MediaItem(id: "movie", title: "Movie", fileName: "second.mov", format: .mvHEVC)

        XCTAssertNotEqual(MediaThumbnailLoader.cacheKey(for: first), MediaThumbnailLoader.cacheKey(for: second))
    }
}

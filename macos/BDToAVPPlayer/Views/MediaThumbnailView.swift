import AVFoundation
import SwiftUI

private actor MediaThumbnailGenerationLimiter {
    private var availablePermits: Int
    private var waiters: [CheckedContinuation<Void, Never>] = []

    init(limit: Int) {
        availablePermits = max(1, limit)
    }

    func acquire() async {
        if availablePermits > 0 {
            availablePermits -= 1
            return
        }

        await withCheckedContinuation { continuation in
            waiters.append(continuation)
        }
    }

    func release() {
        if waiters.isEmpty {
            availablePermits += 1
        } else {
            waiters.removeFirst().resume()
        }
    }
}

final class MediaThumbnailCache {
    static let defaultCountLimit = 48
    static let defaultTotalCostLimit = 96 * 1_024 * 1_024

    private final class Entry: NSObject {
        let image: CGImage

        init(image: CGImage) {
            self.image = image
        }
    }

    private let cache: NSCache<NSString, Entry>

    init(
        countLimit: Int = MediaThumbnailCache.defaultCountLimit,
        totalCostLimit: Int = MediaThumbnailCache.defaultTotalCostLimit
    ) {
        cache = NSCache<NSString, Entry>()
        cache.countLimit = max(1, countLimit)
        cache.totalCostLimit = max(1, totalCostLimit)
    }

    func image(for key: String) -> CGImage? {
        cache.object(forKey: key as NSString)?.image
    }

    func insert(_ image: CGImage, for key: String) {
        cache.setObject(
            Entry(image: image),
            forKey: key as NSString,
            cost: image.bytesPerRow * image.height
        )
    }
}

@MainActor
final class MediaThumbnailLoader {
    static let shared = MediaThumbnailLoader()

    let cache: MediaThumbnailCache
    private let generationLimiter: MediaThumbnailGenerationLimiter

    init(
        cache: MediaThumbnailCache = MediaThumbnailCache(),
        maximumConcurrentGenerations: Int = 4
    ) {
        self.cache = cache
        generationLimiter = MediaThumbnailGenerationLimiter(limit: maximumConcurrentGenerations)
    }

    func image(for item: MediaItem, bookmarkStore: BookmarkStore) async -> CGImage? {
        let key = Self.cacheKey(for: item, bookmarkData: bookmarkStore.bookmarkData(for: item.id))
        if let cachedImage = cache.image(for: key) {
            return cachedImage
        }

        await generationLimiter.acquire()
        defer {
            Task {
                await generationLimiter.release()
            }
        }

        guard !Task.isCancelled,
              let lease = try? await bookmarkStore.open(id: item.id)
        else {
            return nil
        }
        defer { lease.close() }

        let asset = AVURLAsset(url: lease.url)
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: 1280, height: 720)

        let duration = try? await asset.load(.duration)
        let requestedTime = Self.requestedTime(for: duration?.seconds)
        let image = try? await generator.image(at: requestedTime).image

        if let image {
            cache.insert(image, for: key)
            guard !Task.isCancelled else { return nil }
            return image
        }
        return nil
    }

    nonisolated static func cacheKey(for item: MediaItem, bookmarkData: Data? = nil) -> String {
        "\(item.id)|\(item.fileName)|\(item.format.rawValue)|\(bookmarkData?.hashValue ?? 0)"
    }

    nonisolated static func requestedTime(for duration: TimeInterval?) -> CMTime {
        guard let duration, duration.isFinite, duration > 0 else {
            return CMTime(seconds: 1, preferredTimescale: 600)
        }
        let seconds = min(max(duration * 0.1, 0), min(30, duration))
        return CMTime(seconds: seconds, preferredTimescale: 600)
    }

}

struct MediaThumbnailView: View {
    let item: MediaItem
    let bookmarkStore: BookmarkStore
    let sourceStatus: MediaSourceStatus?

    @State private var image: CGImage?

    var body: some View {
        GeometryReader { geometry in
            Group {
                if let image {
                    Image(decorative: image, scale: 1, orientation: .up)
                        .resizable()
                        .scaledToFill()
                } else {
                    MediaThumbnailFallback(title: item.title)
                }
            }
            .frame(width: geometry.size.width, height: geometry.size.height)
            .clipped()
            .clipShape(RoundedRectangle(cornerRadius: LibraryTheme.tileCornerRadius - 6, style: .continuous))
        }
        .task(id: thumbnailTaskID) {
            guard sourceStatus == .available else {
                image = nil
                return
            }
            let loadedImage = await MediaThumbnailLoader.shared.image(for: item, bookmarkStore: bookmarkStore)
            guard !Task.isCancelled else { return }
            image = loadedImage
        }
        .accessibilityHidden(true)
    }

    private var thumbnailTaskID: String {
        let cacheKey = MediaThumbnailLoader.cacheKey(
            for: item,
            bookmarkData: bookmarkStore.bookmarkData(for: item.id)
        )
        return "\(cacheKey)|\(sourceStatus == .available)"
    }
}

struct MediaThumbnailFallback: View {
    let title: String

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            LinearGradient(
                colors: [.secondary.opacity(0.42), .black.opacity(0.82)],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            Text(title)
                .font(.title2.weight(.semibold))
                .foregroundStyle(.white)
                .lineLimit(3)
                .padding(20)
        }
    }
}

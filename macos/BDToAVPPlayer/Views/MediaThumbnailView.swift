import AVFoundation
import SwiftUI

final class MediaThumbnailCache {
    static let defaultCountLimit = 48

    private final class Entry: NSObject {
        let image: CGImage

        init(image: CGImage) {
            self.image = image
        }
    }

    private let cache: NSCache<NSString, Entry>

    init(countLimit: Int = MediaThumbnailCache.defaultCountLimit) {
        cache = NSCache<NSString, Entry>()
        cache.countLimit = max(1, countLimit)
    }

    func image(for key: String) -> CGImage? {
        cache.object(forKey: key as NSString)?.image
    }

    func insert(_ image: CGImage, for key: String) {
        cache.setObject(Entry(image: image), forKey: key as NSString)
    }
}

@MainActor
final class MediaThumbnailLoader {
    static let shared = MediaThumbnailLoader()

    let cache: MediaThumbnailCache

    init(cache: MediaThumbnailCache = MediaThumbnailCache()) {
        self.cache = cache
    }

    func image(for item: MediaItem, bookmarkStore: BookmarkStore) async -> CGImage? {
        let key = Self.cacheKey(for: item)
        if let cachedImage = cache.image(for: key) {
            return cachedImage
        }

        let image = await Task.detached(priority: .userInitiated) {
            Self.extractFrame(for: item, bookmarkStore: bookmarkStore)
        }.value

        if let image {
            cache.insert(image, for: key)
        }
        return image
    }

    nonisolated static func cacheKey(for item: MediaItem) -> String {
        "\(item.id)|\(item.fileName)|\(item.format.rawValue)"
    }

    private nonisolated static func extractFrame(for item: MediaItem, bookmarkStore: BookmarkStore) -> CGImage? {
        guard let result = try? bookmarkStore.withResolvedURL(for: item.id, { url in
            let asset = AVAsset(url: url)
            let generator = AVAssetImageGenerator(asset: asset)
            generator.appliesPreferredTrackTransform = true
            generator.maximumSize = CGSize(width: 1600, height: 900)

            let semaphore = DispatchSemaphore(value: 0)
            var generatedImage: CGImage?
            let requestedTime = CMTime(seconds: 1, preferredTimescale: 600)
            generator.generateCGImagesAsynchronously(forTimes: [NSValue(time: requestedTime)]) { _, image, _, result, _ in
                if result == .succeeded {
                    generatedImage = image
                }
                semaphore.signal()
            }
            semaphore.wait()
            return generatedImage
        }) else {
            return nil
        }
        return result
    }
}

struct MediaThumbnailView: View {
    let item: MediaItem
    let bookmarkStore: BookmarkStore
    let sourceStatus: MediaSourceStatus?

    @State private var image: CGImage?

    var body: some View {
        Group {
            if let image {
                Image(decorative: image, scale: 1, orientation: .up)
                    .resizable()
                    .scaledToFill()
            } else {
                MediaThumbnailFallback(title: item.title)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .clipped()
        .clipShape(RoundedRectangle(cornerRadius: LibraryTheme.tileCornerRadius - 6, style: .continuous))
        .task(id: MediaThumbnailLoader.cacheKey(for: item)) {
            guard sourceStatus == .available else {
                image = nil
                return
            }
            image = await MediaThumbnailLoader.shared.image(for: item, bookmarkStore: bookmarkStore)
        }
        .accessibilityLabel("Thumbnail for \(item.title)")
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

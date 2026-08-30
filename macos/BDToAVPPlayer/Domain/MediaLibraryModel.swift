import Combine
import Foundation

final class MediaLibraryModel: ObservableObject {
    @Published private(set) var items: [MediaItem]

    init(items: [MediaItem] = []) {
        self.items = Self.deduplicated(items)
    }

    var posters: [MediaItem] {
        items
    }

    var files: [MediaItem] {
        items
    }

    func upsert(_ item: MediaItem) {
        if let index = items.firstIndex(where: { $0.id == item.id }) {
            items[index] = item
        } else {
            items.append(item)
        }
    }

    func remove(id: String) {
        items.removeAll { $0.id == id }
    }

    private static func deduplicated(_ items: [MediaItem]) -> [MediaItem] {
        var seen = Set<String>()
        return items.filter { seen.insert($0.id).inserted }
    }
}

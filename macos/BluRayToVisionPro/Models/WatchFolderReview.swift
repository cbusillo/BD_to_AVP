import Foundation

/// The items one watch folder holds for review, shown as one line of the queue's review banner.
struct WatchFolderReviewGroup: Identifiable, Equatable {
    let id: String
    let folderName: String
    /// Held items from this folder, in queue order.
    let itemIDs: [UUID]

    var title: String {
        "\(itemIDs.count) new from “\(folderName)”"
    }

    /// The item Review selects.
    var firstItemID: UUID? {
        itemIDs.first
    }

    /// Groups held-for-review items by the watch folder that admitted them, in queue order.
    static func groups(for items: [PersistentQueueItem]) -> [WatchFolderReviewGroup] {
        var order: [String] = []
        var names: [String: String] = [:]
        var itemIDs: [String: [UUID]] = [:]
        for item in items.sorted(by: { $0.ordinal < $1.ordinal }) where item.isHeldForReview {
            let name = item.admission?.watchFolderName ?? "Watch Folder"
            let key = item.admission?.watchFolderID?.uuidString ?? "name:\(name)"
            if itemIDs[key] == nil {
                order.append(key)
                names[key] = name
            }
            itemIDs[key, default: []].append(item.id)
        }
        return order.map { key in
            WatchFolderReviewGroup(id: key, folderName: names[key] ?? "", itemIDs: itemIDs[key] ?? [])
        }
    }
}

/// Whether the queue's review banner shows, so dismissing it lasts until something new is held.
///
/// Dismissing never changes the queue; the items stay held for review.
struct QueueReviewBannerState: Equatable {
    private(set) var dismissedItemIDs: Set<UUID> = []

    /// The groups to show: every held group, unless the banner was dismissed and nothing new has been held since.
    func visibleGroups(for items: [PersistentQueueItem]) -> [WatchFolderReviewGroup] {
        let groups = WatchFolderReviewGroup.groups(for: items)
        let heldItemIDs = Set(groups.flatMap(\.itemIDs))
        guard !heldItemIDs.isSubset(of: dismissedItemIDs) else {
            return []
        }
        return groups
    }

    mutating func dismiss(items: [PersistentQueueItem]) {
        dismissedItemIDs = Set(WatchFolderReviewGroup.groups(for: items).flatMap(\.itemIDs))
    }
}

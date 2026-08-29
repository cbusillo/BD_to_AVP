import Foundation

struct PersistentQueueOutcomeSummary: Equatable {
    let completedItemIDs: [UUID]
    let failedItemIDs: [UUID]
    let needsActionItemIDs: [UUID]

    init(items: [PersistentQueueItem]) {
        var completedItemIDs: [UUID] = []
        var failedItemIDs: [UUID] = []
        var needsActionItemIDs: [UUID] = []

        for item in items.sorted(by: { $0.ordinal < $1.ordinal }) {
            switch item.status {
            case .completed:
                completedItemIDs.append(item.id)
            case .failed:
                failedItemIDs.append(item.id)
            case .needsChoice, .interrupted, .attention:
                needsActionItemIDs.append(item.id)
            case .waiting, .inspecting, .processing, .stopping, .stopped, .notStarted:
                break
            }
        }

        self.completedItemIDs = completedItemIDs
        self.failedItemIDs = failedItemIDs
        self.needsActionItemIDs = needsActionItemIDs
    }

    var completedCount: Int { completedItemIDs.count }
    var failedCount: Int { failedItemIDs.count }
    var needsActionCount: Int { needsActionItemIDs.count }

    var hasAnyResults: Bool {
        completedCount > 0 || failedCount > 0 || needsActionCount > 0
    }
}

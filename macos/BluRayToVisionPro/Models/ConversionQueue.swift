import Foundation

enum ConversionQueueItemStatus: Equatable {
    case waiting
    case processing
    case attention(String)
    case completed(ConversionResult)
    case failed(String)
    case cancelled
}

struct ConversionQueueItem: Identifiable, Equatable {
    let id: UUID
    let draft: ConversionDraft
    var status: ConversionQueueItemStatus

    init(id: UUID = UUID(), draft: ConversionDraft, status: ConversionQueueItemStatus = .waiting) {
        self.id = id
        self.draft = draft
        self.status = status
    }

    var displayName: String {
        draft.selectedTitle?.name ?? draft.source.displayName
    }

    var plannedOutputURL: URL {
        draft.proposedOutputURL
    }
}

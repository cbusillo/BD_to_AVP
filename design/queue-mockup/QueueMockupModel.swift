import Foundation
import SwiftUI

@MainActor
final class QueueMockupModel: ObservableObject {
    @Published var scenario: MockScenario
    @Published var appearance: MockAppearance
    @Published var queueState: MockQueueState
    @Published var items: [MockQueueItem]
    @Published var selectedID: UUID?
    @Published var selectedTab = "Video"
    @Published var compactRows = false
    @Published var showActivity = false
    @Published var showStopConfirmation = false

    init() {
        let initialScenario = MockScenario(rawValue: commandLineValue(for: "--scenario") ?? "") ?? .running
        scenario = initialScenario
        appearance = MockAppearance(rawValue: commandLineValue(for: "--appearance") ?? "") ?? .system
        let fixture = MockFixtures.state(for: initialScenario)
        queueState = fixture.0
        items = fixture.1
        selectedID = fixture.2
        compactRows = scenario == .dense
    }

    var selectedItem: MockQueueItem? {
        guard let selectedID else {
            return nil
        }
        return items.first(where: { $0.id == selectedID })
    }

    var selectedIndex: Int? {
        guard let selectedID else {
            return nil
        }
        return items.firstIndex(where: { $0.id == selectedID })
    }

    var waitingItems: [MockQueueItem] {
        items.filter { $0.status.isWaiting || $0.status == .interrupted || $0.status == .stopped }
    }

    var activeItems: [MockQueueItem] {
        items.filter { $0.status.isActive }
    }

    var completedItems: [MockQueueItem] {
        items.filter { $0.status.isCompleted }
    }

    var attentionItems: [MockQueueItem] {
        items.filter { item in
            switch item.status {
            case .failed, .needsAttention, .unavailable, .paused:
                true
            default:
                false
            }
        }
    }

    var queueCountText: String {
        items.count == 1 ? "1 video" : "\(items.count) videos"
    }

    func load(_ scenario: MockScenario) {
        self.scenario = scenario
        let fixture = MockFixtures.state(for: scenario)
        queueState = fixture.0
        items = fixture.1
        selectedID = fixture.2
        compactRows = scenario == .dense
    }

    func addSyntheticSource() {
        let nextNumber = items.count + 1
        let newItem = MockQueueItem(
            id: UUID(),
            name: "New 3D Movie \(nextNumber).mkv",
            kind: .matroska,
            status: .waiting,
            location: "/Users/chris/Movies/Rips"
        )
        items.append(newItem)
        selectedID = newItem.id
        if queueState == .empty || queueState == .finished {
            queueState = .idle
        }
    }

    func removeSelected() {
        guard let selectedID, let index = items.firstIndex(where: { $0.id == selectedID }) else {
            return
        }
        if items[index].status.isActive {
            showStopConfirmation = true
            return
        }
        items.remove(at: index)
        self.selectedID = items.indices.contains(index) ? items[index].id : items.last?.id
        if items.isEmpty {
            queueState = .empty
        }
    }

    func moveSelected(by offset: Int) {
        guard let selectedID, let index = items.firstIndex(where: { $0.id == selectedID }) else {
            return
        }
        let destination = min(max(index + offset, 0), items.count - 1)
        guard destination != index, !items[index].status.isActive else {
            return
        }
        let item = items.remove(at: index)
        items.insert(item, at: destination)
    }

    func moveSelectedNext() {
        guard let selectedID, let index = items.firstIndex(where: { $0.id == selectedID }) else {
            return
        }
        let activeIndex = items.firstIndex(where: { $0.status.isActive })
        let destination = min((activeIndex ?? -1) + 1, items.count - 1)
        guard destination != index, !items[index].status.isActive else {
            return
        }
        let item = items.remove(at: index)
        items.insert(item, at: destination)
    }

    func clearCompleted() {
        items.removeAll(where: { $0.status.isCompleted })
        if selectedID.map({ id in items.contains(where: { $0.id == id }) }) != true {
            selectedID = items.first?.id
        }
        if items.isEmpty {
            queueState = .empty
        }
    }

    func startQueue() {
        guard !items.isEmpty else {
            return
        }
        queueState = .running
        if !items.contains(where: { $0.status.isActive }), let index = items.firstIndex(where: { $0.status.isWaiting }) {
            items[index].status = .converting(stage: "Preparing source video", progress: 0.18, elapsed: "0:08:42")
            selectedID = items[index].id
        }
    }

    func scheduleQueue() {
        queueState = .scheduled
        for index in items.indices where items[index].status.isWaiting {
            items[index].status = .scheduled("tonight at 11:00 PM")
        }
    }

    func pauseAfterCurrent() {
        queueState = .pausing
    }

    func finishPause() {
        queueState = .paused
        if let index = items.firstIndex(where: { $0.status.isActive }) {
            items[index].status = .paused("Encoding spatial video")
            selectedID = items[index].id
        }
    }

    func resumeQueue() {
        queueState = .running
        if let index = items.firstIndex(where: { item in
            switch item.status {
            case .paused, .interrupted, .stopped, .waiting, .scheduled:
                true
            default:
                false
            }
        }) {
            items[index].status = .converting(stage: "Encoding spatial video", progress: 0.42, elapsed: "0:43:18")
            selectedID = items[index].id
        }
    }

    func stopCurrent() {
        if let index = items.firstIndex(where: { $0.status.isActive }) {
            items[index].status = .stopped
            selectedID = items[index].id
        }
        queueState = .paused
        showStopConfirmation = false
    }

    func retrySelected() {
        guard let selectedID, let index = items.firstIndex(where: { $0.id == selectedID }) else {
            return
        }
        items[index].status = .waiting
        if queueState == .finished {
            queueState = .idle
        }
    }

    func tickProgress() {
        guard queueState == .running, let index = items.firstIndex(where: { $0.status.isActive }) else {
            return
        }
        guard case let .converting(stage, progress, elapsed) = items[index].status else {
            return
        }
        let nextProgress = progress >= 0.82 ? 0.32 : progress + 0.006
        items[index].status = .converting(stage: stage, progress: nextProgress, elapsed: elapsed)
    }
}

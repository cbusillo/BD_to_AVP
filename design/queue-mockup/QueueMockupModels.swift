import Foundation
import SwiftUI

enum MockScenario: String, CaseIterable, Identifiable {
    case empty
    case idle
    case running
    case paused
    case scheduled
    case restored
    case mixed
    case dense

    var id: String { rawValue }

    var title: String {
        switch self {
        case .empty:
            "Empty Queue"
        case .idle:
            "Ready to Start"
        case .running:
            "Running"
        case .paused:
            "Paused"
        case .scheduled:
            "Scheduled"
        case .restored:
            "Restored"
        case .mixed:
            "Morning Summary"
        case .dense:
            "Dense Queue"
        }
    }
}

enum MockAppearance: String, CaseIterable, Identifiable {
    case system
    case light
    case dark
    case contrast

    var id: String { rawValue }

    var title: String {
        switch self {
        case .system:
            "System"
        case .light:
            "Light"
        case .dark:
            "Dark"
        case .contrast:
            "Increased Contrast"
        }
    }

    var colorScheme: ColorScheme? {
        switch self {
        case .system, .contrast:
            nil
        case .light:
            .light
        case .dark:
            .dark
        }
    }
}

enum MockQueueState: Equatable {
    case empty
    case idle
    case running
    case pausing
    case paused
    case scheduled
    case restored
    case finished
}

enum MockSourceKind: String {
    case discImage
    case matroska
    case transportStream
    case bluRayFolder
    case physicalDisc

    var title: String {
        switch self {
        case .discImage:
            "Disc Image"
        case .matroska:
            "3D MKV"
        case .transportStream:
            "Transport Stream"
        case .bluRayFolder:
            "Blu-ray Folder"
        case .physicalDisc:
            "Blu-ray Disc"
        }
    }

    var systemImage: String {
        switch self {
        case .discImage:
            "opticaldiscdrive"
        case .matroska:
            "film.stack"
        case .transportStream:
            "externaldrive.badge.timemachine"
        case .bluRayFolder:
            "folder.badge.gearshape"
        case .physicalDisc:
            "opticaldisc"
        }
    }
}

enum MockItemStatus: Equatable {
    case waiting
    case scheduled(String)
    case inspecting(String)
    case converting(stage: String, progress: Double, elapsed: String)
    case paused(String)
    case needsAttention(String)
    case failed(String)
    case unavailable(String)
    case interrupted
    case completed(String)
    case stopped

    var title: String {
        switch self {
        case .waiting:
            "Waiting"
        case .scheduled:
            "Scheduled"
        case .inspecting:
            "Reading Source"
        case .converting:
            "Converting"
        case .paused:
            "Paused"
        case .needsAttention:
            "Needs Attention"
        case .failed:
            "Failed"
        case .unavailable:
            "Unavailable"
        case .interrupted:
            "Interrupted"
        case .completed:
            "Completed"
        case .stopped:
            "Stopped"
        }
    }

    var systemImage: String {
        switch self {
        case .waiting:
            "clock"
        case .scheduled:
            "calendar.badge.clock"
        case .inspecting:
            "doc.text.magnifyingglass"
        case .converting:
            "gearshape.2"
        case .paused:
            "pause.circle.fill"
        case .needsAttention:
            "exclamationmark.circle.fill"
        case .failed:
            "exclamationmark.triangle.fill"
        case .unavailable:
            "externaldrive.badge.xmark"
        case .interrupted:
            "arrow.counterclockwise.circle"
        case .completed:
            "checkmark.circle.fill"
        case .stopped:
            "stop.circle.fill"
        }
    }

    var tint: Color {
        switch self {
        case .waiting, .scheduled, .stopped:
            .secondary
        case .inspecting, .converting:
            .accentColor
        case .paused, .needsAttention, .interrupted:
            .orange
        case .failed, .unavailable:
            .red
        case .completed:
            .green
        }
    }

    var isWaiting: Bool {
        switch self {
        case .waiting, .scheduled:
            true
        default:
            false
        }
    }

    var isActive: Bool {
        switch self {
        case .inspecting, .converting:
            true
        default:
            false
        }
    }

    var isCompleted: Bool {
        if case .completed = self {
            return true
        }
        return false
    }

    var isEditable: Bool {
        switch self {
        case .waiting, .scheduled, .paused, .needsAttention, .failed, .unavailable, .interrupted, .stopped:
            true
        case .inspecting, .converting, .completed:
            false
        }
    }
}

struct MockQueueItem: Identifiable, Equatable {
    let id: UUID
    var name: String
    var kind: MockSourceKind
    var status: MockItemStatus
    var profile: String
    var location: String
    var destination: String
    var plannedOutput: String
    var duration: String
    var size: String
    var resolution: String
    var frameRate: String
    var selectedTitle: String?

    init(
        id: UUID,
        name: String,
        kind: MockSourceKind,
        status: MockItemStatus,
        profile: String = "Balanced",
        location: String = "/Volumes/Media/Rips",
        destination: String = "/Movies",
        plannedOutput: String? = nil,
        duration: String = "2:14:38",
        size: String = "38.2 GB",
        resolution: String = "1920 × 1080",
        frameRate: String = "23.976 fps",
        selectedTitle: String? = nil
    ) {
        self.id = id
        self.name = name
        self.kind = kind
        self.status = status
        self.profile = profile
        self.location = location
        self.destination = destination
        self.plannedOutput = plannedOutput ?? "\(URL(fileURLWithPath: name).deletingPathExtension().lastPathComponent).mov"
        self.duration = duration
        self.size = size
        self.resolution = resolution
        self.frameRate = frameRate
        self.selectedTitle = selectedTitle
    }

    var subtitle: String {
        switch status {
        case .waiting:
            "\(profile) · \(plannedOutput)"
        case .scheduled:
            "\(profile) · \(plannedOutput)"
        case let .inspecting(stage):
            stage
        case let .converting(stage, _, _):
            stage
        case let .paused(stage):
            "Paused after \(stage)"
        case let .needsAttention(message), let .failed(message), let .unavailable(message):
            message
        case .interrupted:
            "Will start again from the last safe stage"
        case let .completed(output):
            output
        case .stopped:
            "Ready to retry"
        }
    }
}

enum MockFixtures {
    static func state(for scenario: MockScenario) -> (MockQueueState, [MockQueueItem], UUID?) {
        let base = baseItems()
        switch scenario {
        case .empty:
            return (.empty, [], nil)
        case .idle:
            let items = base.map { item in
                var copy = item
                copy.status = .waiting
                return copy
            }
            return (.idle, items, items.first?.id)
        case .running:
            var items = base
            items[0].status = .completed("Aliens.mov")
            items[1].status = .converting(stage: "Encoding spatial video", progress: 0.58, elapsed: "1:12:31")
            items[2].status = .waiting
            items[3].status = .waiting
            items[4].status = .waiting
            return (.running, items, items[1].id)
        case .paused:
            var items = base
            items[0].status = .completed("Aliens.mov")
            items[1].status = .completed("Avatar.mov")
            items[2].status = .paused("Extracting 3D video")
            items[3].status = .waiting
            items[4].status = .waiting
            return (.paused, items, items[2].id)
        case .scheduled:
            var items = base.map { item in
                var copy = item
                copy.status = .waiting
                return copy
            }
            items[0].status = .scheduled("tonight at 11:00 PM")
            return (.scheduled, items, items.first?.id)
        case .restored:
            var items = base
            items[0].status = .completed("Aliens.mov")
            items[1].status = .interrupted
            items[2].status = .waiting
            items[3].status = .waiting
            items[4].status = .waiting
            return (.restored, items, items[1].id)
        case .mixed:
            var items = base
            items[0].status = .completed("Aliens.mov")
            items[1].status = .completed("Avatar.mov")
            items[2].status = .failed("Subtitle extraction needs review")
            items[3].status = .needsAttention("Choose how to retry this video")
            items[4].status = .unavailable("Source drive is not connected")
            return (.finished, items, items[2].id)
        case .dense:
            var items: [MockQueueItem] = []
            let names = [
                "Aliens", "Avatar", "Blade Runner 2049", "Coraline", "Dune", "Edge of Tomorrow",
                "Gravity", "Hugo", "Inside Out", "Jurassic Park", "Life of Pi", "Mad Max Fury Road",
                "Pacific Rim", "Prometheus", "Ready Player One", "The Martian", "Tron Legacy", "Wicked"
            ]
            for (index, name) in names.enumerated() {
                let status: MockItemStatus
                if index < 3 {
                    status = .completed("\(name).mov")
                } else if index == 3 {
                    status = .converting(stage: "Extracting MVC video", progress: 0.34, elapsed: "0:28:14")
                } else if index == 9 {
                    status = .failed("Output volume ran out of space")
                } else {
                    status = .waiting
                }
                items.append(
                    MockQueueItem(
                        id: uuid(index + 20),
                        name: "\(name).mkv",
                        kind: .matroska,
                        status: status,
                        profile: index.isMultiple(of: 4) ? "4K Upscale" : "Balanced"
                    )
                )
            }
            return (.running, items, items[3].id)
        }
    }

    private static func baseItems() -> [MockQueueItem] {
        [
            MockQueueItem(
                id: uuid(1),
                name: "Aliens.iso",
                kind: .discImage,
                status: .waiting,
                duration: "2:17:06",
                size: "42.8 GB"
            ),
            MockQueueItem(
                id: uuid(2),
                name: "Avatar.mkv",
                kind: .matroska,
                status: .waiting,
                profile: "Original Resolution",
                duration: "2:41:42",
                size: "46.1 GB"
            ),
            MockQueueItem(
                id: uuid(3),
                name: "Blade Runner 2049.iso",
                kind: .discImage,
                status: .waiting,
                profile: "4K Upscale",
                duration: "2:43:57",
                size: "44.7 GB"
            ),
            MockQueueItem(
                id: uuid(4),
                name: "Coraline.m2ts",
                kind: .transportStream,
                status: .waiting,
                duration: "1:40:36",
                size: "26.4 GB"
            ),
            MockQueueItem(
                id: uuid(5),
                name: "Dune 3D",
                kind: .bluRayFolder,
                status: .waiting,
                location: "/Volumes/NAS/Movies/Dune 3D",
                duration: "2:35:25",
                size: "39.5 GB"
            ),
        ]
    }

    private static func uuid(_ value: Int) -> UUID {
        UUID(uuidString: String(format: "00000000-0000-0000-0000-%012d", value))!
    }
}

func commandLineValue(for option: String) -> String? {
    let arguments = CommandLine.arguments
    for (index, argument) in arguments.enumerated() {
        if argument == option, arguments.indices.contains(index + 1) {
            return arguments[index + 1]
        }
        let prefix = "\(option)="
        if argument.hasPrefix(prefix) {
            return String(argument.dropFirst(prefix.count))
        }
    }
    return nil
}

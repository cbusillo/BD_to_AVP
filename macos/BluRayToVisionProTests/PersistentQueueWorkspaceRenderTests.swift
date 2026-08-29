import AppKit
import SwiftUI
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class PersistentQueueWorkspaceRenderTests: XCTestCase {
    func testQueueWorkspaceRendersEmptyAndDenseStatesAcrossAppearances() throws {
        let items = try makeItems()
        let appearances: [(ColorScheme, NSAppearance.Name)] = [
            (.light, .aqua),
            (.dark, .darkAqua),
        ]

        for (colorScheme, appearanceName) in appearances {
            try render(
                items: [],
                selectedItem: nil,
                compact: false,
                colorScheme: colorScheme,
                appearanceName: appearanceName
            )
            try render(
                items: items,
                selectedItem: items[1],
                compact: true,
                colorScheme: colorScheme,
                appearanceName: appearanceName
            )
        }
    }

    func testOffPeakScheduleEditorRendersPhysicalDiscWarning() throws {
        let start = Date(timeIntervalSince1970: 1_800_000_000)
        let content = OffPeakScheduleSheet(
            startAt: .constant(start),
            endAt: .constant(start.addingTimeInterval(8 * 60 * 60)),
            isEditing: false,
            hasPhysicalDiscItems: true,
            errorMessage: nil,
            cancel: {},
            save: {}
        )
        .frame(width: 560, height: 430)
        let hostingView = NSHostingView(rootView: content)
        hostingView.frame = NSRect(x: 0, y: 0, width: 560, height: 430)
        hostingView.layoutSubtreeIfNeeded()

        let bitmap = try XCTUnwrap(hostingView.bitmapImageRepForCachingDisplay(in: hostingView.bounds))
        hostingView.cacheDisplay(in: hostingView.bounds, to: bitmap)

        XCTAssertEqual(bitmap.pixelsWide, 560)
        XCTAssertEqual(bitmap.pixelsHigh, 430)
    }

    func testOffPeakScheduleArmedAndMissedBannersRender() throws {
        let items = try makeItems()
        let start = Date(timeIntervalSince1970: 1_800_000_000)
        let schedule = OffPeakQueueSchedule(
            startAt: start,
            endAt: start.addingTimeInterval(8 * 60 * 60),
            createdAt: start.addingTimeInterval(-60)
        )
        try render(
            items: items,
            selectedItem: items[1],
            compact: false,
            colorScheme: .light,
            appearanceName: .aqua,
            offPeakSchedule: schedule
        )
        try render(
            items: items,
            selectedItem: items[1],
            compact: false,
            colorScheme: .dark,
            appearanceName: .darkAqua,
            offPeakScheduleOutcome: .missed(
                scheduleID: schedule.id,
                reason: .windowEndedBeforeEvaluation,
                at: schedule.endAt
            )
        )
    }

    func testHeldRouteQualityChoiceRendersInPersistentQueueDetail() throws {
        let conflict = try makeRouteQualityConflict()
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/Movies/Held Feature.mkv")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies"),
            options: conflict.proposedOptions
        )
        let item = try PersistentQueueItem(item: DurableConversionQueueItem(
            ordinal: 0,
            origin: .singleSource,
            intent: DurableQueueItemIntent(draft: draft),
            state: .needsChoice,
            routeQualityConflict: DurableRouteQualityConflict(conflict: conflict)
        ))
        let group = try XCTUnwrap(QueueResolutionGroup.group([
            QueueResolutionCandidate(id: item.id, draft: item.draft, conflict: conflict),
        ]).first)
        XCTAssertEqual(
            item.queueManipulationLockReason,
            "Resolve this item's required choice before moving or editing it."
        )

        try render(
            items: [item],
            selectedItem: item,
            compact: false,
            colorScheme: .dark,
            appearanceName: .darkAqua,
            resolutionGroup: group
        )
    }

    func testQueueWorkspaceRendersIdentifiableRowsForDuplicateTitlesAndMediaSources() throws {
        let items = try makeIdentifiableItems()

        XCTAssertEqual(items.map(\.sourceIdentity), [
            "A Very Long Archive Name That Keeps Its Source Identity.mkv",
            "Avatar 3D",
            "Blade Runner 2049.iso",
            "Avatar 3D",
        ])
        XCTAssertEqual(items.map(\.selectedTitleIdentity), [
            "Main Movie",
            "Main Movie",
            "Main Movie",
            "Bonus Features",
        ])

        try render(
            items: items,
            selectedItem: items[1],
            compact: false,
            colorScheme: .light,
            appearanceName: .aqua
        )
        try render(
            items: items,
            selectedItem: items[2],
            compact: true,
            colorScheme: .dark,
            appearanceName: .darkAqua
        )
    }

    func testQueueWorkspaceRendersQueueManipulationAtMinimumSidebarWidth() throws {
        let items = try makeItems()
        XCTAssertEqual(
            items[0].queueManipulationLockReason,
            "This item is active and cannot move or be edited until it finishes."
        )
        XCTAssertEqual(
            items[1].queueManipulationLockReason,
            "Restart this interrupted item before changing its position or settings."
        )
        XCTAssertEqual(
            items[2].queueManipulationLockReason,
            "Retry this failed item before changing its position or settings."
        )
        XCTAssertEqual(
            items[3].queueManipulationLockReason,
            "This item is stopping and cannot move or be edited until it has stopped."
        )
        XCTAssertNil(items[4].queueManipulationLockReason)
        XCTAssertEqual(items[10].queueManipulationLockReason, "Completed items cannot move or be edited.")

        try render(
            items: items,
            selectedItem: items[4],
            compact: true,
            colorScheme: .dark,
            appearanceName: .darkAqua,
            sidebarWidth: 300
        )
    }

    private func render(
        items: [PersistentQueueItem],
        selectedItem: PersistentQueueItem?,
        compact: Bool,
        colorScheme: ColorScheme,
        appearanceName: NSAppearance.Name,
        offPeakSchedule: OffPeakQueueSchedule? = nil,
        offPeakScheduleOutcome: OffPeakScheduleOutcome? = nil,
        resolutionGroup: QueueResolutionGroup? = nil,
        sidebarWidth: CGFloat = 330
    ) throws {
        let content = HSplitView {
            PersistentQueueSidebarView(
                items: items,
                selectedID: .constant(selectedItem?.id),
                compactRows: .constant(compact),
                insertedDiscs: [],
                makeMKVAvailable: true,
                activeProgress: WorkerProgress(currentStage: 3, totalStages: 5, stageFraction: 0.61),
                activeElapsedText: "1:12:31",
                runState: .running,
                canStart: !items.isEmpty,
                canPauseAfterCurrent: !items.isEmpty,
                canStopCurrent: !items.isEmpty,
                canUndo: true,
                offPeakSchedule: offPeakSchedule,
                offPeakScheduleOutcome: offPeakScheduleOutcome,
                offPeakScheduleErrorMessage: nil,
                addSources: {},
                addSourceFolder: {},
                addDisc: { _ in },
                move: { _, _ in },
                moveRelative: { _, _, _ in },
                moveNext: { _ in },
                remove: { _ in },
                clearCompleted: {},
                undo: {},
                start: {},
                startLater: {},
                editSchedule: {},
                cancelSchedule: {},
                dismissScheduleOutcome: {},
                pauseAfterCurrent: {},
                stopCurrent: {}
            )
            .frame(width: sidebarWidth)
            PersistentQueueDetailView(
                item: selectedItem,
                resolutionGroup: resolutionGroup,
                resolutionMemoryStore: ResolutionMemoryStore.inMemory(),
                activeProgress: WorkerProgress(currentStage: 3, totalStages: 5, stageFraction: 0.61),
                activeElapsedText: "1:12:31",
                edit: { _ in },
                changeDestination: { _ in },
                retry: { _, _ in },
                resolveRouteQuality: { _, _ in }
            )
            .frame(minWidth: 600)
        }
        .frame(width: 920, height: 620)
        .preferredColorScheme(colorScheme)
        let hostingView = NSHostingView(rootView: content)
        hostingView.appearance = NSAppearance(named: appearanceName)
        hostingView.frame = NSRect(x: 0, y: 0, width: 920, height: 620)
        hostingView.layoutSubtreeIfNeeded()
        let bitmap = try XCTUnwrap(hostingView.bitmapImageRepForCachingDisplay(in: hostingView.bounds))
        hostingView.cacheDisplay(in: hostingView.bounds, to: bitmap)
        XCTAssertEqual(bitmap.pixelsWide, 920)
        XCTAssertEqual(bitmap.pixelsHigh, 620)
    }

    private func makeItems() throws -> [PersistentQueueItem] {
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24000/1001",
            interlaced: false
        )
        return try (0..<12).map { offset in
            let sourceURL = URL(fileURLWithPath: "/Volumes/Media/Rips/Feature-\(offset).mkv")
            let draft = ConversionDraft(
                source: ConversionSource(kind: .matroska, url: sourceURL),
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/Movies"),
                options: ConversionOptions()
            )
            let state: DurableQueueItemState
            let failure: DurableQueueFailure?
            let result: DurableQueueResult?
            switch offset {
            case 0:
                state = .processing
                failure = nil
                result = nil
            case 1:
                state = .interrupted
                failure = nil
                result = nil
            case 2:
                state = .failed
                failure = DurableQueueFailure(code: "temporary", message: "Source needs attention", details: nil, retryable: true)
                result = nil
            case 3:
                state = .stopping
                failure = nil
                result = nil
            case 10, 11:
                state = .completed
                failure = nil
                result = DurableQueueResult(outputPath: "/Movies/Feature-\(offset).mov")
            default:
                state = .waiting
                failure = nil
                result = nil
            }
            return try PersistentQueueItem(item: DurableConversionQueueItem(
                ordinal: offset,
                origin: .singleSource,
                intent: DurableQueueItemIntent(draft: draft),
                inspection: inspection,
                state: state,
                failure: failure,
                result: result
            ))
        }
    }

    private func makeIdentifiableItems() throws -> [PersistentQueueItem] {
        let title = SourceTitle(
            id: "main",
            name: "Main Movie",
            outputName: "Main Movie",
            durationSeconds: 7_200,
            resolution: "1920x1080",
            frameRate: "24000/1001",
            mainFeature: true
        )
        let bonusTitle = SourceTitle(
            id: "bonus",
            name: "Bonus Features",
            outputName: "Bonus Features",
            durationSeconds: 1_800,
            resolution: "1920x1080",
            frameRate: "24000/1001",
            mainFeature: false
        )
        let fixtures: [(source: ConversionSource, title: SourceTitle, state: DurableQueueItemState)] = [
            (
                ConversionSource(
                    kind: .matroska,
                    url: URL(fileURLWithPath: "/Volumes/Archive/A Very Long Archive Name That Keeps Its Source Identity.mkv")
                ),
                title,
                .waiting
            ),
            (
                ConversionSource(
                    kind: .physicalDisc,
                    url: URL(fileURLWithPath: "/Volumes/Avatar 3D"),
                    displayName: "Avatar 3D",
                    mediaIdentifier: "disk4s1"
                ),
                title,
                .processing
            ),
            (
                ConversionSource(
                    kind: .discImage,
                    url: URL(fileURLWithPath: "/Volumes/Rips/Blade Runner 2049.iso")
                ),
                title,
                .waiting
            ),
            (
                ConversionSource(
                    kind: .physicalDisc,
                    url: URL(fileURLWithPath: "/Volumes/Avatar 3D"),
                    displayName: "Avatar 3D",
                    mediaIdentifier: "disk4s1"
                ),
                bonusTitle,
                .completed
            ),
        ]

        return try fixtures.enumerated().map { offset, fixture in
            let draft = ConversionDraft(
                source: fixture.source,
                sourceDetails: nil,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/Movies"),
                options: ConversionOptions(),
                selectedTitle: fixture.title
            )
            return try PersistentQueueItem(item: DurableConversionQueueItem(
                ordinal: offset,
                origin: .multiTitle,
                intent: DurableQueueItemIntent(draft: draft),
                state: fixture.state,
                result: fixture.state == .completed ? DurableQueueResult(outputPath: "/Movies/Avatar Bonus Features.mov") : nil
            ))
        }
    }

    private func makeRouteQualityConflict() throws -> RouteQualityConflict {
        var options = ConversionOptions()
        try options.encoding.selectQualityStep(.maximumDetail)
        guard case let .conflict(conflict) = RouteQualityEngine.propose(
            options: options,
            edit: .reusableIntermediates(true)
        ) else {
            throw NSError(domain: "PersistentQueueWorkspaceRenderTests", code: 1)
        }
        return conflict
    }
}

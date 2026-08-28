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

    private func render(
        items: [PersistentQueueItem],
        selectedItem: PersistentQueueItem?,
        compact: Bool,
        colorScheme: ColorScheme,
        appearanceName: NSAppearance.Name
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
                offPeakSchedule: nil,
                offPeakScheduleOutcome: nil,
                offPeakScheduleErrorMessage: nil,
                addSources: {},
                addSourceFolder: {},
                addDisc: { _ in },
                move: { _, _ in },
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
            .frame(minWidth: 300, idealWidth: 330, maxWidth: 420)
            PersistentQueueDetailView(
                item: selectedItem,
                activeProgress: WorkerProgress(currentStage: 3, totalStages: 5, stageFraction: 0.61),
                activeElapsedText: "1:12:31",
                edit: { _ in },
                changeDestination: { _ in },
                retry: { _, _ in }
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
}

import Foundation
import XCTest
@testable import BluRayToVisionPro

final class StorageForecastTests: XCTestCase {
    func testKnownForecastSeparatesWorkingRetainedMarginAndPeak() {
        let draft = makeDraft(durationSeconds: 7_200)
        let forecast = StorageForecastItem(draft: draft)

        XCTAssertNotNil(forecast.estimatedOutputBytes)
        XCTAssertNotNil(forecast.temporaryWorkingBytes)
        XCTAssertEqual(forecast.retainedIntermediateBytes, 0)
        XCTAssertEqual(
            forecast.safetyMarginBytes,
            StorageForecastItem.safetyMargin(for: forecast.estimatedOutputBytes! + forecast.temporaryWorkingBytes!)
        )
        XCTAssertEqual(
            forecast.totalPeakRequiredBytes,
            (forecast.estimatedOutputBytes ?? 0)
                + (forecast.temporaryWorkingBytes ?? 0)
                + (forecast.safetyMarginBytes ?? 0)
        )
        XCTAssertTrue(forecast.totalPeakDescription.hasPrefix("Up to "))
    }

    func testUnknownForecastNeverProducesBlockingRequiredBytes() {
        var options = ConversionOptions()
        options.encoding.videoOutputMode = .av1Stereo
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/Sources/movie.mkv")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies", isDirectory: true),
            options: options
        )

        let forecast = StorageForecastItem(draft: draft)

        XCTAssertNil(forecast.totalPeakRequiredBytes)
        XCTAssertFalse(forecast.isEstimated)
        XCTAssertTrue(forecast.assumptionDescription.contains("CRF"))
    }

    func testSafetyMarginQuantizesAndClampsConservatively() {
        let gibibyte = StorageForecastFormatting.gibibyte

        XCTAssertEqual(StorageForecastItem.safetyMargin(for: 1), 2 * gibibyte)
        XCTAssertEqual(StorageForecastItem.safetyMargin(for: 25 * gibibyte), 3 * gibibyte)
        XCTAssertEqual(StorageForecastItem.safetyMargin(for: 500 * gibibyte), 20 * gibibyte)
    }

    func testReusableIntermediatesAreReported() {
        var options = ConversionOptions()
        options.job.intermediatePolicy = .reusable
        let forecast = StorageForecastItem(draft: makeDraft(durationSeconds: 3_600, options: options))

        XCTAssertNotNil(forecast.retainedIntermediateBytes)
        XCTAssertGreaterThan(forecast.retainedIntermediateBytes ?? 0, 0)
    }

    func testQueueSummaryGroupsDestinationsInOrdinalOrderAndMarksPartialTotals() throws {
        let first = try makeItem(
            ordinal: 2,
            destination: "/Volumes/B",
            durationSeconds: 3_600
        )
        let second = try makeItem(
            ordinal: 0,
            destination: "/Volumes/A",
            durationSeconds: 3_600
        )
        let unknown = try makeItem(
            ordinal: 1,
            destination: "/Volumes/A",
            durationSeconds: nil,
            av1: true
        )

        let summary = QueueStorageSummary(items: [first, second, unknown])

        XCTAssertEqual(summary.destinations.map(\.destinationPath), ["/Volumes/A", "/Volumes/B"])
        XCTAssertEqual(summary.destinations[0].estimatedItemCount, 1)
        XCTAssertEqual(summary.destinations[0].unestimatedItemCount, 1)
        XCTAssertNil(summary.destinations[0].totalPeakRequiredBytes)
        XCTAssertNotNil(summary.destinations[1].totalPeakRequiredBytes)
    }

    func testQueueSummaryRecomputesWhenOrderOrSettingsChange() throws {
        let lowBitrate = try makeItem(
            ordinal: 0,
            destination: "/Volumes/A",
            durationSeconds: 3_600,
            bitrateMbps: 40
        )
        let highBitrate = try makeItem(
            ordinal: 1,
            destination: "/Volumes/A",
            durationSeconds: 3_600,
            bitrateMbps: 200
        )
        let original = try XCTUnwrap(QueueStorageSummary(items: [lowBitrate, highBitrate]).destinations.first)

        let reorderedLow = try makeItem(
            ordinal: 1,
            destination: "/Volumes/A",
            durationSeconds: 3_600,
            bitrateMbps: 40
        )
        let reorderedHigh = try makeItem(
            ordinal: 0,
            destination: "/Volumes/A",
            durationSeconds: 3_600,
            bitrateMbps: 200
        )
        let reordered = try XCTUnwrap(
            QueueStorageSummary(items: [reorderedLow, reorderedHigh]).destinations.first
        )

        let editedHigh = try makeItem(
            ordinal: 1,
            destination: "/Volumes/A",
            durationSeconds: 3_600,
            bitrateMbps: 300
        )
        let edited = try XCTUnwrap(QueueStorageSummary(items: [lowBitrate, editedHigh]).destinations.first)

        XCTAssertNotEqual(original.totalPeakRequiredBytes, reordered.totalPeakRequiredBytes)
        XCTAssertGreaterThan(edited.estimatedOutputBytes ?? 0, original.estimatedOutputBytes ?? 0)
        XCTAssertGreaterThan(edited.totalPeakRequiredBytes ?? 0, original.totalPeakRequiredBytes ?? 0)
    }

    func testHeldItemsRemainVisibleAsUnestimated() throws {
        let item = try makeItem(
            ordinal: 0,
            destination: "/Volumes/A",
            durationSeconds: 3_600
        )
        let held = PersistentQueueItem(
            id: item.id,
            ordinal: item.ordinal,
            draft: item.draft,
            status: .attention(DurableQueueDecision(
                identifier: "subtitle_decision_required",
                prompt: "Choose subtitle handling.",
                choices: ["continue_without_subtitles"]
            ))
        )

        let destination = try XCTUnwrap(QueueStorageSummary(items: [held]).destinations.first)

        XCTAssertEqual(destination.estimatedItemCount, 0)
        XCTAssertEqual(destination.unestimatedItemCount, 1)
        XCTAssertTrue(destination.unestimatedReasons.first?.contains("recovery action") == true)
        XCTAssertNil(destination.totalPeakRequiredBytes)
    }

    func testSystemPreflightDistinguishesAvailabilityAndCapacity() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("storage-preflight-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let available = SystemQueueStoragePreflight(
            capacityProvider: StubCapacityProvider(
                readings: PreviewCapacityReadings(
                    importantUsageBytes: 100 * StorageForecastFormatting.gibibyte,
                    availableBytes: 100 * StorageForecastFormatting.gibibyte,
                    writable: true
                )
            )
        )
        XCTAssertEqual(
            available.preflight(destinationURL: directory, requiredBytes: 10 * StorageForecastFormatting.gibibyte),
            .available
        )

        let unconfirmed = SystemQueueStoragePreflight(
            capacityProvider: StubCapacityProvider(
                readings: PreviewCapacityReadings(importantUsageBytes: nil, availableBytes: nil, writable: true)
            )
        )
        guard case .unconfirmed = unconfirmed.preflight(destinationURL: directory, requiredBytes: nil) else {
            return XCTFail("Expected an unconfirmed capacity verdict")
        }

        let insufficient = SystemQueueStoragePreflight(
            capacityProvider: StubCapacityProvider(
                readings: PreviewCapacityReadings(
                    importantUsageBytes: 5 * StorageForecastFormatting.gibibyte,
                    availableBytes: 5 * StorageForecastFormatting.gibibyte,
                    writable: true
                )
            )
        )
        XCTAssertEqual(
            insufficient.preflight(destinationURL: directory, requiredBytes: 10 * StorageForecastFormatting.gibibyte),
            .insufficient(
                requiredBytes: 10 * StorageForecastFormatting.gibibyte,
                availableBytes: 5 * StorageForecastFormatting.gibibyte
            )
        )

        let unavailable = SystemQueueStoragePreflight(
            capacityProvider: StubCapacityProvider(
                readings: PreviewCapacityReadings(
                    importantUsageBytes: 100 * StorageForecastFormatting.gibibyte,
                    availableBytes: 100 * StorageForecastFormatting.gibibyte,
                    readOnly: true
                )
            )
        )
        guard case .unavailable = unavailable.preflight(destinationURL: directory, requiredBytes: nil) else {
            return XCTFail("Expected an unavailable destination verdict")
        }

        guard case .unavailable = available.preflight(
            destinationURL: directory.appendingPathComponent("missing", isDirectory: true),
            requiredBytes: nil
        ) else {
            return XCTFail("Expected a missing destination verdict")
        }
    }

    private func makeDraft(
        durationSeconds: Double,
        options: ConversionOptions = ConversionOptions()
    ) -> ConversionDraft {
        var options = options
        options.encoding.mvHEVC.directFinalBitrate = BitratePreference(mode: .custom, customMbps: 40)
        return ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/Sources/movie.mkv")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies", isDirectory: true),
            options: options,
            selectedTitle: SourceTitle(
                id: "title-1",
                name: "Movie",
                outputName: "Movie",
                durationSeconds: durationSeconds,
                resolution: "1920x1080",
                frameRate: "24/1",
                mainFeature: true
            )
        )
    }

    private func makeItem(
        ordinal: Int,
        destination: String,
        durationSeconds: Double?,
        av1: Bool = false,
        bitrateMbps: Int = 40
    ) throws -> PersistentQueueItem {
        var options = ConversionOptions()
        options.encoding.videoOutputMode = av1 ? .av1Stereo : .mvHEVC
        if !av1 {
            options.encoding.mvHEVC.directFinalBitrate = BitratePreference(
                mode: .custom,
                customMbps: bitrateMbps
            )
        }
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: "/Sources/movie-\(ordinal).mkv")),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: destination, isDirectory: true),
            options: options,
            selectedTitle: durationSeconds.map {
                SourceTitle(
                    id: "title-\(ordinal)",
                    name: "Movie \(ordinal)",
                    outputName: "Movie \(ordinal)",
                    durationSeconds: $0,
                    resolution: "1920x1080",
                    frameRate: "24/1",
                    mainFeature: true
                )
            }
        )
        return try PersistentQueueItem(item: DurableConversionQueueItem(
            ordinal: ordinal,
            origin: .singleSource,
            intent: DurableQueueItemIntent(draft: draft)
        ))
    }
}

private struct StubCapacityProvider: PreviewCapacityProviding {
    let readings: PreviewCapacityReadings

    func readings(for workspaceURL: URL) -> PreviewCapacityReadings {
        readings
    }
}

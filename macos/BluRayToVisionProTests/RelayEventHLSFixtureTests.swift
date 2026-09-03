import Foundation
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class RelayEventHLSFixtureTests: XCTestCase {
    func testLoadsEventPlaylistInSourceOrder() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture) }

        let loaded = try RelayEventHLSFixture.load(directory: fixture)

        XCTAssertEqual(loaded.initializationResourceIdentifier, "init.mp4")
        XCTAssertEqual(loaded.targetDuration, 4)
        XCTAssertEqual(loaded.segments.map(\.resourceIdentifier), ["media/00001.m4s", "media/00002.m4s"])
        XCTAssertEqual(loaded.segments.map(\.duration), [4, 3.5])
    }

    func testRejectsPlaylistWithMissingSegment() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture) }
        try "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nmissing.m4s\n".write(
            to: fixture.appendingPathComponent("media.m3u8"),
            atomically: true,
            encoding: .utf8
        )

        XCTAssertThrowsError(try RelayEventHLSFixture.load(directory: fixture)) { error in
            XCTAssertEqual(error as? RelayEventHLSFixtureError, .missingSegment("missing.m4s"))
        }
    }

    func testHostRetainsFixturePlaylistUsingConfiguredRetentionLimit() async throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture) }
        let host = try RelayHost.start(
            configuration: try RelayHostConfiguration(fixtureDirectory: fixture, retainedSegmentLimit: 1)
        )
        try await host.ingestEventHLSFixture()

        let snapshot = try await host.currentPlaylistSnapshot()

        XCTAssertEqual(snapshot.segments.map(\.resourceIdentifier), ["media/00002.m4s"])
        XCTAssertEqual(snapshot.earliestPlayableTimeMilliseconds, 4_000)
        XCTAssertEqual(snapshot.totalDurationMilliseconds, 7_500)
        XCTAssertFalse(snapshot.isFinalized)
    }

    func testControllerCancelsStartedRelayDeterministically() async throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture) }
        let session = RecordingRelaySession()
        let controller = RelayHostSessionController { _, _ in session }

        await controller.start(directory: fixture)
        XCTAssertEqual(controller.lifecycle, .advertising)
        XCTAssertTrue(controller.isSessionActive)
        XCTAssertEqual(controller.segmentCount, 2)
        XCTAssertNotNil(controller.formattedPairingCode)

        await controller.cancel()
        XCTAssertEqual(controller.lifecycle, .cancelled)
        XCTAssertFalse(controller.isSessionActive)
        XCTAssertEqual(session.cancelCount, 1)
    }

    func testControllerStopsStartedRelayDeterministically() async throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture) }
        let session = RecordingRelaySession()
        let controller = RelayHostSessionController { _, _ in session }

        await controller.start(directory: fixture)
        await controller.stop()

        XCTAssertEqual(controller.lifecycle, .stopped)
        XCTAssertFalse(controller.isSessionActive)
        XCTAssertEqual(session.stopCount, 1)
    }

    func testBothAppInfoPlistsDeclareRelayBonjourAndLocalNetworkUsage() throws {
        let macosRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        for filename in ["Info.plist", "Info-Release.plist"] {
            let infoURL = macosRoot.appendingPathComponent("BluRayToVisionPro/\(filename)")
            let dictionary = try XCTUnwrap(NSDictionary(contentsOf: infoURL) as? [String: Any])
            XCTAssertTrue((dictionary["NSBonjourServices"] as? [String])?.contains("_bdtoavp-relay._tcp") == true)
            XCTAssertFalse((dictionary["NSLocalNetworkUsageDescription"] as? String)?.isEmpty ?? true)
        }
    }

    private func makeFixture() throws -> URL {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("RelayEventHLSFixtureTests-\(UUID().uuidString)", isDirectory: true)
        let media = root.appendingPathComponent("media", isDirectory: true)
        try FileManager.default.createDirectory(at: media, withIntermediateDirectories: true)
        try Data("init".utf8).write(to: root.appendingPathComponent("init.mp4"))
        try Data("first".utf8).write(to: media.appendingPathComponent("00001.m4s"))
        try Data("second".utf8).write(to: media.appendingPathComponent("00002.m4s"))
        try """
        #EXTM3U
        #EXT-X-VERSION:7
        #EXT-X-PLAYLIST-TYPE:EVENT
        #EXT-X-TARGETDURATION:4
        #EXT-X-MAP:URI="init.mp4"
        #EXTINF:4,
        media/00001.m4s
        #EXTINF:3.5,
        media/00002.m4s
        """.write(to: root.appendingPathComponent("media.m3u8"), atomically: true, encoding: .utf8)
        return root
    }
}

@MainActor
private final class RecordingRelaySession: RelayHostSessionControlling {
    private(set) var cancelCount = 0
    private(set) var stopCount = 0

    func cancel() async {
        cancelCount += 1
    }

    func stop() async {
        stopCount += 1
    }

    func stopForAppQuit() async {}

    func currentLifecycle() async -> RelayHostLifecycle { .pairing }
}

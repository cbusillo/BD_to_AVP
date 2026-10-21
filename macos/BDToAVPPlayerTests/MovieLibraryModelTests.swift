import AVFoundation
import Foundation
import XCTest
@testable import BDToAVPPlayer

@MainActor
final class MovieLibraryModelTests: XCTestCase {
    private let start = Date(timeIntervalSince1970: 1_700_000_000)

    private func makeModel() async throws -> (MovieLibraryModel, FakeLiveMac, ManualLiveClock) {
        let clock = ManualLiveClock(start)
        let mac = try await FakeLiveMac.make(clock: clock)
        let model = MovieLibraryModel(relayTransport: await mac.makeRelayTransport(), clock: clock)
        return (model, mac, clock)
    }

    private func attach(_ model: MovieLibraryModel, _ mac: FakeLiveMac, titles: [SharedLiveTitle] = [makeLiveTitle()]) {
        model.attach(mac, name: "Studio", catalog: makeLiveCatalog(titles))
    }

    // MARK: Start, status, and ownership

    func testStartPollsStatusAndInstalledOutcomeTransfersCleanupToPlayback() async throws {
        let (model, mac, _) = try await makeModel()
        await mac.set(statusStates: [.starting, .starting, .live])
        attach(model, mac)
        var prepared: (RelayRemotePlaybackConfiguration, RelayLivePlaybackContext)?

        model.startLive(makeLiveTitle()) { configuration, context in
            prepared = (configuration, context)
            return .installed
        }
        await waitUntil { model.livePlayback != nil }

        let startCalls = await mac.startCalls
        let call = try XCTUnwrap(startCalls.first)
        XCTAssertEqual(call.signedAt.unixMilliseconds, call.request.issuedAt, "issuedAt is the first request's signed time")
        XCTAssertEqual(call.request.audioPID, 4352)
        let statusCalls = await mac.statusCalls
        XCTAssertEqual(statusCalls, 3)
        let (configuration, context) = try XCTUnwrap(prepared)
        XCTAssertEqual(configuration.session.sessionID.rawValue, context.streamID.uuidString.lowercased())
        XCTAssertNotEqual(configuration.session.sessionID, mac.parent.client.sessionID)
        XCTAssertNil(model.startingLiveTitle)
        let running = await mac.isStreamRunning
        XCTAssertTrue(running, "An installed stream keeps running until Done")
        let cancels = await mac.cancelCalls
        XCTAssertTrue(cancels.isEmpty)

        let cleanup = try XCTUnwrap(model.livePlayback?.stop())
        model.finishLivePlayback()
        let result = await cleanup.value
        XCTAssertEqual(result, .done(.streamCancel))
        let stopped = await mac.stoppedStreams[context.streamID]
        XCTAssertEqual(stopped, "viewerStopped")
    }

    func testARuntimeFailureOfInstalledPlaybackStopsTheStreamWithItsDerivedCancel() async throws {
        let (model, mac, _) = try await makeModel()
        attach(model, mac)
        let session = MVHEVCPlayerSession()
        model.startLive(makeLiveTitle()) { configuration, context in
            guard let source = try? RelayRemotePlaybackSource(
                session: configuration.session, serverBaseURL: configuration.serverBaseURL
            ) else { return .failed(.relayPreparationFailed("source")) }
            session.installRelayPlaybackResources(
                source: source,
                transport: configuration.transport,
                item: AVPlayerItem(url: URL(fileURLWithPath: "/dev/null")),
                mediaItem: MediaItem(
                    id: "relay:\(context.streamID.uuidString.lowercased())", title: context.title,
                    fileName: "live.m3u8", format: .mvHEVC
                ),
                observeItemStatus: false,
                startRefreshLoop: false,
                live: context
            )
            return .installed
        }
        await waitUntil { model.livePlayback != nil }
        let streamID = try XCTUnwrap(model.livePlayback?.streamID)
        let runningBefore = await mac.isStreamRunning
        XCTAssertTrue(runningBefore)

        await mac.failStream()
        await session.refreshRelayWindowNow()

        XCTAssertEqual(session.state, .failed)
        XCTAssertNil(model.livePlayback, "the player's failure hands the stream back for cleanup")
        await waitUntil { await mac.stoppedStreams[streamID] == "viewerStopped" }
        let paths = await mac.relayPaths
        XCTAssertTrue(paths.contains(RelayWireContract.cancelPath), "stopped with the stream's own derived cancel")
        let startIDCancels = await mac.cancelCalls
        XCTAssertTrue(startIDCancels.isEmpty)
    }

    func testRevocationDuringStartupShowsThePairingMessageAtOnce() async throws {
        let (model, mac, clock) = try await makeModel()
        await mac.set(statusStates: [.starting])
        attach(model, mac)
        await mac.revokeSession()
        let started = clock.now()

        model.startLive(makeLiveTitle()) { _, _ in .installed }
        await waitUntil { model.errorMessage != nil }

        XCTAssertEqual(model.errorMessage, LiveStartFailure.needsPairing.message)
        XCTAssertLessThan(clock.now().timeIntervalSince(started), 5, "no wait for the two-minute timeout")
        let statusCalls = await mac.statusCalls
        XCTAssertEqual(statusCalls, 1)
        XCTAssertNil(model.livePlayback)
    }

    func testFailedAndSupersededPreparationStopTheStream() async throws {
        for outcome in [RelayPlaybackPreparationOutcome.failed(.relayPreparationFailed("x")), .superseded] {
            let (model, mac, _) = try await makeModel()
            attach(model, mac)
            model.startLive(makeLiveTitle()) { _, _ in outcome }
            await waitUntil { await !mac.stoppedStreams.isEmpty }
            XCTAssertNil(model.livePlayback, "Only an installed stream hands cleanup to playback")
            let running = await mac.isStreamRunning
            XCTAssertFalse(running)
            let paths = await mac.relayPaths
            XCTAssertEqual(paths, [RelayWireContract.cancelPath], "\(outcome) sends the derived stream cancel")
        }
    }

    func testMacReadyTimeoutFailureShowsReasonAndStopsWithStreamCancel() async throws {
        let (model, mac, _) = try await makeModel()
        await mac.set(
            statusStates: [.starting, .failed],
            failure: LiveStreamStatus.Failure(code: "live_start_timeout", message: "The live stream did not start in time.")
        )
        attach(model, mac)
        var prepared = false
        model.startLive(makeLiveTitle()) { _, _ in prepared = true; return .installed }
        await waitUntil { model.errorMessage != nil }
        XCTAssertFalse(prepared)
        XCTAssertEqual(model.errorMessage, "Live playback stopped on the Mac: The live stream did not start in time.")
        XCTAssertFalse(model.errorMessage?.contains("Pair") ?? true)
        await waitUntil { await mac.relayPaths == [RelayWireContract.cancelPath] }
    }

    func testClientTimeoutAfterTwoMinutesStopsTheStream() async throws {
        let (model, mac, clock) = try await makeModel()
        await mac.set(statusStates: [.starting])
        attach(model, mac)
        model.startLive(makeLiveTitle()) { _, _ in .installed }
        await waitUntil { model.errorMessage != nil }
        XCTAssertEqual(model.errorMessage, LiveStartFailure.timedOut.message)
        XCTAssertGreaterThanOrEqual(clock.now().timeIntervalSince(start), LiveStartTiming.clientTimeout)
        await waitUntil { await mac.relayPaths == [RelayWireContract.cancelPath] }
        let running = await mac.isStreamRunning
        XCTAssertFalse(running)
    }

    func testUserCancelDuringStartupStopsTheStream() async throws {
        let (model, mac, _) = try await makeModel()
        await mac.set(statusStates: [.starting])
        await mac.holdStatus()
        attach(model, mac)
        model.startLive(makeLiveTitle()) { _, _ in .installed }
        await waitUntil { await mac.statusCalls >= 1 }
        model.cancelLiveStart()
        await waitUntil { await !mac.isStreamRunning }
        let paths = await mac.relayPaths
        XCTAssertEqual(paths, [RelayWireContract.cancelPath], "The stream ID is known, so cleanup uses the stream cancel")
        await mac.releaseStatus()
        XCTAssertNil(model.startingLiveTitle)
        XCTAssertNil(model.errorMessage)
    }

    func testLostStartResponseIsRecoveredByRetryWithTheSameIdentity() async throws {
        let (model, mac, _) = try await makeModel()
        await mac.set(startMode: .admitAndLoseResponse)
        attach(model, mac)
        Task { @MainActor in
            await waitUntil { await mac.startCalls.count == 1 }
            await mac.set(startMode: .admit)
        }
        model.startLive(makeLiveTitle()) { _, _ in .installed }
        await waitUntil { model.livePlayback != nil }
        let calls = await mac.startCalls
        XCTAssertEqual(calls.count, 2)
        XCTAssertEqual(calls[0].request, calls[1].request, "A retry repeats the start body")
        XCTAssertGreaterThan(calls[1].signedAt, calls[0].signedAt, "A retry is freshly signed")
    }

    func testChoosingAnotherMacStopsAStartInProgress() async throws {
        let (model, mac, _) = try await makeModel()
        await mac.set(statusStates: [.starting])
        await mac.holdStatus()
        attach(model, mac)
        model.startLive(makeLiveTitle()) { _, _ in .installed }
        await waitUntil { await mac.statusCalls >= 1 }
        model.discover()
        model.stopDiscovery()
        await waitUntil { await !mac.isStreamRunning }
        await mac.releaseStatus()
    }

    func testBusyMacShowsTheConvertingNotice() async throws {
        let (model, mac, _) = try await makeModel()
        await mac.set(statusStates: [.starting], busy: true)
        attach(model, mac)
        model.startLive(makeLiveTitle()) { _, _ in .installed }
        await waitUntil { model.liveNotice != nil }
        XCTAssertEqual(model.liveNotice, "The Mac is busy converting; playback may pause.")
        model.cancelLiveStart()
    }

    func testRefusedStartShowsTheMacReasonAndSendsNoCleanup() async throws {
        let (model, mac, _) = try await makeModel()
        await mac.set(startMode: .refuse(.busy))
        attach(model, mac)
        model.startLive(makeLiveTitle()) { _, _ in .installed }
        await waitUntil { model.errorMessage != nil }
        XCTAssertEqual(model.errorMessage, "The Mac is already streaming to another device.")
        try? await Task.sleep(for: .milliseconds(50))
        let cancels = await mac.cancelCalls
        let releases = await mac.releaseCalls
        XCTAssertTrue(cancels.isEmpty && releases.isEmpty)
    }

    // MARK: Catalog

    func testCheckingRowsRefreshUntilResolved() async throws {
        let (model, mac, _) = try await makeModel()
        await mac.set(catalogs: [
            makeLiveCatalog([makeLiveTitle(state: .checking)]),
            makeLiveCatalog([makeLiveTitle(state: .eligible)]),
        ])
        attach(model, mac, titles: [makeLiveTitle(state: .checking)])
        XCTAssertTrue(model.isCheckingRefreshActive)
        await waitUntil { model.liveTitles.first?.state == .eligible }
        await waitUntil { !model.isCheckingRefreshActive }
        let calls = await mac.catalogCalls
        XCTAssertEqual(calls, 2)
    }

    func testCheckingRefreshStopsAfterTwoMinutesAndOnNavigation() async throws {
        let (model, mac, clock) = try await makeModel()
        await mac.set(catalogs: [makeLiveCatalog([makeLiveTitle(state: .checking)])])
        attach(model, mac, titles: [makeLiveTitle(state: .checking)])
        await waitUntil { !model.isCheckingRefreshActive }
        let calls = await mac.catalogCalls
        XCTAssertEqual(calls, Int(MovieLibraryModel.checkingRefreshLimit / MovieLibraryModel.checkingRefreshInterval))
        XCTAssertGreaterThanOrEqual(clock.now().timeIntervalSince(start), MovieLibraryModel.checkingRefreshLimit)

        attach(model, mac, titles: [makeLiveTitle(state: .checking)])
        XCTAssertTrue(model.isCheckingRefreshActive)
        model.stopDiscovery()
        XCTAssertFalse(model.isCheckingRefreshActive)
    }

    func testIneligibleAndCheckingTitlesCannotStart() async throws {
        let (model, mac, _) = try await makeModel()
        attach(model, mac, titles: [makeLiveTitle(state: .ineligible, name: "A"), makeLiveTitle(state: .checking, name: "B")])
        model.stopCheckingRefresh()
        for title in model.liveTitles {
            model.startLive(title) { _, _ in .installed }
        }
        XCTAssertNil(model.liveOperation)
        let calls = await mac.startCalls
        XCTAssertTrue(calls.isEmpty)
    }
}

/// The cleanup routes, chosen by what the player knows and how long ago the start was issued.
final class LiveStreamCleanupTests: XCTestCase {
    private let issued = Date(timeIntervalSince1970: 1_700_000_000)

    private func admittedStart(_ mac: FakeLiveMac) async throws -> (LiveStartRequest, UUID) {
        let request = LiveStartRequest(
            startID: UUID(), issuedAt: issued.unixMilliseconds,
            titleID: makeLiveTitle().id, revision: makeLiveTitle().revision, audioPID: 4352
        )
        guard case let .started(response) = try await mac.startLive(request, signedAt: issued) else {
            throw XCTSkip("start not admitted")
        }
        return (request, response.streamID)
    }

    private func cleanup(_ mac: FakeLiveMac, _ request: LiveStartRequest, streamID: UUID?, clock: ManualLiveClock) async -> LiveStreamCleanup {
        LiveStreamCleanup(
            reference: LiveStartReference(startID: request.startID, issuedAt: request.issuedAt),
            streamID: streamID, client: mac, relayTransport: await mac.makeRelayTransport(), clock: clock
        )
    }

    func testStartIDCancelAt39Seconds() async throws {
        let clock = ManualLiveClock(issued)
        let mac = try await FakeLiveMac.make(clock: clock)
        let (request, _) = try await admittedStart(mac)
        clock.set(issued.addingTimeInterval(39))
        let result = await cleanup(mac, request, streamID: nil, clock: clock).run()
        XCTAssertEqual(result, .done(.startIDCancel))
        let running = await mac.isStreamRunning
        let releases = await mac.releaseCalls
        XCTAssertFalse(running)
        XCTAssertTrue(releases.isEmpty)
    }

    func testReleaseAt41SecondsWithoutAStreamID() async throws {
        let clock = ManualLiveClock(issued)
        let mac = try await FakeLiveMac.make(clock: clock)
        let (request, _) = try await admittedStart(mac)
        clock.set(issued.addingTimeInterval(41))
        let result = await cleanup(mac, request, streamID: nil, clock: clock).run()
        XCTAssertEqual(result, .done(.release))
        let running = await mac.isStreamRunning
        let cancels = await mac.cancelCalls
        XCTAssertFalse(running)
        XCTAssertTrue(cancels.isEmpty, "A start-ID cancel after 40 s would be refused, so it is not sent")
    }

    func testStreamCancelAfterTenMinutesOfPlayback() async throws {
        let clock = ManualLiveClock(issued)
        let mac = try await FakeLiveMac.make(clock: clock)
        let (request, streamID) = try await admittedStart(mac)
        clock.set(issued.addingTimeInterval(600))
        let result = await cleanup(mac, request, streamID: streamID, clock: clock).run()
        XCTAssertEqual(result, .done(.streamCancel))
        let stopped = await mac.stoppedStreams[streamID]
        XCTAssertEqual(stopped, "viewerStopped")
        let sessionIDs = await mac.relaySessionIDs
        XCTAssertEqual(sessionIDs, [streamID.uuidString.lowercased()])
        let cancels = await mac.cancelCalls
        let releases = await mac.releaseCalls
        XCTAssertTrue(cancels.isEmpty && releases.isEmpty)
    }

    func testStreamCancelForAnEndedStreamIsDone() async throws {
        let clock = ManualLiveClock(issued)
        let mac = try await FakeLiveMac.make(clock: clock)
        let (request, streamID) = try await admittedStart(mac)
        await mac.endStream(reason: "idle")
        let result = await cleanup(mac, request, streamID: streamID, clock: clock).run()
        XCTAssertEqual(result, .done(.streamCancel), "A signed 410 stream_ended means the stream is gone")
    }

    func testReleaseKeepsRetryingUntil75SecondsAndCatchesAStartAdmittedAt65() async throws {
        let clock = ManualLiveClock(issued.addingTimeInterval(41))
        let mac = try await FakeLiveMac.make(clock: clock)
        let request = LiveStartRequest(
            startID: UUID(), issuedAt: issued.unixMilliseconds,
            titleID: makeLiveTitle().id, revision: makeLiveTitle().revision, audioPID: 4352
        )
        clock.schedule(at: issued.addingTimeInterval(65)) { await mac.admitLate(request) }
        let result = await cleanup(mac, request, streamID: nil, clock: clock).run()
        XCTAssertEqual(result, .done(.release))
        let releases = await mac.releaseCalls
        let last = try XCTUnwrap(releases.last)
        XCTAssertGreaterThanOrEqual(last.at, issued.addingTimeInterval(65))
        XCTAssertLessThanOrEqual(last.at, issued.addingTimeInterval(LiveStartTiming.releaseUntil))
        let running = await mac.isStreamRunning
        XCTAssertFalse(running)
    }

    func testReleaseStopsAt75SecondsWhenNothingWasAdmitted() async throws {
        let clock = ManualLiveClock(issued.addingTimeInterval(41))
        let mac = try await FakeLiveMac.make(clock: clock)
        let request = LiveStartRequest(
            startID: UUID(), issuedAt: issued.unixMilliseconds,
            titleID: makeLiveTitle().id, revision: makeLiveTitle().revision, audioPID: 4352
        )
        let result = await cleanup(mac, request, streamID: nil, clock: clock).run()
        XCTAssertEqual(result, .unconfirmed)
        let releases = await mac.releaseCalls
        XCTAssertTrue(releases.allSatisfy { $0.at <= self.issued.addingTimeInterval(LiveStartTiming.releaseUntil) })
        XCTAssertGreaterThan(try XCTUnwrap(releases.last).at, issued.addingTimeInterval(71), "The last attempt is after admission ends at 70 s")
    }

    func testExpiredStartIDCancelSwitchesToRelease() async throws {
        let clock = ManualLiveClock(issued)
        let mac = try await FakeLiveMac.make(clock: clock)
        let (request, _) = try await admittedStart(mac)
        await mac.set(cancelAnswers: [.expired])
        clock.set(issued.addingTimeInterval(30))
        let result = await cleanup(mac, request, streamID: nil, clock: clock).run()
        XCTAssertEqual(result, .done(.release))
        let cancels = await mac.cancelCalls
        XCTAssertEqual(cancels.count, 1, "expired is final for the start-ID route")
    }

    func testRetryLaterNearTheDeadlineMovesToRelease() async throws {
        let clock = ManualLiveClock(issued)
        let mac = try await FakeLiveMac.make(clock: clock)
        let (request, _) = try await admittedStart(mac)
        await mac.set(cancelAnswers: [.retryLater])
        clock.set(issued.addingTimeInterval(39.8))
        let result = await cleanup(mac, request, streamID: nil, clock: clock).run()
        XCTAssertEqual(result, .done(.release))
        let cancels = await mac.cancelCalls
        XCTAssertEqual(cancels.count, 1, "No start-ID cancel is sent after issuedAt + 40 s")
    }

    func testBackoffSchedule() {
        XCTAssertEqual((0..<6).map(LiveStartTiming.backoff(attempt:)), [0.5, 1, 2, 4, 4, 4])
    }

    /// Every relay request for a live stream is signed with the derived stream session: the
    /// snapshot, a position report, media, and the stream cancel. None carries the parent ID.
    func testEveryRelayRequestIsSignedWithTheStreamSession() async throws {
        let clock = ManualLiveClock(issued)
        let mac = try await FakeLiveMac.make(clock: clock)
        let (request, streamID) = try await admittedStart(mac)
        let stream = try await mac.liveStreamSession(streamID: streamID)
        let transport = await mac.makeRelayTransport()
        var source = try RelayRemotePlaybackSource(session: stream.session, serverBaseURL: stream.baseURL)
        try await source.refreshRetainedWindow(transport: transport, clock: { clock.now() })
        try await source.reportPosition(milliseconds: 1_500, transport: transport, clock: { clock.now() })
        let media = RelayAuthenticatedResourceClient(
            signer: stream.session, transport: transport, serverBaseURL: stream.baseURL, clock: { clock.now() }
        )
        let segment = try XCTUnwrap(source.retainedSeekPolicy.window?.segments.first)
        _ = try await media.load(source.resolveSegmentURL(for: segment))
        let result = await cleanup(mac, request, streamID: streamID, clock: clock).run()
        XCTAssertEqual(result, .done(.streamCancel))

        let sessionIDs = await mac.relaySessionIDs
        XCTAssertEqual(sessionIDs.count, 4)
        XCTAssertTrue(sessionIDs.allSatisfy { $0 == streamID.uuidString.lowercased() })
        XCTAssertFalse(sessionIDs.contains(mac.parent.client.sessionID.rawValue))
    }

    func testParentSignedRelayRequestIsRejected() async throws {
        let clock = ManualLiveClock(issued)
        let mac = try await FakeLiveMac.make(clock: clock)
        let (_, streamID) = try await admittedStart(mac)
        _ = streamID
        let transport = await mac.makeRelayTransport()
        var source = try RelayRemotePlaybackSource(session: mac.parent.client, serverBaseURL: mac.baseURL)
        do {
            try await source.refreshRetainedWindow(transport: transport, clock: { clock.now() })
            XCTFail("A parent-signed relay request must not verify as a stream request")
        } catch {
            XCTAssertEqual(error as? RelayTransportError, .unexpectedStatusCode(404), "\(error)")
        }
    }
}

import AVFoundation
import CoreMedia
import CoreVideo
import XCTest
@testable import BDToAVPPlayer

final class PlaybackPresentationTests: XCTestCase {
    func testTimeFormatterUsesMinutesAndSeconds() {
        XCTAssertEqual(PlaybackTimeFormatter.string(for: 65.9), "1:05")
    }

    func testTimeFormatterIncludesHoursWhenNeeded() {
        XCTAssertEqual(PlaybackTimeFormatter.string(for: 3_661), "1:01:01")
    }

    func testTimeFormatterHandlesInvalidValues() {
        XCTAssertEqual(PlaybackTimeFormatter.string(for: .nan), "0:00")
        XCTAssertEqual(PlaybackTimeFormatter.string(for: -1), "0:00")
    }

    func testSeekClampsToThePlayableRange() {
        XCTAssertEqual(PlaybackSeekPolicy.clampedTime(-2, duration: 120), 0)
        XCTAssertEqual(PlaybackSeekPolicy.clampedTime(200, duration: 120), 120)
        XCTAssertEqual(PlaybackSeekPolicy.clampedTime(40, duration: 120), 40)
    }

    func testSeekKeepsFinitePositionWhenDurationIsUnavailable() {
        XCTAssertEqual(PlaybackSeekPolicy.clampedTime(40, duration: .nan), 40)
    }

    func testResumeWriteAllowsFinishingDuringEyeOrderChange() {
        XCTAssertFalse(
            ResumeWritePolicy.allowsWrite(
                isChangingEyeOrder: true,
                isFinishing: false,
                hasEstablishedPlayback: true
            )
        )
        XCTAssertTrue(
            ResumeWritePolicy.allowsWrite(
                isChangingEyeOrder: true,
                isFinishing: true,
                hasEstablishedPlayback: true
            )
        )
        XCTAssertTrue(
            ResumeWritePolicy.allowsWrite(
                isChangingEyeOrder: false,
                isFinishing: false,
                hasEstablishedPlayback: true
            )
        )
        XCTAssertFalse(
            ResumeWritePolicy.allowsWrite(
                isChangingEyeOrder: false,
                isFinishing: true,
                hasEstablishedPlayback: false
            )
        )
    }

    func testSourceOpeningPresentationIsHonestAboutFilesStaging() {
        XCTAssertEqual(PlaybackPreparationPhase.openingSource.title, "Opening Source")
        XCTAssertEqual(
            PlaybackPreparationPhase.openingSource.message(for: "Movie"),
            "Opening Movie from Files. This may take longer while its source becomes available."
        )
    }

    func testFailurePresentationsExposeOnlySupportedRecoveryActions() {
        XCTAssertFalse(PlaybackFailurePresentation.unsupported.canRetry)
        XCTAssertFalse(PlaybackFailurePresentation.unsupported.canLocate)
        XCTAssertFalse(PlaybackFailurePresentation.sourceNeedsLocation.canRetry)
        XCTAssertTrue(PlaybackFailurePresentation.sourceNeedsLocation.canLocate)
        XCTAssertTrue(PlaybackFailurePresentation.sourceUnavailable.canRetry)
        XCTAssertTrue(PlaybackFailurePresentation.sourceUnavailable.canLocate)
        XCTAssertFalse(PlaybackFailurePresentation.sourceUnavailable.message.localizedCaseInsensitiveContains("reconnect"))
        XCTAssertFalse(PlaybackFailurePresentation.sourceUnavailable.message.contains("%"))
    }

    func testResumeWriteUsesCapturedPositionWhenFinishingEyeOrderChange() {
        XCTAssertEqual(
            ResumeWritePolicy.position(
                currentTime: 0,
                eyeOrderChangeTime: 2_400,
                isChangingEyeOrder: true,
                isFinishing: true
            ),
            2_400
        )
        XCTAssertEqual(
            ResumeWritePolicy.position(
                currentTime: 15,
                eyeOrderChangeTime: 2_400,
                isChangingEyeOrder: true,
                isFinishing: false
            ),
            15
        )
    }

    func testPendingResumeIsClampedAndConsumedOnlyOnce() {
        var pendingResume = PlaybackPendingResumeState()

        pendingResume.store(140, duration: 120)

        XCTAssertEqual(pendingResume.value, 120)
        XCTAssertEqual(pendingResume.consume(), 120)
        XCTAssertNil(pendingResume.consume())
    }

    func testPendingResumeClearDiscardsStoredPosition() {
        var pendingResume = PlaybackPendingResumeState()
        pendingResume.store(42, duration: 120)

        pendingResume.clear()

        XCTAssertNil(pendingResume.value)
        XCTAssertNil(pendingResume.consume())
    }

    func testPlaybackIntentInactiveSceneCancelsAutoplayWithoutRearmingOnReturn() {
        var intent = PlaybackIntentState()
        intent.requestPlayback()

        intent.sceneBecameInactive()

        XCTAssertFalse(intent.isSceneActive)
        XCTAssertFalse(intent.shouldPlay)

        intent.sceneBecameActive()

        XCTAssertTrue(intent.isSceneActive)
        XCTAssertFalse(intent.shouldPlay)
    }

    func testPlaybackIntentRequiresANewRequestAfterInactivePreparation() {
        var intent = PlaybackIntentState()
        intent.sceneBecameInactive()

        intent.requestPlayback()
        intent.sceneBecameActive()

        XCTAssertFalse(intent.shouldPlay)

        intent.requestPlayback()

        XCTAssertTrue(intent.shouldPlay)
    }

    func testPlaybackIntentPreservesEyeOrderResumeOnlyForActivePlayback() {
        var intent = PlaybackIntentState()

        intent.preservePlaybackIntent(wasPlaying: true)
        XCTAssertTrue(intent.shouldPlay)

        intent.pause()
        intent.preservePlaybackIntent(wasPlaying: false)
        XCTAssertFalse(intent.shouldPlay)

        intent.sceneBecameInactive()
        intent.preservePlaybackIntent(wasPlaying: true)
        XCTAssertFalse(intent.shouldPlay)
    }

    func testScrubStateKeepsLocalThumbValueUntilEditingEnds() {
        var scrubState = PlaybackScrubState()
        scrubState.begin(currentTime: 12)
        scrubState.update(requestedTime: 88, duration: 120)

        XCTAssertEqual(scrubState.value, 88)
        XCTAssertEqual(scrubState.finish(), 88)
        XCTAssertNil(scrubState.value)
    }

    func testScrubStateFinishesWithNoSeekWhenCancelled() {
        var scrubState = PlaybackScrubState()
        scrubState.begin(currentTime: 12)
        scrubState.cancel()

        XCTAssertNil(scrubState.finish())
    }

    func testHUDVisibilitySchedulesAutomaticHidingOnlyDuringUnfocusedPlayback() {
        var visibility = PlaybackHUDVisibilityState()

        visibility.reconcile(isPlaying: true)

        XCTAssertTrue(visibility.isVisible)
        XCTAssertTrue(visibility.isAutoHideScheduled)
        XCTAssertEqual(PlaybackHUDVisibilityState.autoHideDelay, 3)
    }

    func testHUDAutomaticHidingStaysDisabledForAssistiveTechnology() {
        XCTAssertTrue(
            PlaybackHUDVisibilityPolicy.allowsAutomaticHiding(
                isPlaying: true,
                isVoiceOverEnabled: false,
                isSwitchControlEnabled: false
            )
        )
        XCTAssertFalse(
            PlaybackHUDVisibilityPolicy.allowsAutomaticHiding(
                isPlaying: true,
                isVoiceOverEnabled: true,
                isSwitchControlEnabled: false
            )
        )
        XCTAssertFalse(
            PlaybackHUDVisibilityPolicy.allowsAutomaticHiding(
                isPlaying: true,
                isVoiceOverEnabled: false,
                isSwitchControlEnabled: true
            )
        )
    }

    func testHUDVisibilityHidesOnlyForTheCurrentScheduledTimer() {
        var visibility = PlaybackHUDVisibilityState()
        visibility.reconcile(isPlaying: true)
        let initialGeneration = visibility.autoHideGeneration

        visibility.reveal(isPlaying: true)
        let replacementGeneration = visibility.autoHideGeneration
        visibility.autoHideTimerFired(generation: initialGeneration)

        XCTAssertTrue(visibility.isVisible)
        XCTAssertTrue(visibility.isAutoHideScheduled)

        visibility.autoHideTimerFired(generation: replacementGeneration)

        XCTAssertFalse(visibility.isVisible)
        XCTAssertFalse(visibility.isAutoHideScheduled)
    }

    func testHUDVisibilityRemainsVisibleWhilePausedOrScrubbing() {
        var visibility = PlaybackHUDVisibilityState()

        visibility.reconcile(isPlaying: false)
        XCTAssertTrue(visibility.isVisible)
        XCTAssertFalse(visibility.isAutoHideScheduled)

        visibility.setInteracting(true, isPlaying: true)
        XCTAssertTrue(visibility.isVisible)
        XCTAssertFalse(visibility.isAutoHideScheduled)

        visibility.setInteracting(false, isPlaying: true)
        XCTAssertTrue(visibility.isAutoHideScheduled)
    }

    func testHUDHoverRestartsAutoHideWithoutPinningControlsVisible() {
        var visibility = PlaybackHUDVisibilityState()
        visibility.reconcile(isPlaying: true)

        visibility.hoverBegan(isPlaying: true)
        let generation = visibility.autoHideGeneration
        visibility.autoHideTimerFired(generation: generation)

        XCTAssertFalse(visibility.isVisible)
        XCTAssertFalse(visibility.isAutoHideScheduled)
    }

    func testEyeOrderPresentationHasVisibleAndAccessibleSelectedState() {
        XCTAssertEqual(
            PlaybackEyeOrderPresentation.value(isEyeSwapped: false),
            PlaybackEyeOrderPresentation(
                title: "Normal",
                systemImage: "arrow.left.arrow.right",
                isSelected: false
            )
        )
        XCTAssertEqual(
            PlaybackEyeOrderPresentation.value(isEyeSwapped: true),
            PlaybackEyeOrderPresentation(
                title: "Reversed",
                systemImage: "arrow.left.arrow.right.circle.fill",
                isSelected: true
            )
        )
    }

    func testAudioLabelsLeaveUniqueNamesUnchangedAndDisambiguateCollisions() {
        let labels = PlaybackAudioOptionLabelPolicy.labels(for: [
            PlaybackAudioOptionLabelMetadata(
                baseName: "English",
                role: nil,
                index: 0
            ),
            PlaybackAudioOptionLabelMetadata(
                baseName: "English",
                role: "Commentary",
                index: 1
            ),
            PlaybackAudioOptionLabelMetadata(
                baseName: "French",
                role: nil,
                index: 2
            ),
        ])

        XCTAssertEqual(labels, [
            "English — Track 1",
            "English — Commentary — Track 2",
            "French",
        ])
    }

    func testAudioLabelsUseStableTrackIndexesWhenMetadataStillCollides() {
        let labels = PlaybackAudioOptionLabelPolicy.labels(for: [
            PlaybackAudioOptionLabelMetadata(
                baseName: "English",
                role: nil,
                index: 0
            ),
            PlaybackAudioOptionLabelMetadata(
                baseName: "English",
                role: nil,
                index: 1
            ),
        ])

        XCTAssertEqual(labels, ["English — Track 1", "English — Track 2"])
    }

    func testPackedAudioSelectionPrefersCurrentThenPlayableDefaultThenFirstPlayable() {
        XCTAssertEqual(
            PlaybackAudioSelectionPolicy.preferredIndex(
                currentIndex: 2,
                defaultIndex: 1,
                playableIndices: [0, 1, 2]
            ),
            2
        )
        XCTAssertEqual(
            PlaybackAudioSelectionPolicy.preferredIndex(
                currentIndex: 2,
                defaultIndex: 1,
                playableIndices: [0, 1]
            ),
            1
        )
        XCTAssertEqual(
            PlaybackAudioSelectionPolicy.preferredIndex(
                currentIndex: nil,
                defaultIndex: 1,
                playableIndices: [0, 1]
            ),
            1
        )
        XCTAssertEqual(
            PlaybackAudioSelectionPolicy.preferredIndex(
                currentIndex: nil,
                defaultIndex: 2,
                playableIndices: [1, 3]
            ),
            1
        )
        XCTAssertNil(
            PlaybackAudioSelectionPolicy.preferredIndex(
                currentIndex: nil,
                defaultIndex: nil,
                playableIndices: []
            )
        )
    }

    func testPackedStereoStatusUsesQualificationCopyOnlyForBuiltInChecks() {
        let builtInItem = MediaItem(
            id: BuiltInStereoChecks.sideBySideID,
            title: "Side-by-Side Stereo Check",
            fileName: "Stereo-Check-SBS.mov",
            format: .sideBySide
        )
        let importedItem = MediaItem(
            id: "movie-1",
            title: "Feature Film",
            fileName: "Feature-Film-SBS.mov",
            format: .sideBySide
        )

        XCTAssertEqual(
            PackedStereoStatusPresentation.message(
                mediaItem: builtInItem,
                isReady: true,
                failureMessage: nil
            ),
            "Cover one eye at a time"
        )
        XCTAssertEqual(
            PackedStereoStatusPresentation.message(
                mediaItem: importedItem,
                isReady: false,
                failureMessage: nil
            ),
            "Preparing Feature Film…"
        )
        XCTAssertEqual(
            PackedStereoStatusPresentation.message(
                mediaItem: importedItem,
                isReady: true,
                failureMessage: nil
            ),
            "Packed stereo playback"
        )
    }

    func testResumePolicyWritesAnInProgressPosition() {
        XCTAssertEqual(ResumeWritePolicy.decision(currentTime: 42, duration: 120), .write(42))
    }

    func testResumePolicyRemovesCompletedPlayback() {
        XCTAssertEqual(ResumeWritePolicy.decision(currentTime: 117, duration: 120), .remove)
    }

    func testResumePolicySkipsInvalidPosition() {
        XCTAssertEqual(ResumeWritePolicy.decision(currentTime: .infinity, duration: 120), .skip)
    }

    func testPackedStereoOutputDescribesSeparateEyeBuffers() {
        XCTAssertEqual(
            PackedStereoComposition.outputBufferDescription,
            [
                [
                    CMTag.mediaType(.video),
                    CMTag.stereoView(.leftEye),
                    CMTag.projectionType(.rectangular),
                ],
                [
                    CMTag.mediaType(.video),
                    CMTag.stereoView(.rightEye),
                    CMTag.projectionType(.rectangular),
                ],
            ]
        )
    }

    func testPackedStereoEyeOrderIsCarriedByTheInstructionNotTheOutputTags() throws {
        let geometry = try XCTUnwrap(
            PackedStereoGeometry(sourceWidth: 3840, sourceHeight: 1080, format: .sideBySide)
        )
        let instruction = PackedStereoCompositionInstruction(
            timeRange: CMTimeRange(start: .zero, duration: CMTime(seconds: 12, preferredTimescale: 600)),
            sourceTrackID: 7,
            geometry: geometry,
            eyeOrder: .reversed,
            spatialConfiguration: AVSpatialVideoConfiguration()
        )

        XCTAssertEqual(instruction.eyeOrder, .reversed)
        XCTAssertEqual(instruction.sourceTrackID, 7)
        XCTAssertEqual(PackedStereoComposition.outputBufferDescription[0].contains(.stereoView(.leftEye)), true)
        XCTAssertEqual(PackedStereoComposition.outputBufferDescription[1].contains(.stereoView(.rightEye)), true)
    }

    func testPackedStereoCropGeometrySplitsSideBySideFrames() throws {
        let geometry = try XCTUnwrap(
            PackedStereoGeometry(sourceWidth: 3840, sourceHeight: 1080, format: .sideBySide)
        )

        XCTAssertEqual(
            geometry.sourceRegion(for: .left, eyeOrder: .normal),
            PackedStereoRegion(x: 0, y: 0, width: 1920, height: 1080)
        )
        XCTAssertEqual(
            geometry.sourceRegion(for: .right, eyeOrder: .normal),
            PackedStereoRegion(x: 1920, y: 0, width: 1920, height: 1080)
        )
        XCTAssertEqual(
            geometry.sourceRegion(for: .left, eyeOrder: .reversed),
            PackedStereoRegion(x: 1920, y: 0, width: 1920, height: 1080)
        )
    }

    func testPackedStereoCropGeometryMapsOverUnderTopToLeftEye() throws {
        let geometry = try XCTUnwrap(
            PackedStereoGeometry(sourceWidth: 1920, sourceHeight: 2160, format: .overUnder)
        )

        XCTAssertEqual(
            geometry.sourceRegion(for: .left, eyeOrder: .normal),
            PackedStereoRegion(x: 0, y: 0, width: 1920, height: 1080)
        )
        XCTAssertEqual(
            geometry.sourceRegion(for: .right, eyeOrder: .normal),
            PackedStereoRegion(x: 0, y: 1080, width: 1920, height: 1080)
        )
    }

    func testPackedStereoGeometryRejectsChromaMisalignedFrames() {
        XCTAssertNil(PackedStereoGeometry(sourceWidth: 3838, sourceHeight: 1080, format: .sideBySide))
        XCTAssertNil(PackedStereoGeometry(sourceWidth: 1920, sourceHeight: 2158, format: .overUnder))
    }

    func testPackedStereoCompositorRequestsNativeBiplanarBuffers() {
        let compositor = PackedStereoVideoCompositor()
        let sourceFormats = compositor.sourcePixelBufferAttributes?[kCVPixelBufferPixelFormatTypeKey as String]
            as? [OSType]
        let outputFormats = compositor.requiredPixelBufferAttributesForRenderContext[
            kCVPixelBufferPixelFormatTypeKey as String
        ] as? [OSType]

        XCTAssertEqual(sourceFormats, [kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange])
        XCTAssertEqual(outputFormats, [kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange])
    }

    func testPackedStereoRendererMapsSBSAndOverUnderPixelsForBothEyeOrders() throws {
        for format in [StereoFormat.sideBySide, .overUnder] {
            let geometry = try XCTUnwrap(
                PackedStereoGeometry(sourceWidth: 8, sourceHeight: 8, format: format)
            )
            var source = try makePixelBuffer(width: geometry.sourceWidth, height: geometry.sourceHeight)
            try fill(
                &source,
                region: geometry.sourceRegion(for: .left, eyeOrder: .normal),
                luma: 36,
                chromaBlue: 90,
                chromaRed: 120
            )
            try fill(
                &source,
                region: geometry.sourceRegion(for: .right, eyeOrder: .normal),
                luma: 210,
                chromaBlue: 180,
                chromaRed: 220
            )
            source.withUnsafeBuffer { buffer in
                CVBufferSetAttachment(
                    buffer,
                    kCVImageBufferColorPrimariesKey,
                    kCVImageBufferColorPrimaries_ITU_R_709_2,
                    .shouldPropagate
                )
            }
            let sourceReadOnly = CVReadOnlyPixelBuffer(source)

            var normalLeft = try makePixelBuffer(width: geometry.eyeWidth, height: geometry.eyeHeight)
            var normalRight = try makePixelBuffer(width: geometry.eyeWidth, height: geometry.eyeHeight)
            try PackedStereoFrameRenderer.render(
                source: sourceReadOnly,
                geometry: geometry,
                eyeOrder: .normal,
                leftOutput: &normalLeft,
                rightOutput: &normalRight
            )

            XCTAssertEqual(try planeBytes(normalLeft, plane: 0), Array(repeating: 36, count: geometry.eyeWidth * geometry.eyeHeight))
            XCTAssertEqual(try planeBytes(normalRight, plane: 0), Array(repeating: 210, count: geometry.eyeWidth * geometry.eyeHeight))
            XCTAssertEqual(try planeBytes(normalLeft, plane: 1), repeatedChroma(blue: 90, red: 120, geometry: geometry))
            XCTAssertEqual(try planeBytes(normalRight, plane: 1), repeatedChroma(blue: 180, red: 220, geometry: geometry))
            XCTAssertEqual(pixelFormat(normalLeft), kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange)
            XCTAssertEqual(pixelFormat(normalRight), kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange)
            XCTAssertTrue(hasColorPrimariesAttachment(normalLeft))
            XCTAssertTrue(hasColorPrimariesAttachment(normalRight))

            var reversedLeft = try makePixelBuffer(width: geometry.eyeWidth, height: geometry.eyeHeight)
            var reversedRight = try makePixelBuffer(width: geometry.eyeWidth, height: geometry.eyeHeight)
            try PackedStereoFrameRenderer.render(
                source: sourceReadOnly,
                geometry: geometry,
                eyeOrder: .reversed,
                leftOutput: &reversedLeft,
                rightOutput: &reversedRight
            )

            XCTAssertEqual(try planeBytes(reversedLeft, plane: 0), try planeBytes(normalRight, plane: 0))
            XCTAssertEqual(try planeBytes(reversedRight, plane: 0), try planeBytes(normalLeft, plane: 0))
            XCTAssertEqual(try planeBytes(reversedLeft, plane: 1), try planeBytes(normalRight, plane: 1))
            XCTAssertEqual(try planeBytes(reversedRight, plane: 1), try planeBytes(normalLeft, plane: 1))
        }
    }

    @MainActor
    func testEyeSwapIsANoOpUntilPackedStereoPlaybackIsReady() {
        let session = MVHEVCPlayerSession()

        session.toggleEyeSwap()

        XCTAssertFalse(session.supportsEyeSwap)
        XCTAssertFalse(session.isEyeSwapped)
        XCTAssertFalse(session.isChangingEyeOrder)
    }

    @MainActor
    func testPreparePackedStereoAttemptsBookmarkAccess() async {
        let session = MVHEVCPlayerSession()
        let mediaItem = MediaItem(id: "sbs", title: "SBS", fileName: "sbs.mov", format: .sideBySide)
        let temporaryDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests-\(UUID().uuidString)", isDirectory: true)
        let bookmarkStore = BookmarkStore(storageURL: temporaryDirectory.appendingPathComponent("bookmarks.json"))
        let resumeStore = ResumeStore(storageURL: temporaryDirectory.appendingPathComponent("resume.json"))

        await session.prepare(mediaItem: mediaItem, bookmarkStore: bookmarkStore, resumeStore: resumeStore)

        XCTAssertEqual(session.state, .failed)
        XCTAssertEqual(session.failurePresentation, .sourceNeedsLocation)
        XCTAssertEqual(session.failureMessage, PlaybackFailurePresentation.sourceNeedsLocation.message)
    }

    @MainActor
    func testFailedPreparationDoesNotOverwriteExistingResumePosition() async throws {
        let session = MVHEVCPlayerSession()
        let mediaItem = MediaItem(id: "offline", title: "Offline", fileName: "offline.mov", format: .mvHEVC)
        let temporaryDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests-\(UUID().uuidString)", isDirectory: true)
        let bookmarkStore = BookmarkStore(storageURL: temporaryDirectory.appendingPathComponent("bookmarks.json"))
        let resumeStore = ResumeStore(storageURL: temporaryDirectory.appendingPathComponent("resume.json"))
        try resumeStore.setResumeTime(2_700, for: mediaItem.id)

        await session.prepare(mediaItem: mediaItem, bookmarkStore: bookmarkStore, resumeStore: resumeStore)
        session.finish()

        XCTAssertEqual(resumeStore.resumeTime(for: mediaItem.id), 2_700)
    }

    @MainActor
    func testFinishResetsFailedPlaybackSession() async {
        let session = MVHEVCPlayerSession()
        let mediaItem = MediaItem(id: "sbs", title: "SBS", fileName: "sbs.mov", format: .sideBySide)
        let temporaryDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests-\(UUID().uuidString)", isDirectory: true)
        let bookmarkStore = BookmarkStore(storageURL: temporaryDirectory.appendingPathComponent("bookmarks.json"))
        let resumeStore = ResumeStore(storageURL: temporaryDirectory.appendingPathComponent("resume.json"))

        await session.prepare(mediaItem: mediaItem, bookmarkStore: bookmarkStore, resumeStore: resumeStore)
        session.finish()

        XCTAssertEqual(session.state, .idle)
        XCTAssertNil(session.mediaItem)
        XCTAssertNil(session.failureMessage)
        XCTAssertEqual(session.currentTime, 0)
        XCTAssertEqual(session.duration, 0)
    }

    @MainActor
    func testExpiredRelayRefreshClosesLoopbackAndPreservesFailureForRetry() async throws {
        let fixture = try await makeRelayPlaybackFixture()
        let session = MVHEVCPlayerSession()
        session.installRelayPlaybackResources(
            source: fixture.source,
            transport: fixture.transport,
            item: try makePlayableRelayTestItem(),
            mediaItem: relayItem(for: fixture.source),
            observeItemStatus: false,
            startRefreshLoop: false
        )

        let reachableSession = URLSession(configuration: .ephemeral)
        defer { reachableSession.invalidateAndCancel() }
        let (_, reachableResponse) = try await reachableSession.data(from: fixture.loopbackURL)
        XCTAssertEqual((reachableResponse as? HTTPURLResponse)?.statusCode, 200)

        await fixture.mode.set(.expired)
        await session.refreshRelayWindowNow()

        XCTAssertEqual(session.state, .failed)
        XCTAssertNil(session.player.currentItem)
        XCTAssertNil(session.playerItem)
        XCTAssertTrue(session.isRelayPlayback)
        XCTAssertEqual(
            session.failureMessage,
            "The relay session ended. Pair with the Mac again to continue."
        )
        XCTAssertEqual(session.relayTerminationEvent?.reason, .sessionEnded)

        let closedConfiguration = URLSessionConfiguration.ephemeral
        closedConfiguration.timeoutIntervalForRequest = 0.25
        let closedSession = URLSession(configuration: closedConfiguration)
        defer { closedSession.invalidateAndCancel() }
        do {
            _ = try await closedSession.data(from: fixture.loopbackURL)
            XCTFail("Terminal relay cleanup must close its localhost listener")
        } catch { }
    }

    @MainActor
    func testRelayRefreshFailureBudgetResetsAfterSuccess() async throws {
        let fixture = try await makeRelayPlaybackFixture(startLoopback: false)
        let session = MVHEVCPlayerSession()
        let item = try makePlayableRelayTestItem()
        session.installRelayPlaybackResources(
            source: fixture.source,
            transport: fixture.transport,
            item: item,
            mediaItem: relayItem(for: fixture.source),
            observeItemStatus: false,
            startRefreshLoop: false
        )

        await fixture.mode.set(.unreachable)
        await session.refreshRelayWindowNow()
        await session.refreshRelayWindowNow()
        XCTAssertNotEqual(session.state, .failed)
        XCTAssertTrue(session.player.currentItem === item)

        await fixture.mode.set(.success)
        await session.refreshRelayWindowNow()
        await fixture.mode.set(.unreachable)
        await session.refreshRelayWindowNow()
        await session.refreshRelayWindowNow()
        XCTAssertNotEqual(session.state, .failed)

        await session.refreshRelayWindowNow()
        XCTAssertEqual(session.state, .failed)
        XCTAssertNil(session.player.currentItem)
        XCTAssertTrue(session.isRelayPlayback)
        XCTAssertEqual(session.relayTerminationEvent?.reason, .serverUnreachable)
    }

    @MainActor
    func testStaleRelayRefreshCompletionCannotFailFinishedSession() async throws {
        let sessions = try await makePairedSessions(now: Date())
        let transport = SuspendingRelayRefreshTransport()
        let source = try RelayRemotePlaybackSource(
            session: sessions.client,
            serverBaseURL: URL(string: "http://relay.local:7431")!
        )
        let session = MVHEVCPlayerSession()
        session.installRelayPlaybackResources(
            source: source,
            transport: transport,
            item: try makePlayableRelayTestItem(),
            mediaItem: relayItem(for: source),
            observeItemStatus: false,
            startRefreshLoop: false
        )

        let refresh = Task { await session.refreshRelayWindowNow() }
        await transport.waitUntilRequested()
        session.finish()
        await transport.fail(with: .sessionExpired)
        await refresh.value

        XCTAssertEqual(session.state, .idle)
        XCTAssertNil(session.failureMessage)
        XCTAssertNil(session.relayTerminationEvent)
        XCTAssertFalse(session.isRelayPlayback)
    }

    @MainActor
    func testRelayPreparationRejectsInvalidAuthenticatedResponseAndRetainsRetryIdentity() async throws {
        let sessions = try await makePairedSessions(now: Date())
        let transport = FakeRelayTransport()
        await transport.setHandler { request in
            let body = Data("{}".utf8)
            return (
                body,
                try makeAuthenticatedHTTPResponse(
                    request,
                    body: body,
                    serverSession: sessions.server,
                    authenticatedBody: Data("different".utf8)
                )
            )
        }
        let session = MVHEVCPlayerSession()

        await session.prepareRelayPlayback(
            RelayRemotePlaybackConfiguration(
                session: sessions.client,
                serverBaseURL: URL(string: "http://relay.local:7431")!,
                serverName: "Test Relay",
                transport: transport
            )
        )

        XCTAssertEqual(session.state, .failed)
        XCTAssertNil(session.player.currentItem)
        XCTAssertNil(session.playerItem)
        XCTAssertTrue(session.isRelayPlayback)
        XCTAssertEqual(session.relayTerminationEvent?.reason, .rejected)
        XCTAssertEqual(
            session.relayTerminationEvent?.sessionID,
            sessions.client.sessionID.rawValue
        )
    }

    @MainActor
    private func makeRelayPlaybackFixture(
        startLoopback: Bool = true
    ) async throws -> (
        source: RelayRemotePlaybackSource,
        loopbackURL: URL,
        transport: FakeRelayTransport,
        mode: RelayRefreshMode
    ) {
        let now = Date()
        let sessions = try await makePairedSessions(now: now)
        let mode = RelayRefreshMode()
        let transport = FakeRelayTransport()
        await transport.setHandler { request in
            switch await mode.value {
            case .expired:
                throw RelayTransportError.sessionExpired
            case .unreachable:
                throw URLError(.cannotConnectToHost)
            case .success:
                let body: Data
                if request.url?.path == RelayWireContract.playlistPath {
                    body = Data("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-ENDLIST\n".utf8)
                } else {
                    body = try JSONEncoder().encode(
                        try RelayPlaylistSnapshot(
                            earliestPlayableTimeMilliseconds: 0,
                            totalDurationMilliseconds: 2_000,
                            isFinalized: false,
                            segments: [
                                try RelayPlaylistSegment(
                                    sequenceNumber: 0,
                                    startTimeMilliseconds: 0,
                                    durationMilliseconds: 2_000,
                                    resourceIdentifier: "segment-000.m4s"
                                ),
                            ]
                        )
                    )
                }
                return (
                    body,
                    try makeAuthenticatedHTTPResponse(request, body: body, serverSession: sessions.server)
                )
            }
        }
        var source = try RelayRemotePlaybackSource(
            session: sessions.client,
            serverBaseURL: URL(string: "http://relay.local:7431")!
        )
        let loopbackURL: URL
        if startLoopback {
            let (asset, _) = try await source.makeAssetAndLoader(transport: transport)
            loopbackURL = asset.url
        } else {
            loopbackURL = URL(string: "http://127.0.0.1")!
        }
        return (source, loopbackURL, transport, mode)
    }

    private func makePlayableRelayTestItem() throws -> AVPlayerItem {
        let destination = FileManager.default.temporaryDirectory
            .appendingPathComponent("RelayPlayerSessionTests")
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let check = try XCTUnwrap(BuiltInStereoChecks.install(destinationDirectory: destination).first)
        return AVPlayerItem(url: check.url)
    }

    private func relayItem(for source: RelayRemotePlaybackSource) -> MediaItem {
        MediaItem(
            id: "relay:\(source.session.sessionID.rawValue)",
            title: "Test Relay",
            fileName: "live.m3u8",
            format: .mvHEVC
        )
    }

    private func makePixelBuffer(width: Int, height: Int) throws -> CVMutablePixelBuffer {
        var attributes = CVPixelBufferCreationAttributes(
            pixelFormatType: CVPixelFormatType(rawValue: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange),
            size: CVImageSize(width: width, height: height)
        )
        attributes.backing = .ioSurface
        return try CVMutablePixelBuffer(attributes)
    }

    private func fill(
        _ buffer: inout CVMutablePixelBuffer,
        region: PackedStereoRegion,
        luma: UInt8,
        chromaBlue: UInt8,
        chromaRed: UInt8
    ) throws {
        try buffer.withUnsafeBuffer { pixelBuffer in
            let lockStatus = CVPixelBufferLockBaseAddress(pixelBuffer, [])
            guard lockStatus == kCVReturnSuccess else {
                throw PackedStereoFrameRenderer.Error.bufferLockFailed(lockStatus)
            }
            defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, []) }

            let lumaStride = CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, 0)
            let lumaBase = try XCTUnwrap(CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, 0))
            for row in region.y ..< region.y + region.height {
                memset(lumaBase.advanced(by: row * lumaStride + region.x), Int32(luma), region.width)
            }

            let chromaStride = CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, 1)
            let chromaBase = try XCTUnwrap(CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, 1))
            for row in region.y / 2 ..< (region.y + region.height) / 2 {
                let rowBase = chromaBase.advanced(by: row * chromaStride + region.x)
                for column in 0 ..< region.width / 2 {
                    rowBase.storeBytes(of: chromaBlue, toByteOffset: column * 2, as: UInt8.self)
                    rowBase.storeBytes(of: chromaRed, toByteOffset: column * 2 + 1, as: UInt8.self)
                }
            }
        }
    }

    private func planeBytes(_ buffer: borrowing CVMutablePixelBuffer, plane: Int) throws -> [UInt8] {
        try buffer.withUnsafeBuffer { pixelBuffer in
            let lockStatus = CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
            guard lockStatus == kCVReturnSuccess else {
                throw PackedStereoFrameRenderer.Error.bufferLockFailed(lockStatus)
            }
            defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }

            let height = CVPixelBufferGetHeightOfPlane(pixelBuffer, plane)
            let widthInBytes = plane == 0
                ? CVPixelBufferGetWidthOfPlane(pixelBuffer, plane)
                : CVPixelBufferGetWidthOfPlane(pixelBuffer, plane) * 2
            let stride = CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, plane)
            let baseAddress = try XCTUnwrap(CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, plane))
            return (0 ..< height).flatMap { row in
                let rowAddress = baseAddress.advanced(by: row * stride).assumingMemoryBound(to: UInt8.self)
                return Array(UnsafeBufferPointer(start: rowAddress, count: widthInBytes))
            }
        }
    }

    private func repeatedChroma(blue: UInt8, red: UInt8, geometry: PackedStereoGeometry) -> [UInt8] {
        Array(repeating: [blue, red], count: geometry.eyeWidth * geometry.eyeHeight / 4).flatMap { $0 }
    }

    private func pixelFormat(_ buffer: borrowing CVMutablePixelBuffer) -> OSType {
        buffer.withUnsafeBuffer(CVPixelBufferGetPixelFormatType)
    }

    private func hasColorPrimariesAttachment(_ buffer: borrowing CVMutablePixelBuffer) -> Bool {
        buffer.withUnsafeBuffer { pixelBuffer in
            CVBufferCopyAttachment(pixelBuffer, kCVImageBufferColorPrimariesKey, nil) != nil
        }
    }
}

private actor RelayRefreshMode {
    enum Value { case success, expired, unreachable }
    private(set) var value: Value = .success
    func set(_ value: Value) { self.value = value }
}

private actor SuspendingRelayRefreshTransport: RelayTransport {
    private var continuation: CheckedContinuation<(Data, HTTPURLResponse), Error>?
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        for waiter in waiters { waiter.resume() }
        waiters.removeAll()
        return try await withCheckedThrowingContinuation { continuation = $0 }
    }

    func waitUntilRequested() async {
        if continuation != nil { return }
        await withCheckedContinuation { waiters.append($0) }
    }

    func fail(with error: RelayTransportError) {
        continuation?.resume(throwing: error)
        continuation = nil
    }
}

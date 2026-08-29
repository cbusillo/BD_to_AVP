import Darwin
import Foundation
import XCTest
@testable import BluRayToVisionPro

final class ConversionViewModelTests: XCTestCase {
    @MainActor
    func testSourceScopedResetMatrixResetsOnlySourceState() {
        let encoding = EncodingOptions(
            videoOutputMode: .av1Stereo,
            videoQuality: .custom(
                mvHEVC: MVHEVCOptions(generatedMergeQuality: 82),
                av1CRF: 28,
                upscaleQuality: 88
            ),
            av1CRF: 28,
            upscaleEnabled: true,
            upscaleQuality: 88,
            fieldOfView: 110,
            frameRateOverride: "24000/1001",
            resolutionOverride: "3840x2160",
            cropBlackBars: true,
            swapEyes: true,
            audioHandling: .pcm,
            audioBitrate: 512,
            audioLanguages: AudioLanguagePolicy(
                mode: .allLanguages,
                preferredLanguage: .japanese
            ),
            subtitles: SubtitlePolicy(
                mode: .preferredOnly,
                preferredLanguage: .french
            )
        )
        let job = JobOptions(
            startStage: .upscaleVideo,
            intermediatePolicy: .reusable,
            overwriteExisting: true,
            removeOriginalAfterSuccess: true,
            continueOnError: true,
            softwareEncoder: true,
            outputCommands: true,
            keepAwake: false,
            playSound: false
        )

        for sourceKind in ConversionSourceKind.allCases {
            var options = ConversionOptions(encoding: encoding, job: job)
            var titleSelection = DiscTitleSelection.all
            let summary = options.resetSourceScopedState(
                for: sourceKind,
                titleSelection: &titleSelection,
                recoveryDecisionPresent: true
            )

            XCTAssertEqual(summary.choices, SourceScopedResetSummary.Choice.allCases, sourceKind.rawValue)
            XCTAssertEqual(
                summary.message,
                "New source: reset restart stage, overwrite output, delete source after success, recovery choices, and title selection.",
                sourceKind.rawValue
            )
            XCTAssertEqual(options.job.startStage, .createMKV, sourceKind.rawValue)
            XCTAssertFalse(options.job.overwriteExisting, sourceKind.rawValue)
            XCTAssertFalse(options.job.removeOriginalAfterSuccess, sourceKind.rawValue)
            XCTAssertFalse(options.job.continueOnError, sourceKind.rawValue)
            XCTAssertEqual(options.encoding.frameRateOverride, encoding.frameRateOverride, sourceKind.rawValue)
            XCTAssertEqual(options.encoding.resolutionOverride, encoding.resolutionOverride, sourceKind.rawValue)
            XCTAssertEqual(titleSelection, .main, sourceKind.rawValue)

            XCTAssertEqual(options.encoding.videoOutputMode, encoding.videoOutputMode, sourceKind.rawValue)
            XCTAssertEqual(options.encoding.videoQuality, encoding.videoQuality, sourceKind.rawValue)
            XCTAssertEqual(options.encoding.cropBlackBars, encoding.cropBlackBars, sourceKind.rawValue)
            XCTAssertEqual(options.encoding.swapEyes, encoding.swapEyes, sourceKind.rawValue)
            XCTAssertEqual(options.encoding.audioHandling, encoding.audioHandling, sourceKind.rawValue)
            XCTAssertEqual(options.encoding.audioLanguages, encoding.audioLanguages, sourceKind.rawValue)
            XCTAssertEqual(options.encoding.subtitles, encoding.subtitles, sourceKind.rawValue)
            XCTAssertEqual(options.job.intermediatePolicy, job.intermediatePolicy, sourceKind.rawValue)
            XCTAssertEqual(options.job.softwareEncoder, job.softwareEncoder, sourceKind.rawValue)
            XCTAssertEqual(options.job.outputCommands, job.outputCommands, sourceKind.rawValue)
            XCTAssertEqual(options.job.keepAwake, job.keepAwake, sourceKind.rawValue)
            XCTAssertEqual(options.job.playSound, job.playSound, sourceKind.rawValue)
        }
    }

    @MainActor
    func testSourceScopedResetIsSilentWhenNothingChanged() {
        var options = ConversionOptions()
        var titleSelection = DiscTitleSelection.main

        let summary = options.resetSourceScopedState(
            for: .matroska,
            titleSelection: &titleSelection,
            recoveryDecisionPresent: false
        )

        XCTAssertFalse(summary.didReset)
        XCTAssertNil(summary.message)
        XCTAssertEqual(options, ConversionOptions())
        XCTAssertEqual(titleSelection, .main)
    }

    @MainActor
    func testPhysicalDiscResetKeepsRemoveOriginalFalse() {
        var options = ConversionOptions()
        options.job.removeOriginalAfterSuccess = true

        _ = options.resetSourceScopedState(for: .physicalDisc)

        XCTAssertFalse(options.job.removeOriginalAfterSuccess)
    }

    @MainActor
    func testSourceFolderSelectionTransitionsResetLifecycleState() throws {
        let rootURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let firstFolderURL = rootURL.appendingPathComponent("first", isDirectory: true)
        let secondFolderURL = rootURL.appendingPathComponent("second", isDirectory: true)
        try FileManager.default.createDirectory(at: firstFolderURL, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: secondFolderURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: rootURL) }

        let viewModel = ConversionViewModel()
        viewModel.selectSource(ConversionSource(kind: .sourceFolder, url: firstFolderURL))
        viewModel.selectSource(ConversionSource(kind: .sourceFolder, url: secondFolderURL))

        XCTAssertEqual(viewModel.source?.url, secondFolderURL.standardizedFileURL)
        XCTAssertEqual(viewModel.batchQueue?.folderSource.url, secondFolderURL.standardizedFileURL)
        XCTAssertEqual(viewModel.state.phase, .empty)
        XCTAssertNil(viewModel.state.recoveryDecision)
    }

    @MainActor
    func testCanonicalObservabilityUpdatesLiveStatusAndPersists() async throws {
        let terminalDelivered = expectation(description: "terminal delivered")
        let observabilityEvent = try makeTestObservabilityEvent(kind: "tool.started")
        let worker = ControlledWorkerClient(
            terminalDelivered: terminalDelivered,
            observabilityEvent: observabilityEvent
        )
        let store = RecordingObservabilityEventStore()
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            observabilityEventStore: store
        )

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)
            await fulfillment(of: [terminalDelivered], timeout: 2)

            XCTAssertEqual(store.events, [observabilityEvent])
            XCTAssertEqual(viewModel.liveObservabilityStatus.stageID, "create_mkv")
            XCTAssertEqual(viewModel.liveObservabilityStatus.toolID, "makemkvcon")
            XCTAssertEqual(viewModel.liveObservabilityStatus.processState, .running)

            await viewModel.stopForQuit()
        }
    }

    @MainActor
    func testUpdateInstallRelaunchWaitsForActiveWorker() async throws {
        let terminalDelivered = expectation(description: "terminal delivered")
        let worker = ControlledWorkerClient(terminalDelivered: terminalDelivered)
        let viewModel = ConversionViewModel { worker }
        var installCount = 0

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)
            await fulfillment(of: [terminalDelivered], timeout: 2)

            XCTAssertTrue(
                viewModel.postponeInstallUntilIdle {
                    installCount += 1
                }
            )
            XCTAssertEqual(installCount, 0)

            await viewModel.stopForQuit()

            XCTAssertEqual(installCount, 1)
            XCTAssertFalse(viewModel.hasActiveWorker)
        }
    }

    @MainActor
    func testUpdateInstallRelaunchDoesNotPostponeWhileIdle() {
        let viewModel = ConversionViewModel()
        var installCount = 0

        XCTAssertFalse(
            viewModel.postponeInstallUntilIdle {
                installCount += 1
            }
        )
        XCTAssertEqual(installCount, 0)
    }

    @MainActor
    func testTerminalCompletionWinsIfStopIsRequestedBeforeProcessExit() async throws {
        let terminalDelivered = expectation(description: "terminal delivered")
        let worker = ControlledWorkerClient(terminalDelivered: terminalDelivered)
        let viewModel = ConversionViewModel { worker }

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)

            await fulfillment(of: [terminalDelivered], timeout: 2)
            XCTAssertTrue(viewModel.hasActiveWorker)
            XCTAssertEqual(viewModel.state.phase, .inspecting)

            await viewModel.stopForQuit()

            XCTAssertFalse(viewModel.hasActiveWorker)
            XCTAssertEqual(viewModel.state.phase, .completed)
            XCTAssertEqual(viewModel.state.result?.name, "movie")
        }
    }

    @MainActor
    func testUnsupportedSourceCannotBeRetried() {
        let viewModel = ConversionViewModel()
        let sourceURL = URL(fileURLWithPath: "/tmp/movie.mp4")

        viewModel.selectSource(sourceURL)

        XCTAssertEqual(viewModel.state.phase, .failed)
        XCTAssertFalse(viewModel.state.failureRetryable)
        XCTAssertFalse(viewModel.canRetry)

        viewModel.restartInspection()
        XCTAssertEqual(viewModel.state.phase, .failed)
    }

    @MainActor
    func testUnsupportedSelectionClearsPreviouslyInspectedSource() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let validSourceURL = directoryURL.appendingPathComponent("Feature.mkv")
        let unsupportedSourceURL = directoryURL.appendingPathComponent("Feature.img")
        _ = FileManager.default.createFile(atPath: validSourceURL.path, contents: Data("video".utf8))
        _ = FileManager.default.createFile(atPath: unsupportedSourceURL.path, contents: Data("disc".utf8))
        defer { try? FileManager.default.removeItem(at: directoryURL) }

        let inspectionDone = expectation(description: "inspection done")
        let viewModel = ConversionViewModel {
            TwoPhaseWorkerClient(onInspectionComplete: { inspectionDone.fulfill() })
        }

        viewModel.selectSource(validSourceURL)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        XCTAssertNotNil(viewModel.state.result)

        viewModel.selectSource(unsupportedSourceURL)

        XCTAssertNil(viewModel.source)
        XCTAssertNil(viewModel.state.result)
        XCTAssertEqual(viewModel.state.phase, .failed)
        XCTAssertEqual(
            viewModel.state.failureMessage,
            "Choose a 3D Blu-ray disc, ISO, Blu-ray folder, MKV, MTS, or M2TS source."
        )
    }

    @MainActor
    func testDiscImageSelectionStartsInspection() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let imageURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: imageURL.path, contents: Data("disc".utf8))
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let inspectionDone = expectation(description: "inspection done")
        let viewModel = ConversionViewModel {
            TwoPhaseWorkerClient(onInspectionComplete: { inspectionDone.fulfill() })
        }

        viewModel.selectSource(ConversionSource(kind: .discImage, url: imageURL))
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }

        XCTAssertEqual(viewModel.source?.kind, .discImage)
        XCTAssertEqual(viewModel.state.phase, .completed)
        XCTAssertNotNil(viewModel.state.result)
        XCTAssertFalse(viewModel.hasActiveWorker)
    }

    @MainActor
    func testStartConversionStartsWorkerForInspectedISO() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let conversionStarted = expectation(description: "conversion started")
        let title = SourceTitle(
            id: "makemkv:0",
            name: "Main Movie",
            outputName: "Feature",
            durationSeconds: 7_200,
            resolution: "1920x1080",
            frameRate: "24000/1001",
            mainFeature: true
        )
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24000/1001",
            interlaced: false,
            sizeBytes: 10,
            durationSeconds: 7_200,
            titles: [title]
        )
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { spec in
                XCTAssertEqual(URL(fileURLWithPath: spec.source.path).pathExtension, "iso")
                XCTAssertEqual(spec.operation, "convert_source")
                XCTAssertEqual(spec.source.titleID, title.id)
                conversionStarted.fulfill()
            },
            inspectionResult: inspection
        )
        let viewModel = ConversionViewModel { worker }
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let imageURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: imageURL.path, contents: Data("disc".utf8))
        defer { try? FileManager.default.removeItem(at: directoryURL) }

        viewModel.selectSource(ConversionSource(kind: .discImage, url: imageURL))
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }

        let draft = ConversionDraft(
            source: viewModel.source!,
            sourceDetails: viewModel.state.result,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies"),
            options: ConversionOptions(),
            selectedTitle: title
        )
        viewModel.startConversion(draft: draft)

        await fulfillment(of: [conversionStarted], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        XCTAssertEqual(viewModel.state.phase, .completed)
    }

    @MainActor
    func testDiagnosticStorageSamplingRunsOffMainActor() async throws {
        try await withTemporarySource { sourceURL in
            let observation = DiagnosticProbeThreadObservation()
            let worker = TwoPhaseWorkerClient()
            let viewModel = ConversionViewModel(
                clientFactory: { worker },
                diagnosticStorageProbe: ThreadRecordingStorageProbe(observation: observation)
            )

            viewModel.selectSource(sourceURL)
            while viewModel.hasActiveWorker { await Task.yield() }
            let draft = ConversionDraft(
                source: try XCTUnwrap(viewModel.source),
                sourceDetails: try XCTUnwrap(viewModel.state.result),
                profile: BuiltInProfile.balanced.profile,
                destinationURL: sourceURL.deletingLastPathComponent(),
                options: ConversionOptions()
            )

            viewModel.startConversion(draft: draft)
            while viewModel.hasActiveWorker { await Task.yield() }
            let deadline = Date().addingTimeInterval(2)
            while observation.observedMainThread == nil, Date() < deadline {
                try await Task.sleep(nanoseconds: 10_000_000)
            }

            XCTAssertEqual(viewModel.state.phase, .completed)
            XCTAssertEqual(observation.observedMainThread, false)
        }
    }

    @MainActor
    func testDiagnosticBundleIncludesUserCommentFromViewModel() async throws {
        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: outputDirectory) }

        let viewModel = ConversionViewModel()
        let comment = try XCTUnwrap(
            DiagnosticUserComment.normalize("Left and right outputs stop growing at 1.48 GB.")
        )

        let artifact = try await viewModel.captureDiagnosticBundle(
            in: outputDirectory,
            userComment: comment
        )

        XCTAssertEqual(
            artifact.preview.userDescription,
            "Left and right outputs stop growing at 1.48 GB."
        )
        XCTAssertEqual(
            artifact.handoff.redactedDescription,
            "Left and right outputs stop growing at 1.48 GB."
        )
    }

    @MainActor
    func testDiagnosticBundleCancellationReachesDetachedBuilder() async throws {
        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: outputDirectory) }

        let archiveWriter = BlockingDiagnosticArchiveWriter()
        let builder = DiagnosticBundleBuilder(archiveWriter: archiveWriter.write)
        let viewModel = ConversionViewModel(diagnosticBundleBuilder: builder)
        let captureTask = Task {
            try await viewModel.captureDiagnosticBundle(in: outputDirectory)
        }

        let deadline = Date().addingTimeInterval(2)
        while !archiveWriter.hasStarted, Date() < deadline {
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertTrue(archiveWriter.hasStarted)

        captureTask.cancel()
        do {
            _ = try await captureTask.value
            XCTFail("Expected diagnostic bundle capture to be cancelled")
        } catch is CancellationError {
            // Expected.
        }

        XCTAssertTrue(archiveWriter.observedCancellation)
        XCTAssertTrue((try FileManager.default.contentsOfDirectory(atPath: outputDirectory.path)).isEmpty)
    }

    @MainActor
    func testUnavailableDiscTitleCanBeReanalyzedBeforeRetry() async throws {
        let inspectionDone = expectation(description: "inspection done")
        inspectionDone.expectedFulfillmentCount = 2
        let conversionFailed = expectation(description: "conversion failed")
        let title = SourceTitle(
            id: "makemkv:0",
            name: "Main Movie",
            outputName: "Feature",
            durationSeconds: 7_200,
            resolution: "1920x1080",
            frameRate: "24000/1001",
            mainFeature: true
        )
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24000/1001",
            interlaced: false,
            titles: [title]
        )
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { _ in conversionFailed.fulfill() },
            failureOnConversionNumber: 1,
            conversionFailure: WorkerFailure(
                code: "title_unavailable",
                message: "The selected 3D video is no longer available.",
                details: nil,
                retryable: true
            ),
            inspectionResult: inspection
        )
        let viewModel = ConversionViewModel { worker }
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let imageURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: imageURL.path, contents: Data("disc".utf8))
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let source = ConversionSource(kind: .discImage, url: imageURL)

        viewModel.selectSource(source)
        while viewModel.hasActiveWorker { await Task.yield() }
        viewModel.startConversion(
            draft: ConversionDraft(
                source: source,
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: ConversionOptions(),
                selectedTitle: title
            )
        )
        await fulfillment(of: [conversionFailed], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(viewModel.state.failureCode, "title_unavailable")
        XCTAssertTrue(viewModel.canRetry)
        viewModel.restartInspection()
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }

        XCTAssertEqual(viewModel.state.phase, .completed)
        XCTAssertNil(viewModel.state.failureCode)
        XCTAssertEqual(viewModel.state.result?.titles, [title])
    }

    @MainActor
    func testBluRayFolderSelectionStartsInspectionAndConversion() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let conversionStarted = expectation(description: "conversion started")
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { spec in
                XCTAssertEqual(spec.source.kind, .bluRayFolder)
                conversionStarted.fulfill()
            }
        )
        let viewModel = ConversionViewModel { worker }
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let discURL = directoryURL.appendingPathComponent("Feature", isDirectory: true)
        try FileManager.default.createDirectory(
            at: discURL.appendingPathComponent("BDMV", isDirectory: true),
            withIntermediateDirectories: true
        )
        defer { try? FileManager.default.removeItem(at: directoryURL) }

        viewModel.selectSource(discURL)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }

        let draft = ConversionDraft(
            source: try XCTUnwrap(viewModel.source),
            sourceDetails: viewModel.state.result,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: directoryURL.appendingPathComponent("Output", isDirectory: true),
            options: ConversionOptions()
        )
        viewModel.startConversion(draft: draft)

        await fulfillment(of: [conversionStarted], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        XCTAssertEqual(viewModel.state.phase, .completed)
    }

    @MainActor
    func testPhysicalDiscSelectionStartsInspectionAndConversion() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let conversionStarted = expectation(description: "conversion started")
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { spec in
                XCTAssertEqual(spec.source.kind, .physicalDisc)
                XCTAssertEqual(spec.source.path, "/dev/disk9")
                XCTAssertFalse(spec.job?.removeOriginal == true)
                conversionStarted.fulfill()
            }
        )
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            sourceAvailabilityResolver: { _ in true }
        )
        let volumeURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: volumeURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: volumeURL) }
        let discSource = ConversionSource(
            kind: .physicalDisc,
            url: volumeURL,
            workerSourcePath: "/dev/disk9"
        )

        viewModel.selectSource(discSource)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }

        let draft = ConversionDraft(
            source: discSource,
            sourceDetails: viewModel.state.result,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: FileManager.default.temporaryDirectory,
            options: ConversionOptions()
        )
        viewModel.startConversion(draft: draft)

        await fulfillment(of: [conversionStarted], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        XCTAssertEqual(viewModel.state.phase, .completed)
    }

    @MainActor
    func testPhysicalDiscConversionRejectsDestinationOnDisc() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let viewModel = ConversionViewModel(
            clientFactory: { TwoPhaseWorkerClient(onInspectionComplete: { inspectionDone.fulfill() }) },
            sourceAvailabilityResolver: { _ in true }
        )
        let volumeURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: volumeURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: volumeURL) }
        let discSource = ConversionSource(
            kind: .physicalDisc,
            url: volumeURL,
            workerSourcePath: "/dev/disk9"
        )

        viewModel.selectSource(discSource)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        viewModel.startConversion(
            draft: ConversionDraft(
                source: discSource,
                sourceDetails: viewModel.state.result,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: volumeURL.appendingPathComponent("Output", isDirectory: true),
                options: ConversionOptions()
            )
        )

        XCTAssertFalse(viewModel.hasActiveWorker)
        XCTAssertEqual(viewModel.state.failureMessage, "Choose a destination outside the Blu-ray disc.")
    }

    @MainActor
    func testUnmountedPhysicalDiscClearsIdleSelection() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let viewModel = ConversionViewModel {
            TwoPhaseWorkerClient(onInspectionComplete: { inspectionDone.fulfill() })
        }
        let volumeURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: volumeURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: volumeURL) }

        viewModel.selectSource(
            ConversionSource(kind: .physicalDisc, url: volumeURL, workerSourcePath: "/dev/disk9")
        )
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }

        viewModel.sourceVolumeDidUnmount(volumeURL)

        XCTAssertNil(viewModel.source)
        XCTAssertEqual(viewModel.state.phase, .empty)
    }

    @MainActor
    func testUnmountedPhysicalDiscStopsActiveConversion() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let conversionStarted = expectation(description: "conversion started")
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { _ in conversionStarted.fulfill() },
            waitsForConversionCancellation: true
        )
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            sourceAvailabilityResolver: { _ in true }
        )
        let volumeURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: volumeURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: volumeURL) }
        let source = ConversionSource(
            kind: .physicalDisc,
            url: volumeURL,
            workerSourcePath: "/dev/disk9"
        )

        viewModel.selectSource(source)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        viewModel.startConversion(
            draft: ConversionDraft(
                source: source,
                sourceDetails: viewModel.state.result,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: FileManager.default.temporaryDirectory,
                options: ConversionOptions()
            )
        )
        await fulfillment(of: [conversionStarted], timeout: 2)

        viewModel.sourceVolumeDidUnmount(volumeURL)

        XCTAssertEqual(viewModel.state.phase, .stopping)
    }

    @MainActor
    func testMakeMKVDecisionStartsFreshStageTwoRecoveryJob() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let firstConversion = expectation(description: "first conversion received")
        let recoveryConversion = expectation(description: "recovery conversion received")
        var conversionJobs: [WorkerJobSpec] = []
        let decision = WorkerDecision(
            identifier: "mkv_creation_decision_required",
            prompt: "MakeMKV reported errors.",
            choices: ["retry_continue_on_error", "cancel"],
            details: "Continue only if the created MKV is usable."
        )
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { job in
                conversionJobs.append(job)
                if conversionJobs.count == 1 {
                    firstConversion.fulfill()
                } else {
                    recoveryConversion.fulfill()
                }
            },
            recoveryDecision: decision
        )
        let viewModel = ConversionViewModel { worker }

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)
            await fulfillment(of: [inspectionDone], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }
            let draft = ConversionDraft(
                source: try XCTUnwrap(viewModel.source),
                sourceDetails: viewModel.state.result,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/Movies"),
                options: ConversionOptions()
            )

            viewModel.startConversion(draft: draft)
            await fulfillment(of: [firstConversion], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            XCTAssertEqual(viewModel.state.phase, .decisionRequired)
            XCTAssertEqual(viewModel.state.recoveryDecision, decision)
            XCTAssertTrue(viewModel.resolveRecoveryChoice(.retryContinueOnError))
            await fulfillment(of: [recoveryConversion], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            XCTAssertEqual(conversionJobs.count, 2)
            XCTAssertNotEqual(conversionJobs[0].jobID, conversionJobs[1].jobID)
            XCTAssertEqual(conversionJobs[1].job?.startStage, ConversionStage.extractMVCAndAudio.rawValue)
            XCTAssertTrue(conversionJobs[1].job?.continueOnError == true)
            XCTAssertFalse(conversionJobs[1].job?.keepFiles == true)
            XCTAssertEqual(draft.options.job.startStage, .createMKV)
            XCTAssertFalse(draft.options.job.continueOnError)
            XCTAssertEqual(viewModel.state.phase, .completed)
        }
    }

    @MainActor
    func testSubtitleDecisionStartsFreshStageThreeRecoveryWithoutSubtitles() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let firstConversion = expectation(description: "first conversion received")
        let recoveryConversion = expectation(description: "recovery conversion received")
        var conversionJobs: [WorkerJobSpec] = []
        let decision = WorkerDecision(
            identifier: "subtitle_decision_required",
            prompt: "Subtitle extraction needs attention.",
            choices: ["retry_without_subtitles", "cancel"],
            details: "Continue without subtitles."
        )
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { job in
                conversionJobs.append(job)
                if conversionJobs.count == 1 {
                    firstConversion.fulfill()
                } else {
                    recoveryConversion.fulfill()
                }
            },
            recoveryDecision: decision
        )
        let viewModel = ConversionViewModel { worker }

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)
            await fulfillment(of: [inspectionDone], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }
            let draft = ConversionDraft(
                source: try XCTUnwrap(viewModel.source),
                sourceDetails: viewModel.state.result,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/Movies"),
                options: ConversionOptions()
            )

            viewModel.startConversion(draft: draft)
            await fulfillment(of: [firstConversion], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            XCTAssertTrue(viewModel.resolveRecoveryChoice(.retryWithoutSubtitles))
            await fulfillment(of: [recoveryConversion], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            XCTAssertEqual(conversionJobs[1].job?.startStage, ConversionStage.extractSubtitles.rawValue)
            XCTAssertEqual(conversionJobs[1].encoding?.subtitles.mode, .off)
            XCTAssertNil(conversionJobs[1].encoding?.subtitles.preferredLanguage)
            XCTAssertEqual(draft.options.encoding.subtitles.mode, .preferredPlusOthers)
            XCTAssertEqual(viewModel.state.phase, .completed)
        }
    }

    @MainActor
    func testCancellingRecoveryStartsNoAdditionalWorker() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let conversionStarted = expectation(description: "conversion received")
        var conversionCount = 0
        let decision = WorkerDecision(
            identifier: "subtitle_decision_required",
            prompt: "Subtitle extraction needs attention.",
            choices: ["retry_without_subtitles", "cancel"],
            details: nil
        )
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { _ in
                conversionCount += 1
                conversionStarted.fulfill()
            },
            recoveryDecision: decision
        )
        let viewModel = ConversionViewModel { worker }

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)
            await fulfillment(of: [inspectionDone], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }
            viewModel.startConversion(
                draft: ConversionDraft(
                    source: try XCTUnwrap(viewModel.source),
                    sourceDetails: viewModel.state.result,
                    profile: BuiltInProfile.balanced.profile,
                    destinationURL: URL(fileURLWithPath: "/Movies"),
                    options: ConversionOptions()
                )
            )
            await fulfillment(of: [conversionStarted], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            XCTAssertFalse(viewModel.canSelectSource)
            let previewViewModel = PreviewViewModel(
                clientFactory: { worker },
                cache: PreviewCache(
                    rootURL: sourceURL.deletingLastPathComponent().appendingPathComponent("Previews", isDirectory: true)
                )
            )
            let coordinator = AppWorkCoordinator(conversion: viewModel, preview: previewViewModel)
            XCTAssertTrue(coordinator.hasActiveWorker)
            let deferredUpdateRan = expectation(description: "deferred update ran")
            XCTAssertTrue(coordinator.postponeInstallUntilIdle { deferredUpdateRan.fulfill() })
            XCTAssertTrue(viewModel.resolveRecoveryChoice(.cancel))
            await fulfillment(of: [deferredUpdateRan], timeout: 2)

            XCTAssertEqual(conversionCount, 1)
            XCTAssertEqual(viewModel.state.phase, .failed)
            XCTAssertNil(viewModel.state.recoveryDecision)
            XCTAssertTrue(viewModel.canSelectSource)
        }
    }

    @MainActor
    func testStartConversionStartsWorkerWithConvertSourceJobSpec() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let conversionStarted = expectation(description: "conversion started")
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { spec in
                XCTAssertEqual(spec.operation, "convert_source")
                XCTAssertNotNil(spec.destination)
                XCTAssertNotNil(spec.encoding)
                XCTAssertNil(spec.preview)
                conversionStarted.fulfill()
            }
        )
        let viewModel = ConversionViewModel { worker }

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)
            await fulfillment(of: [inspectionDone], timeout: 2)
            // Drain the inspection Task so finish() runs and hasActiveWorker becomes false
            while viewModel.hasActiveWorker { await Task.yield() }

            let draft = ConversionDraft(
                source: viewModel.source!,
                sourceDetails: viewModel.state.result,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/Movies"),
                options: ConversionOptions()
            )
            viewModel.startConversion(draft: draft)

            await fulfillment(of: [conversionStarted], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }
            XCTAssertEqual(viewModel.state.operationKind, .conversion)
            XCTAssertEqual(viewModel.state.phase, .completed)
            XCTAssertEqual(viewModel.state.conversionResult?.outputPath, "/Movies/movie_AVP.mov")
        }
    }

    @MainActor
    func testSingleDraftQueueUsesSingleConversionPath() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let conversionStarted = expectation(description: "conversion started")
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { _ in conversionStarted.fulfill() }
        )
        let viewModel = ConversionViewModel { worker }

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)
            await fulfillment(of: [inspectionDone], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            let draft = ConversionDraft(
                source: try XCTUnwrap(viewModel.source),
                sourceDetails: viewModel.state.result,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/Movies"),
                options: ConversionOptions()
            )
            viewModel.startConversionQueue(drafts: [draft])

            await fulfillment(of: [conversionStarted], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }
            XCTAssertTrue(viewModel.queueItems.isEmpty)
            XCTAssertEqual(viewModel.state.phase, .completed)
        }
    }

    @MainActor
    func testPersistentQueueRunsMultipleSingleSourceDraftsSequentially() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURLs = ["One.mkv", "Two.mkv"].map { directoryURL.appendingPathComponent($0) }
        for sourceURL in sourceURLs {
            _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8))
        }
        let scenario = BatchWorkerScenario()
        let queueStore = ConversionQueueStore.inMemory()
        let viewModel = ConversionViewModel(
            clientFactory: { scenario.makeClient() },
            durableQueueStore: queueStore
        )
        let drafts = sourceURLs.map { sourceURL in
            let inspection = SourceInspection(
                name: sourceURL.deletingPathExtension().lastPathComponent,
                resolution: "1920x1080",
                frameRate: "24/1",
                interlaced: false,
                sizeBytes: 10
            )
            return ConversionDraft(
                source: ConversionSource(kind: .matroska, url: sourceURL),
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: ConversionOptions()
            )
        }

        _ = try await viewModel.appendPersistentQueueDrafts(drafts)
        let startOutcome = await viewModel.startPersistentQueue()
        XCTAssertEqual(startOutcome, .accepted(.running))

        while queueStore.items.count < sourceURLs.count { await Task.yield() }
        while viewModel.hasActiveWorker || viewModel.hasQueuedWork { await Task.yield() }
        XCTAssertEqual(
            scenario.records.filter { $0.operation == "convert_source" }.map(\.sourcePath),
            sourceURLs.map(\.path)
        )
        XCTAssertEqual(queueStore.items.map(\.origin), [.singleSource, .singleSource])
        XCTAssertEqual(queueStore.items.map(\.state), [.completed, .completed])
    }

    @MainActor
    func testHeldPersistentQueueItemPreservesIdentityAndResolutionTrace() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("Movie.mkv")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8))
        let scenario = BatchWorkerScenario()
        let queueStore = ConversionQueueStore.inMemory()
        let viewModel = ConversionViewModel(
            clientFactory: { scenario.makeClient() },
            durableQueueStore: queueStore
        )
        var options = ConversionOptions()
        try options.encoding.selectQualityStep(.maximumDetail)
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: sourceURL),
            sourceDetails: SourceInspection(
                name: "Movie",
                resolution: "1920x1080",
                frameRate: "24/1",
                interlaced: false,
                sizeBytes: 10
            ),
            profile: BuiltInProfile.balanced.profile,
            destinationURL: directoryURL,
            options: options
        )
        guard case let .conflict(conflict) = RouteQualityEngine.propose(
            options: options,
            edit: .reusableIntermediates(true)
        ) else {
            return XCTFail("Expected reusable-file conflict")
        }
        _ = try await viewModel.appendPersistentQueueDrafts([draft], conflicts: [conflict])
        let admittedID = try XCTUnwrap(queueStore.items.first?.id)
        let group = try XCTUnwrap(viewModel.persistentQueueResolutionGroups.first)
        var selection = QueueResolutionSelection()
        selection.resolutionID = try XCTUnwrap(
            group.conflict.resolutions.first(where: {
                $0.choice == .keepRequestedWorkflow && $0.isAvailable
            })?.id
        )
        try await viewModel.resolvePersistentQueueItems(group: group, selection: selection)
        let trace = try XCTUnwrap(queueStore.items.first?.resolutionTrace)

        let startOutcome = await viewModel.startPersistentQueue()
        XCTAssertEqual(startOutcome, .accepted(.running))

        while queueStore.items.isEmpty { await Task.yield() }
        while viewModel.hasActiveWorker || viewModel.hasQueuedWork { await Task.yield() }
        let storedItem = try XCTUnwrap(queueStore.items.first)
        XCTAssertEqual(storedItem.id, admittedID)
        XCTAssertEqual(storedItem.resolutionTrace, trace)
        XCTAssertEqual(storedItem.origin, .singleSource)
        XCTAssertEqual(storedItem.state, .completed)
    }

    @MainActor
    func testPersistentQueueAppendWriteFailureFailsClosed() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("Movie.mkv")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8))
        let queueStore = ConversionQueueStore(
            fileURL: directoryURL.appendingPathComponent("queue.json"),
            dataWriter: { _, _ in throw RuntimePersistenceTestError.writeFailed }
        )
        let viewModel = ConversionViewModel(durableQueueStore: queueStore)
        let draft =
            ConversionDraft(
                source: ConversionSource(kind: .matroska, url: sourceURL),
                sourceDetails: SourceInspection(
                    name: "Movie",
                    resolution: "1920x1080",
                    frameRate: "24/1",
                    interlaced: false,
                    sizeBytes: 10
                ),
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: ConversionOptions()
            )

        do {
            _ = try await viewModel.appendPersistentQueueDrafts([draft])
            XCTFail("Expected persistent queue append to fail")
        } catch {
        }
        XCTAssertTrue(queueStore.items.isEmpty)
    }

    @MainActor
    func testPersistentQueueTerminalWriteFailureStopsActiveWork() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("Movie.mkv")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8))
        let writeCount = RuntimePersistenceTestCounter()
        let queueStore = ConversionQueueStore(
            fileURL: directoryURL.appendingPathComponent("queue.json"),
            dataWriter: { data, url in
                if writeCount.incrementAndReturn() == 3 {
                    throw RuntimePersistenceTestError.writeFailed
                }
                try data.write(to: url, options: .atomic)
            }
        )
        let scenario = BatchWorkerScenario()
        let viewModel = ConversionViewModel(
            clientFactory: { scenario.makeClient() },
            durableQueueStore: queueStore
        )
        let draft =
            ConversionDraft(
                source: ConversionSource(kind: .matroska, url: sourceURL),
                sourceDetails: SourceInspection(
                    name: "Movie",
                    resolution: "1920x1080",
                    frameRate: "24/1",
                    interlaced: false,
                    sizeBytes: 10
                ),
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: ConversionOptions()
            )

        _ = try await viewModel.appendPersistentQueueDrafts([draft])
        let startOutcome = await viewModel.startPersistentQueue()
        XCTAssertEqual(startOutcome, .accepted(.running))
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertNotNil(viewModel.durableQueueRuntimeDiagnostic)
        XCTAssertFalse(viewModel.hasActiveWork)
    }

    @MainActor
    func testStopActiveWorkerCancelsConversionAndTransitionsToStopping() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let conversionStarted = expectation(description: "conversion started")
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { _ in conversionStarted.fulfill() },
            waitsForConversionCancellation: true
        )
        let viewModel = ConversionViewModel { worker }

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)
            await fulfillment(of: [inspectionDone], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            let draft = ConversionDraft(
                source: viewModel.source!,
                sourceDetails: viewModel.state.result,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/Movies"),
                options: ConversionOptions()
            )
            viewModel.startConversion(draft: draft)
            await fulfillment(of: [conversionStarted], timeout: 2)

            let startOutcome = await viewModel.startPersistentQueue()
            XCTAssertEqual(startOutcome, .rejected(.noEligibleItems))

            viewModel.stopActiveWorker()

            XCTAssertEqual(viewModel.state.phase, .stopping)
        }
    }

    @MainActor
    func testStartConversionRejectsDraftForDifferentSource() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let viewModel = ConversionViewModel {
            TwoPhaseWorkerClient(onInspectionComplete: { inspectionDone.fulfill() })
        }

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)
            await fulfillment(of: [inspectionDone], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            let otherURL = sourceURL.deletingLastPathComponent().appendingPathComponent("other.m2ts")
            _ = FileManager.default.createFile(atPath: otherURL.path, contents: Data("video".utf8))
            let draft = ConversionDraft(
                source: ConversionSource(kind: .transportStream, url: otherURL),
                sourceDetails: viewModel.state.result,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/Movies"),
                options: ConversionOptions()
            )

            viewModel.startConversion(draft: draft)

            XCTAssertFalse(viewModel.hasActiveWorker)
            XCTAssertEqual(viewModel.state.phase, .failed)
            XCTAssertEqual(viewModel.state.failureMessage, "Analyze the selected source before starting conversion.")
        }
    }

    @MainActor
    func testStartConversionUsesSuppliedParentJobID() async throws {
        let inspectionDone = expectation(description: "inspection done")
        let conversionStarted = expectation(description: "conversion started")
        let expectedJobID = UUID()
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { spec in
                XCTAssertEqual(spec.jobID, expectedJobID)
                conversionStarted.fulfill()
            }
        )
        let viewModel = ConversionViewModel { worker }

        try await withTemporarySource { sourceURL in
            viewModel.selectSource(sourceURL)
            await fulfillment(of: [inspectionDone], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            let draft = ConversionDraft(
                source: viewModel.source!,
                sourceDetails: viewModel.state.result,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: URL(fileURLWithPath: "/Movies"),
                options: ConversionOptions()
            )

            viewModel.startConversion(draft: draft, jobID: expectedJobID)

            await fulfillment(of: [conversionStarted], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }
            XCTAssertEqual(viewModel.state.phase, .completed)
        }
    }

    @MainActor
    func testBatchConversionRunsOneInspectionAndConversionPerSourceSequentially() async throws {
        try await withTemporaryBatchSources(["B.mkv", "a.m2ts"]) { folderURL, _, destinationURL in
            let scenario = BatchWorkerScenario()
            let viewModel = ConversionViewModel { scenario.makeClient() }

            viewModel.selectSource(folderURL)

            XCTAssertFalse(viewModel.hasActiveWork)
            XCTAssertEqual(viewModel.batchQueue?.items.map(\.source.displayName), ["a.m2ts", "B.mkv"])
            XCTAssertEqual(scenario.clientCount, 0)

            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchCompletion(viewModel)

            let queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.completed, .completed])
            XCTAssertEqual(queue.completedCount, 2)
            XCTAssertEqual(queue.summaryText, "2 of 2 completed")
            XCTAssertEqual(scenario.clientCount, 4)
            XCTAssertEqual(scenario.maximumActiveCount, 1)
            XCTAssertEqual(
                scenario.records.map { "\($0.operation):\(URL(fileURLWithPath: $0.sourcePath).lastPathComponent)" },
                [
                    "inspect_source:a.m2ts",
                    "convert_source:a.m2ts",
                    "inspect_source:B.mkv",
                    "convert_source:B.mkv",
                ]
            )
        }
    }

    @MainActor
    func testSourceFolderBatchProjectionSurvivesHistoryCompaction() async throws {
        try await withTemporaryBatchSources(["first.mkv", "second.m2ts"]) { folderURL, _, destinationURL in
            let historyDraft = ConversionDraft(
                source: ConversionSource(kind: .matroska, url: folderURL.appendingPathComponent("history.mkv")),
                sourceDetails: nil,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            let queueStore = ConversionQueueStore.inMemory()
            try await queueStore.replaceItems(makeCompletedHistoryItems(count: 5, draft: historyDraft))
            let scenario = BatchWorkerScenario()
            let viewModel = ConversionViewModel(
                clientFactory: { scenario.makeClient() },
                durableQueueStore: queueStore
            )

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchCompletion(viewModel)

            XCTAssertEqual(viewModel.batchQueue?.items.map(\.status), [.completed, .completed])
            XCTAssertEqual(queueStore.items.filter { $0.origin == .sourceFolder }.map(\.state), [.completed, .completed])
            XCTAssertEqual(queueStore.items.count, 6)
            XCTAssertEqual(queueStore.items.map(\.ordinal), Array(queueStore.items.indices))
        }
    }

    @MainActor
    func testCompletedBatchCanStartAgainWithFreshDurableItemIDs() async throws {
        try await withTemporaryBatchSources(["first.mkv", "second.m2ts"]) { folderURL, _, destinationURL in
            let scenario = BatchWorkerScenario()
            let queueStore = ConversionQueueStore.inMemory()
            let viewModel = ConversionViewModel(clientFactory: { scenario.makeClient() }, durableQueueStore: queueStore)

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchCompletion(viewModel)
            let firstRunIDs = Set(try XCTUnwrap(viewModel.batchQueue).items.map(\.id))

            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchCompletion(viewModel)
            let secondRunIDs = Set(try XCTUnwrap(viewModel.batchQueue).items.map(\.id))

            XCTAssertTrue(firstRunIDs.isDisjoint(with: secondRunIDs))
            XCTAssertEqual(queueStore.items.map(\.state), [.completed, .completed, .completed, .completed])
            XCTAssertNil(viewModel.durableQueueRuntimeDiagnostic)
            XCTAssertEqual(scenario.records.count, 8)
        }
    }

    @MainActor
    func testStoppedBatchCanStartAgainWithFreshDurableItemIDs() async throws {
        try await withTemporaryBatchSources(["first.mkv", "second.m2ts"]) { folderURL, sourceURLs, destinationURL in
            let scenario = BatchWorkerScenario(holdConversionForCancellation: [sourceURLs[0].path])
            let queueStore = ConversionQueueStore.inMemory()
            let viewModel = ConversionViewModel(clientFactory: { scenario.makeClient() }, durableQueueStore: queueStore)

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchStatus(viewModel, status: .converting)
            viewModel.stopActiveWorker()
            await waitForBatchCompletion(viewModel)
            let firstRunIDs = Set(try XCTUnwrap(viewModel.batchQueue).items.map(\.id))

            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchCompletion(viewModel)
            let secondRunIDs = Set(try XCTUnwrap(viewModel.batchQueue).items.map(\.id))

            XCTAssertTrue(firstRunIDs.isDisjoint(with: secondRunIDs))
            XCTAssertEqual(queueStore.items.map(\.state), [.stopped, .notStarted, .completed, .completed])
            XCTAssertNil(viewModel.durableQueueRuntimeDiagnostic)
        }
    }

    @MainActor
    func testBatchFailureContinuesAndExplicitRetryUsesStoredDraft() async throws {
        try await withTemporaryBatchSources(["first.mkv", "second.m2ts"]) { folderURL, sourceURLs, destinationURL in
            var options = ConversionOptions()
            options.encoding.mvHEVC.generatedMergeQuality = 83
            let firstPath = sourceURLs[0].path
            let scenario = BatchWorkerScenario(failConversionOnceFor: [firstPath])
            let viewModel = ConversionViewModel { scenario.makeClient() }

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.originalResolution.profile,
                destinationURL: destinationURL,
                options: options
            )
            await waitForBatchCompletion(viewModel)

            var queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.failed, .completed])
            XCTAssertEqual(queue.failedCount, 1)
            XCTAssertEqual(queue.completedCount, 1)
            XCTAssertEqual(queue.items[0].failureMessage, "Synthetic conversion failure")
            XCTAssertEqual(scenario.maximumActiveCount, 1)

            let failedItemID = queue.items[0].id
            viewModel.retryBatchItem(failedItemID)
            await waitForBatchCompletion(viewModel)

            queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.completed, .completed])
            XCTAssertEqual(queue.items[0].draft?.profile.id, BuiltInProfile.originalResolution.id)
            XCTAssertEqual(queue.items[0].draft?.destinationURL, destinationURL)
            XCTAssertEqual(queue.items[0].draft?.options.encoding.mvHEVC.generatedMergeQuality, 83)
            XCTAssertEqual(
                scenario.records.filter { $0.sourcePath == firstPath }.map(\.operation),
                ["inspect_source", "convert_source", "inspect_source", "convert_source"]
            )
        }
    }

    @MainActor
    func testBatchRetryDiagnosticEventUsesFailedItemsJobContext() async throws {
        try await withTemporaryBatchSources(["first.mkv", "second.m2ts"]) { folderURL, sourceURLs, destinationURL in
            let firstPath = sourceURLs[0].path
            let scenario = BatchWorkerScenario(failConversionOnceFor: [firstPath])
            let viewModel = ConversionViewModel { scenario.makeClient() }

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchCompletion(viewModel)
            let failedItemID = try XCTUnwrap(viewModel.batchQueue?.items.first?.id)

            viewModel.retryBatchItem(failedItemID)
            await waitForBatchCompletion(viewModel)

            let artifact = try await viewModel.captureDiagnosticBundle(in: destinationURL)
            let events = try diagnosticEvents(from: artifact.archiveURL)
            let failureEvent = try XCTUnwrap(events.first {
                $0["name"] as? String == "job.failed"
                    && $0["failure_code"] as? String == "synthetic_failure"
            })
            let retryEvent = try XCTUnwrap(events.first {
                $0["name"] as? String == "batch.retry_requested"
            })

            XCTAssertNotNil(failureEvent["job_token"] as? String)
            XCTAssertEqual(
                retryEvent["job_token"] as? String,
                failureEvent["job_token"] as? String
            )
        }
    }

    @MainActor
    func testBatchInspectionFailureContinuesAndRetryRestartsInspection() async throws {
        try await withTemporaryBatchSources(["first.mkv", "second.m2ts"]) { folderURL, sourceURLs, destinationURL in
            let firstPath = sourceURLs[0].path
            let scenario = BatchWorkerScenario(failInspectionOnceFor: [firstPath])
            let viewModel = ConversionViewModel { scenario.makeClient() }

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchCompletion(viewModel)

            var queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.failed, .completed])
            XCTAssertEqual(queue.items[0].failureMessage, "Synthetic inspection failure")

            viewModel.retryBatchItem(queue.items[0].id)
            await waitForBatchCompletion(viewModel)

            queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.completed, .completed])
            XCTAssertEqual(
                scenario.records.filter { $0.sourcePath == firstPath }.map(\.operation),
                ["inspect_source", "inspect_source", "convert_source"]
            )
        }
    }

    @MainActor
    func testBatchRecoveryChoiceRetriesFromStoredDecisionDraft() async throws {
        try await withTemporaryBatchSources(["first.mkv", "second.m2ts"]) { folderURL, sourceURLs, destinationURL in
            let firstPath = sourceURLs[0].path
            let decision = WorkerDecision(
                identifier: "mkv_creation_decision_required",
                prompt: "MakeMKV needs attention.",
                choices: ["retry_continue_on_error", "cancel"],
                details: "A usable MKV was created."
            )
            let scenario = BatchWorkerScenario(decisionConversionOnceFor: [firstPath: decision])
            let viewModel = ConversionViewModel { scenario.makeClient() }

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchCompletion(viewModel)

            var queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.failed, .completed])
            XCTAssertEqual(queue.items[0].recoveryDecision, decision)
            XCTAssertTrue(queue.items[0].canRetry)
            let durableItemID = queue.items[0].id

            viewModel.retryBatchItem(
                queue.items[0].id,
                recoveryChoice: .retryContinueOnError
            )
            await waitForBatchCompletion(viewModel)

            queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.completed, .completed])
            let conversionRecords = scenario.records.filter {
                $0.sourcePath == firstPath && $0.operation == "convert_source"
            }
            XCTAssertEqual(conversionRecords.count, 2)
            XCTAssertEqual(conversionRecords.last?.startStage, ConversionStage.extractMVCAndAudio.rawValue)
            XCTAssertEqual(conversionRecords.last?.continueOnError, true)
            XCTAssertTrue(
                viewModel.restoredDurableQueueItems
                    .first(where: { $0.id == durableItemID })?
                    .attempts
                    .contains(where: { $0.recoveryChoice == WorkerRecoveryChoice.retryContinueOnError.rawValue }) == true
            )
        }
    }

    @MainActor
    func testBatchFailsLaterItemWhenInspectionCreatesOutputNameCollision() async throws {
        try await withTemporaryBatchSources(["first.mkv", "second.m2ts"]) { folderURL, sourceURLs, destinationURL in
            let inspectionNames = Dictionary(uniqueKeysWithValues: sourceURLs.map { ($0.path, "Feature") })
            let scenario = BatchWorkerScenario(inspectionNames: inspectionNames)
            let viewModel = ConversionViewModel { scenario.makeClient() }

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchCompletion(viewModel)

            let queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.completed, .failed])
            XCTAssertFalse(queue.items[1].canRetry)
            XCTAssertEqual(
                queue.items[1].failureMessage,
                "Another queued source resolves to the same output file."
            )
            XCTAssertEqual(scenario.records.map(\.operation), [
                "inspect_source",
                "convert_source",
                "inspect_source",
            ])
        }
    }

    @MainActor
    func testBatchAllowsDistinctDottedInspectionNames() async throws {
        try await withTemporaryBatchSources(["first.mkv", "second.m2ts"]) { folderURL, sourceURLs, destinationURL in
            let scenario = BatchWorkerScenario(
                inspectionNames: [
                    sourceURLs[0].path: "Feature.Part1",
                    sourceURLs[1].path: "Feature.Part2",
                ]
            )
            let viewModel = ConversionViewModel { scenario.makeClient() }

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchCompletion(viewModel)

            let queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.completed, .completed])
            XCTAssertEqual(queue.items.map { $0.draft?.proposedOutputURL.lastPathComponent }, [
                "Feature.Part1_AVP.mov",
                "Feature.Part2_AVP.mov",
            ])
            XCTAssertEqual(scenario.records.map(\.operation), [
                "inspect_source",
                "convert_source",
                "inspect_source",
                "convert_source",
            ])
        }
    }

    @MainActor
    func testBatchISOUsesInspectedMainTitleAndPreservesMultiTitleSource() async throws {
        try await withTemporaryBatchSources(["Feature.iso"]) { folderURL, sourceURLs, destinationURL in
            let mainTitle = SourceTitle(
                id: "makemkv:0",
                name: "Main Movie",
                outputName: "Feature",
                durationSeconds: 7_200,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: true
            )
            let extraTitle = SourceTitle(
                id: "makemkv:2",
                name: "3D Video 1",
                outputName: "Feature - 3D Video 1",
                durationSeconds: 600,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: false
            )
            let inspection = SourceInspection(
                name: "Feature",
                resolution: "1920x1080",
                frameRate: "24000/1001",
                interlaced: false,
                titles: [mainTitle, extraTitle]
            )
            let scenario = BatchWorkerScenario(
                inspectionResults: [sourceURLs[0].path: [inspection]]
            )
            let viewModel = ConversionViewModel { scenario.makeClient() }
            var options = ConversionOptions()
            options.job.removeOriginalAfterSuccess = true

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: options
            )
            await waitForBatchCompletion(viewModel)

            let queue = try XCTUnwrap(viewModel.batchQueue)
            let conversionRecord = try XCTUnwrap(
                scenario.records.first(where: { $0.operation == "convert_source" })
            )
            XCTAssertEqual(queue.items.map(\.status), [.completed])
            XCTAssertEqual(queue.items[0].draft?.selectedTitle, mainTitle)
            XCTAssertEqual(conversionRecord.titleID, mainTitle.id)
            XCTAssertEqual(conversionRecord.removeOriginal, false)
        }
    }

    @MainActor
    func testBatchAllTitlesConvertsEveryDiscTitleBeforeNextSource() async throws {
        try await withTemporaryBatchSources(["Feature.iso", "next.mkv"]) { folderURL, sourceURLs, destinationURL in
            let mainTitle = SourceTitle(
                id: "makemkv:0",
                name: "Main Feature",
                outputName: "Feature",
                durationSeconds: 7_200,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: true
            )
            let extraTitle = SourceTitle(
                id: "makemkv:2",
                name: "3D Video 1",
                outputName: "Feature - 3D Video 1",
                durationSeconds: 600,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: false
            )
            let inspection = SourceInspection(
                name: "Feature",
                resolution: "1920x1080",
                frameRate: "24000/1001",
                interlaced: false,
                titles: [mainTitle, extraTitle]
            )
            let scenario = BatchWorkerScenario(
                inspectionResults: [sourceURLs[0].path: [inspection]]
            )
            let viewModel = ConversionViewModel { scenario.makeClient() }
            var options = ConversionOptions()
            options.job.removeOriginalAfterSuccess = true

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: options,
                titleSelection: .all
            )
            await waitForBatchCompletion(viewModel)

            let queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.completed, .completed, .completed])
            XCTAssertEqual(queue.items.map { $0.draft?.selectedTitle?.id }, [mainTitle.id, extraTitle.id, nil])
            XCTAssertEqual(queue.items.map(\.displayName), [
                "Feature.iso — Main Feature",
                "Feature.iso — 3D Video 1",
                "next.mkv",
            ])
            XCTAssertEqual(
                scenario.records.map {
                    "\($0.operation):\(URL(fileURLWithPath: $0.sourcePath).lastPathComponent):\($0.titleID ?? "none")"
                },
                [
                    "inspect_source:Feature.iso:none",
                    "convert_source:Feature.iso:makemkv:0",
                    "convert_source:Feature.iso:makemkv:2",
                    "inspect_source:next.mkv:none",
                    "convert_source:next.mkv:none",
                ]
            )
            let conversionRecords = scenario.records.filter { $0.operation == "convert_source" }
            XCTAssertEqual(conversionRecords.map(\.removeOriginal), [false, false, true])
            XCTAssertEqual(
                viewModel.restoredDurableQueueItems.map(\.ordinal),
                Array(viewModel.restoredDurableQueueItems.indices)
            )
        }
    }

    @MainActor
    func testBatchAllTitlesRetryDoesNotFanOutAgain() async throws {
        try await withTemporaryBatchSources(["Feature.iso"]) { folderURL, sourceURLs, destinationURL in
            let originalTitles = [
                SourceTitle(
                    id: "makemkv:0",
                    name: "Main Feature",
                    outputName: "Feature",
                    durationSeconds: 7_200,
                    resolution: "1920x1080",
                    frameRate: "24000/1001",
                    mainFeature: true
                ),
                SourceTitle(
                    id: "makemkv:2",
                    name: "3D Video 1",
                    outputName: "Feature - 3D Video 1",
                    durationSeconds: 600,
                    resolution: "1920x1080",
                    frameRate: "24000/1001",
                    mainFeature: false
                ),
            ]
            let refreshedTitles = [
                SourceTitle(
                    id: "makemkv:5",
                    name: "3D Video 1",
                    outputName: "Feature - 3D Video 1",
                    durationSeconds: 600,
                    resolution: "1920x1080",
                    frameRate: "24000/1001",
                    mainFeature: false
                ),
                SourceTitle(
                    id: "makemkv:3",
                    name: "Main Feature",
                    outputName: "Feature",
                    durationSeconds: 7_200,
                    resolution: "1920x1080",
                    frameRate: "24000/1001",
                    mainFeature: true
                ),
            ]
            let originalInspection = SourceInspection(
                name: "Feature",
                resolution: "1920x1080",
                frameRate: "24000/1001",
                interlaced: false,
                titles: originalTitles
            )
            let refreshedInspection = SourceInspection(
                name: "Feature",
                resolution: "1920x1080",
                frameRate: "24000/1001",
                interlaced: false,
                titles: refreshedTitles
            )
            let sourcePath = sourceURLs[0].path
            let scenario = BatchWorkerScenario(
                failConversionOnceFor: [sourcePath],
                inspectionResults: [sourcePath: [originalInspection, refreshedInspection]]
            )
            let viewModel = ConversionViewModel { scenario.makeClient() }

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions(),
                titleSelection: .all
            )
            await waitForBatchCompletion(viewModel)

            var queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.failed, .completed])
            viewModel.retryBatchItem(queue.items[0].id)
            await waitForBatchCompletion(viewModel)

            queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.count, 2)
            XCTAssertEqual(queue.items.map(\.status), [.completed, .completed])
            XCTAssertEqual(
                scenario.records.filter { $0.operation == "convert_source" }.map(\.titleID),
                [originalTitles[0].id, originalTitles[1].id, refreshedTitles[1].id]
            )
        }
    }

    @MainActor
    func testBatchAllTitlesRetryFailsWhenSelectedTitleDisappears() async throws {
        try await withTemporaryBatchSources(["Feature.iso"]) { folderURL, sourceURLs, destinationURL in
            let originalTitle = SourceTitle(
                id: "makemkv:0",
                name: "Main Feature",
                outputName: "Feature",
                durationSeconds: 7_200,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: true
            )
            let unrelatedTitle = SourceTitle(
                id: "makemkv:7",
                name: "Different 3D Video",
                outputName: "Different Feature",
                durationSeconds: 900,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: false
            )
            let inspections = [originalTitle, unrelatedTitle].map { title in
                SourceInspection(
                    name: "Feature",
                    resolution: "1920x1080",
                    frameRate: "24000/1001",
                    interlaced: false,
                    titles: [title]
                )
            }
            let sourcePath = sourceURLs[0].path
            let scenario = BatchWorkerScenario(
                failConversionOnceFor: [sourcePath],
                inspectionResults: [sourcePath: inspections]
            )
            let viewModel = ConversionViewModel { scenario.makeClient() }

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions(),
                titleSelection: .all
            )
            await waitForBatchCompletion(viewModel)

            var queue = try XCTUnwrap(viewModel.batchQueue)
            viewModel.retryBatchItem(queue.items[0].id)
            await waitForBatchCompletion(viewModel)

            queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.failed])
            XCTAssertEqual(queue.items[0].failureMessage, "The previously selected 3D video is no longer available.")
            XCTAssertEqual(scenario.records.filter { $0.operation == "convert_source" }.count, 1)
        }
    }

    @MainActor
    func testBatchAllTitlesRejectsDuplicateGeneratedOutputs() async throws {
        try await withTemporaryBatchSources(["Feature.iso"]) { folderURL, sourceURLs, destinationURL in
            let inspection = SourceInspection(
                name: "Feature",
                resolution: "1920x1080",
                frameRate: "24000/1001",
                interlaced: false,
                titles: [
                    SourceTitle(
                        id: "makemkv:0",
                        name: "Main Feature",
                        outputName: "Feature",
                        durationSeconds: 7_200,
                        resolution: "1920x1080",
                        frameRate: "24000/1001",
                        mainFeature: true
                    ),
                    SourceTitle(
                        id: "makemkv:2",
                        name: "Duplicate Output",
                        outputName: "Feature",
                        durationSeconds: 600,
                        resolution: "1920x1080",
                        frameRate: "24000/1001",
                        mainFeature: false
                    ),
                ]
            )
            let scenario = BatchWorkerScenario(
                inspectionResults: [sourceURLs[0].path: [inspection]]
            )
            let viewModel = ConversionViewModel { scenario.makeClient() }

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions(),
                titleSelection: .all
            )
            await waitForBatchCompletion(viewModel)

            let queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.failed, .failed])
            XCTAssertTrue(queue.items.allSatisfy {
                $0.failureMessage == "Another queued source resolves to the same output file."
            })
            XCTAssertFalse(scenario.records.contains(where: { $0.operation == "convert_source" }))
        }
    }

    @MainActor
    func testBatchAllTitlesRerunRediscoversSourcesInsteadOfProjectedRows() async throws {
        try await withTemporaryBatchSources(["Feature.iso"]) { folderURL, sourceURLs, destinationURL in
            let inspection = SourceInspection(
                name: "Feature",
                resolution: "1920x1080",
                frameRate: "24000/1001",
                interlaced: false,
                titles: [
                    SourceTitle(
                        id: "makemkv:0",
                        name: "Main Feature",
                        outputName: "Feature",
                        durationSeconds: 7_200,
                        resolution: "1920x1080",
                        frameRate: "24000/1001",
                        mainFeature: true
                    ),
                    SourceTitle(
                        id: "makemkv:2",
                        name: "3D Video 1",
                        outputName: "Feature - 3D Video 1",
                        durationSeconds: 600,
                        resolution: "1920x1080",
                        frameRate: "24000/1001",
                        mainFeature: false
                    ),
                ]
            )
            let scenario = BatchWorkerScenario(
                inspectionResults: [sourceURLs[0].path: [inspection, inspection]]
            )
            let viewModel = ConversionViewModel { scenario.makeClient() }

            viewModel.selectSource(folderURL)
            for _ in 0 ..< 2 {
                viewModel.startBatchConversion(
                    profile: BuiltInProfile.balanced.profile,
                    destinationURL: destinationURL,
                    options: ConversionOptions(),
                    titleSelection: .all
                )
                await waitForBatchCompletion(viewModel)
                XCTAssertEqual(viewModel.batchQueue?.items.count, 2)
            }

            XCTAssertEqual(scenario.records.filter { $0.operation == "inspect_source" }.count, 2)
            XCTAssertEqual(scenario.records.filter { $0.operation == "convert_source" }.count, 4)
        }
    }

    @MainActor
    func testBatchSingleTitleSourcePreservesRemoveOriginalIntent() async throws {
        try await withTemporaryBatchSources(["feature.mkv"]) { folderURL, _, destinationURL in
            let scenario = BatchWorkerScenario()
            let viewModel = ConversionViewModel { scenario.makeClient() }
            var options = ConversionOptions()
            options.job.removeOriginalAfterSuccess = true

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: options
            )
            await waitForBatchCompletion(viewModel)

            let conversionRecord = try XCTUnwrap(
                scenario.records.first(where: { $0.operation == "convert_source" })
            )
            XCTAssertEqual(conversionRecord.removeOriginal, true)
            XCTAssertTrue(
                try XCTUnwrap(viewModel.batchQueue?.items.first?.draft)
                    .options.job.removeOriginalAfterSuccess
            )
        }
    }

    @MainActor
    func testBatchISOWithoutTitlesFailsItemAndContinues() async throws {
        try await withTemporaryBatchSources(["Feature.iso", "next.mkv"]) { folderURL, sourceURLs, destinationURL in
            let emptyInspection = SourceInspection(
                name: "Feature",
                resolution: "1920x1080",
                frameRate: "24000/1001",
                interlaced: false
            )
            let scenario = BatchWorkerScenario(
                inspectionResults: [sourceURLs[0].path: [emptyInspection]]
            )
            let viewModel = ConversionViewModel { scenario.makeClient() }

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchCompletion(viewModel)

            let queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.failed, .completed])
            XCTAssertTrue(queue.items[0].canRetry)
            XCTAssertEqual(queue.items[0].failureMessage, "No convertible 3D title was found in this source.")
            XCTAssertEqual(
                scenario.records.map { "\($0.operation):\(URL(fileURLWithPath: $0.sourcePath).lastPathComponent)" },
                [
                    "inspect_source:Feature.iso",
                    "inspect_source:next.mkv",
                    "convert_source:next.mkv",
                ]
            )
        }
    }

    @MainActor
    func testBatchISORetryUsesFreshlyInspectedTitleID() async throws {
        try await withTemporaryBatchSources(["Feature.iso"]) { folderURL, sourceURLs, destinationURL in
            let originalTitle = SourceTitle(
                id: "makemkv:0",
                name: "Main Movie",
                outputName: "Feature",
                durationSeconds: 7_200,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: true
            )
            let refreshedTitle = SourceTitle(
                id: "makemkv:3",
                name: "Main Movie",
                outputName: "Feature",
                durationSeconds: 7_200,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: true
            )
            let inspectionResults = [originalTitle, refreshedTitle].map { title in
                SourceInspection(
                    name: "Feature",
                    resolution: "1920x1080",
                    frameRate: "24000/1001",
                    interlaced: false,
                    titles: [title]
                )
            }
            let sourcePath = sourceURLs[0].path
            let scenario = BatchWorkerScenario(
                failConversionOnceFor: [sourcePath],
                inspectionResults: [sourcePath: inspectionResults]
            )
            let viewModel = ConversionViewModel { scenario.makeClient() }

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchCompletion(viewModel)

            var queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.failed])
            viewModel.retryBatchItem(queue.items[0].id)
            await waitForBatchCompletion(viewModel)

            queue = try XCTUnwrap(viewModel.batchQueue)
            let conversionRecords = scenario.records.filter { $0.operation == "convert_source" }
            XCTAssertEqual(queue.items.map(\.status), [.completed])
            XCTAssertEqual(conversionRecords.map(\.titleID), [originalTitle.id, refreshedTitle.id])
            XCTAssertEqual(scenario.records.filter { $0.operation == "inspect_source" }.count, 2)
        }
    }

    @MainActor
    func testBatchFactoryFailuresAdvanceWithoutRecursiveWorkerLaunches() async throws {
        let sourceNames = (0..<40).map { String(format: "source-%03d.mkv", $0) }
        try await withTemporaryBatchSources(sourceNames) { folderURL, _, destinationURL in
            let viewModel = ConversionViewModel { throw BatchFactoryError() }

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchCompletion(viewModel)

            let queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.failedCount, sourceNames.count)
            XCTAssertTrue(queue.items.allSatisfy { $0.status == .failed })
            XCTAssertTrue(queue.items.allSatisfy(\.canRetry))
        }
    }

    @MainActor
    func testStoppingDuringDurableBatchAdmissionPreventsWorkerLaunch() async throws {
        try await withTemporaryBatchSources(["first.mkv", "second.m2ts"]) { folderURL, _, destinationURL in
            var factoryCalls = 0
            let viewModel = ConversionViewModel {
                factoryCalls += 1
                throw BatchFactoryError()
            }

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )

            XCTAssertFalse(viewModel.hasActiveWorker)
            XCTAssertTrue(viewModel.hasActiveWork)
            viewModel.stopActiveWorker()
            await viewModel.stopForQuit()

            let queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.notStarted, .notStarted])
            XCTAssertEqual(factoryCalls, 0)
            XCTAssertFalse(viewModel.hasActiveWork)
        }
    }

    @MainActor
    func testStoppingBatchStopsActiveItemAndLeavesRemainingItemsNotStarted() async throws {
        try await withTemporaryBatchSources(["first.mkv", "second.m2ts"]) { folderURL, sourceURLs, destinationURL in
            let firstPath = sourceURLs[0].path
            let scenario = BatchWorkerScenario(holdConversionForCancellation: [firstPath])
            let viewModel = ConversionViewModel { scenario.makeClient() }

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchStatus(viewModel, status: .converting)

            var installCount = 0
            XCTAssertTrue(
                viewModel.postponeInstallUntilIdle {
                    installCount += 1
                }
            )

            viewModel.stopActiveWorker()
            XCTAssertEqual(viewModel.batchQueue?.items.map(\.status), [.stopping, .notStarted])
            await waitForBatchCompletion(viewModel)

            let queue = try XCTUnwrap(viewModel.batchQueue)
            XCTAssertEqual(queue.items.map(\.status), [.stopped, .notStarted])
            XCTAssertTrue(queue.stopRequested)
            XCTAssertEqual(queue.completedCount, 0)
            XCTAssertEqual(queue.stoppedCount, 1)
            XCTAssertEqual(queue.notStartedCount, 1)
            XCTAssertEqual(scenario.records.map(\.operation), ["inspect_source", "convert_source"])
            XCTAssertEqual(scenario.maximumActiveCount, 1)
            XCTAssertEqual(installCount, 1)
        }
    }

    @MainActor
    func testLateStopPreservesAlreadyDeliveredBatchCompletion() async throws {
        try await withTemporaryBatchSources(["feature.mkv"]) { folderURL, sourceURLs, destinationURL in
            let terminalDelivered = expectation(description: "conversion terminal delivered")
            let scenario = BatchWorkerScenario(
                holdCompletedConversionForCancellation: [sourceURLs[0].path],
                onCompletedConversionDelivered: { terminalDelivered.fulfill() }
            )
            let queueStore = ConversionQueueStore.inMemory()
            let viewModel = ConversionViewModel(clientFactory: { scenario.makeClient() }, durableQueueStore: queueStore)

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await fulfillment(of: [terminalDelivered], timeout: 2)

            viewModel.stopActiveWorker()
            await waitForBatchCompletion(viewModel)

            XCTAssertEqual(queueStore.items.map(\.state), [.completed])
            XCTAssertNotNil(queueStore.items.first?.result)
            XCTAssertEqual(viewModel.batchQueue?.items.map(\.status), [.completed])
        }
    }

    @MainActor
    private func waitForBatchCompletion(_ viewModel: ConversionViewModel) async {
        await viewModel.waitForBatchQueueSettled()
        XCTAssertNotNil(viewModel.batchQueue?.completionID)
        XCTAssertFalse(viewModel.hasActiveWork)
    }

    @MainActor
    private func waitForBatchStatus(
        _ viewModel: ConversionViewModel,
        status: SourceFolderQueueItemStatus
    ) async {
        let deadline = Date().addingTimeInterval(5)
        while Date() < deadline {
            if viewModel.batchQueue?.activeItem?.status == status {
                return
            }
            await Task.yield()
        }
        XCTFail("Timed out waiting for batch status \(status)")
    }

    private func withTemporaryBatchSources(
        _ names: [String],
        operation: @MainActor (URL, [URL], URL) async throws -> Void
    ) async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let sourceURLs = names.map { directoryURL.appendingPathComponent($0) }.sorted {
            $0.path.lowercased() < $1.path.lowercased()
        }
        for sourceURL in sourceURLs {
            _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8))
        }
        let destinationURL = directoryURL.appendingPathComponent("Output", isDirectory: true)
        try FileManager.default.createDirectory(at: destinationURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try await operation(directoryURL, sourceURLs, destinationURL)
    }

    @MainActor
    func testMixedSourceQueuePreservesRemovalIntentPerSource() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURLs = ["Feature A.iso", "Feature B.iso"].map {
            directoryURL.appendingPathComponent($0)
        }
        for sourceURL in sourceURLs {
            _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        }
        let conversionsDone = expectation(description: "mixed-source conversions done")
        conversionsDone.expectedFulfillmentCount = 4
        var conversionJobs: [WorkerJobSpec] = []
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { job in
            conversionJobs.append(job)
            conversionsDone.fulfill()
        })
        let viewModel = ConversionViewModel { worker }
        var options = ConversionOptions()
        options.job.removeOriginalAfterSuccess = true
        let drafts = sourceURLs.flatMap { sourceURL in
            let source = ConversionSource(kind: .discImage, url: sourceURL)
            let titles = [
                SourceTitle(id: "title-1", name: "One", outputName: "One", durationSeconds: 100, resolution: "1920x1080", frameRate: "24/1", mainFeature: true),
                SourceTitle(id: "title-2", name: "Two", outputName: "Two", durationSeconds: 90, resolution: "1920x1080", frameRate: "24/1", mainFeature: false),
            ]
            let inspection = SourceInspection(
                name: source.displayName,
                resolution: "1920x1080",
                frameRate: "24/1",
                interlaced: false,
                titles: titles
            )
            return titles.map { title in
                ConversionDraft(
                    source: source,
                    sourceDetails: inspection,
                    profile: BuiltInProfile.balanced.profile,
                    destinationURL: directoryURL,
                    options: options,
                    selectedTitle: title
                )
            }
        }

        viewModel.startConversionQueue(drafts: drafts)
        await fulfillment(of: [conversionsDone], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(conversionJobs.map(\.source.path), [
            sourceURLs[0].path,
            sourceURLs[0].path,
            sourceURLs[1].path,
            sourceURLs[1].path,
        ])
        XCTAssertEqual(conversionJobs.map { $0.job?.removeOriginal }, [false, true, false, true])
        XCTAssertEqual(
            viewModel.restoredDurableQueueItems.map { $0.intent.options.job.removeOriginalAfterSuccess },
            [false, true, false, true]
        )
    }

    @MainActor
    func testMultiTitleQueueRunsSeriallyAndPreservesSourceUntilFinalJob() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let sourceURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        defer { try? FileManager.default.removeItem(at: directoryURL) }

        let inspectionDone = expectation(description: "inspection done")
        let conversionsDone = expectation(description: "queued conversions done")
        conversionsDone.expectedFulfillmentCount = 2
        var conversionJobs: [WorkerJobSpec] = []
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { job in
                conversionJobs.append(job)
                conversionsDone.fulfill()
            }
        )
        let viewModel = ConversionViewModel { worker }
        let source = ConversionSource(kind: .discImage, url: sourceURL)

        viewModel.selectSource(source)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }

        var options = ConversionOptions()
        options.job.removeOriginalAfterSuccess = true
        let titles = [
            SourceTitle(
                id: "makemkv:0",
                name: "Main Movie",
                outputName: "Feature",
                durationSeconds: 7_200,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: true
            ),
            SourceTitle(
                id: "makemkv:2",
                name: "3D Video 1",
                outputName: "Feature - 3D Video 1",
                durationSeconds: 600,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: false
            ),
        ]
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24000/1001",
            interlaced: false,
            titles: titles
        )
        let drafts = titles.map { title in
            ConversionDraft(
                source: source,
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: options,
                selectedTitle: title
            )
        }

        viewModel.startConversionQueue(drafts: drafts)

        await fulfillment(of: [conversionsDone], timeout: 2)
        while viewModel.hasActiveWorker || viewModel.hasQueuedWork { await Task.yield() }

        XCTAssertEqual(conversionJobs.map(\.source.titleID), ["makemkv:0", "makemkv:2"])
        XCTAssertFalse(try XCTUnwrap(conversionJobs.first?.job).removeOriginal)
        XCTAssertTrue(try XCTUnwrap(conversionJobs.last?.job).removeOriginal)
        XCTAssertEqual(viewModel.completedBatchResults?.count, 2)
        XCTAssertTrue(
            viewModel.queueItems.allSatisfy { item in
                if case .completed = item.status { return true }
                return false
            }
        )
    }

    @MainActor
    func testMultiTitleQueueRoutesPendingReorderAndCompletionByStableItemID() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let sourceURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        defer { try? FileManager.default.removeItem(at: directoryURL) }

        let inspectionDone = expectation(description: "inspection done")
        let firstConversionStarted = expectation(description: "first conversion started")
        let remainingConversionsDone = expectation(description: "remaining conversions done")
        remainingConversionsDone.expectedFulfillmentCount = 2
        let firstConversionGate = QueueTestGate()
        var conversionTitleIDs: [String] = []
        var removeOriginalValues: [Bool] = []
        let worker = ReorderableQueueWorkerClient(
            inspectionDone: { inspectionDone.fulfill() },
            conversionStarted: { job, conversionNumber in
                conversionTitleIDs.append(job.source.titleID ?? "")
                removeOriginalValues.append(job.job?.removeOriginal ?? false)
                if conversionNumber == 1 {
                    firstConversionStarted.fulfill()
                } else {
                    remainingConversionsDone.fulfill()
                }
            },
            conversionGate: firstConversionGate
        )
        let viewModel = ConversionViewModel { worker }
        let source = ConversionSource(kind: .discImage, url: sourceURL)
        let titles = [
            SourceTitle(id: "title-1", name: "One", outputName: "One", durationSeconds: 100, resolution: "1920x1080", frameRate: "24/1", mainFeature: true),
            SourceTitle(id: "title-2", name: "Two", outputName: "Two", durationSeconds: 90, resolution: "1920x1080", frameRate: "24/1", mainFeature: false),
            SourceTitle(id: "title-3", name: "Three", outputName: "Three", durationSeconds: 80, resolution: "1920x1080", frameRate: "24/1", mainFeature: false),
        ]
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24/1",
            interlaced: false,
            titles: titles
        )
        var options = ConversionOptions()
        options.job.removeOriginalAfterSuccess = true
        let drafts = titles.map { title in
            ConversionDraft(
                source: source,
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: options,
                selectedTitle: title
            )
        }

        viewModel.selectSource(source)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        viewModel.startConversionQueue(drafts: drafts)
        await fulfillment(of: [firstConversionStarted], timeout: 2)
        XCTAssertNil(viewModel.completedBatchResults)
        let secondID = viewModel.queueItems[1].id
        let thirdID = viewModel.queueItems[2].id

        XCTAssertTrue(viewModel.moveWaitingQueueItem(thirdID, before: secondID))
        while viewModel.queueItems.map({ $0.draft.selectedTitle?.id }) != ["title-1", "title-3", "title-2"] {
            await Task.yield()
        }
        XCTAssertEqual(
            viewModel.queueItems.map { $0.draft.selectedTitle?.id },
            ["title-1", "title-3", "title-2"]
        )
        await firstConversionGate.open()
        await fulfillment(of: [remainingConversionsDone], timeout: 2)
        while viewModel.hasActiveWorker || viewModel.hasQueuedWork { await Task.yield() }

        XCTAssertEqual(conversionTitleIDs, ["title-1", "title-3", "title-2"])
        XCTAssertEqual(removeOriginalValues, [false, false, true])
        XCTAssertTrue(viewModel.queueItems.allSatisfy {
            if case .completed = $0.status { return true }
            return false
        })
        XCTAssertEqual(viewModel.restoredDurableQueueItems.map(\.state), [
            .completed,
            .completed,
            .completed,
        ])
        XCTAssertEqual(viewModel.restoredDurableQueueItems.map(\.ordinal), [0, 1, 2])
        XCTAssertEqual(
            viewModel.restoredDurableQueueItems.map { $0.intent.options.job.removeOriginalAfterSuccess },
            [false, false, true]
        )
    }

    @MainActor
    func testCompletedResultsPublishOnlyAfterFinalQueuedTitle() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        let inspectionDone = expectation(description: "inspection done")
        let secondConversionStarted = expectation(description: "second conversion started")
        let secondConversionGate = QueueTestGate()
        let worker = ReorderableQueueWorkerClient(
            inspectionDone: { inspectionDone.fulfill() },
            conversionStarted: { _, conversionNumber in
                if conversionNumber == 2 {
                    secondConversionStarted.fulfill()
                }
            },
            blockedConversionNumber: 2,
            conversionGate: secondConversionGate
        )
        let viewModel = ConversionViewModel { worker }
        let source = ConversionSource(kind: .discImage, url: sourceURL)

        viewModel.selectSource(source)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        viewModel.startConversionQueue(drafts: makeDiscQueueDrafts(source: source, destinationURL: directoryURL))
        await fulfillment(of: [secondConversionStarted], timeout: 2)

        XCTAssertNil(viewModel.completedBatchResults)

        await secondConversionGate.open()
        while viewModel.hasActiveWork { await Task.yield() }
        XCTAssertEqual(viewModel.completedBatchResults?.count, 2)
    }

    @MainActor
    func testCompletedTitleResultsSurviveHistoryCompaction() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        let source = ConversionSource(kind: .discImage, url: sourceURL)
        let drafts = makeDiscQueueDrafts(source: source, destinationURL: directoryURL)
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems(makeCompletedHistoryItems(count: 5, draft: drafts[0]))
        let inspectionDone = expectation(description: "inspection done")
        let worker = TwoPhaseWorkerClient(onInspectionComplete: { inspectionDone.fulfill() })
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            durableQueueStore: queueStore
        )

        viewModel.selectSource(source)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        viewModel.startConversionQueue(drafts: drafts)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(viewModel.completedBatchResults?.count, 2)
        XCTAssertEqual(viewModel.queueItems.count, 2)
        XCTAssertTrue(viewModel.queueItems.allSatisfy {
            if case .completed = $0.status { return true }
            return false
        })
        XCTAssertEqual(queueStore.items.filter { $0.origin == .multiTitle }.map(\.state), [.completed, .completed])
        XCTAssertEqual(queueStore.items.count, 6)
        XCTAssertEqual(queueStore.items.map(\.ordinal), Array(queueStore.items.indices))
    }

    @MainActor
    func testReorderWriteFailurePreservesActiveWorkerAndDurableGroup() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        let queueURL = directoryURL.appendingPathComponent("queue.json")
        let writeCount = RuntimePersistenceTestCounter()
        let queueStore = ConversionQueueStore(
            fileURL: queueURL,
            dataWriter: { data, url in
                if writeCount.incrementAndReturn() == 3 {
                    throw RuntimePersistenceTestError.writeFailed
                }
                try data.write(to: url, options: .atomic)
            }
        )
        let inspectionDone = expectation(description: "inspection done")
        let firstConversionStarted = expectation(description: "first conversion started")
        let conversionGate = QueueTestGate()
        let worker = ReorderableQueueWorkerClient(
            inspectionDone: { inspectionDone.fulfill() },
            conversionStarted: { _, conversionNumber in
                if conversionNumber == 1 {
                    firstConversionStarted.fulfill()
                }
            },
            blockedConversionNumber: 1,
            conversionGate: conversionGate
        )
        let viewModel = ConversionViewModel(clientFactory: { worker }, durableQueueStore: queueStore)
        let source = ConversionSource(kind: .discImage, url: sourceURL)

        viewModel.selectSource(source)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        let drafts = makeDiscQueueDrafts(source: source, destinationURL: directoryURL) + [
            makeDiscQueueDrafts(source: source, destinationURL: directoryURL)[0],
        ]
        viewModel.startConversionQueue(drafts: drafts)
        await fulfillment(of: [firstConversionStarted], timeout: 2)
        let secondID = viewModel.queueItems[1].id
        let thirdID = viewModel.queueItems[2].id

        XCTAssertTrue(viewModel.moveWaitingQueueItem(thirdID, before: secondID))
        while viewModel.durableQueueRuntimeDiagnostic == nil { await Task.yield() }

        XCTAssertTrue(viewModel.hasActiveWorker)
        XCTAssertEqual(queueStore.items.map(\.state), [.processing, .waiting, .waiting])

        await conversionGate.open()
        while viewModel.hasActiveWork { await Task.yield() }
        XCTAssertEqual(queueStore.items.map(\.state), [.completed, .completed, .completed])
    }

    @MainActor
    func testDurableTitleQueueWritesProcessingBeforeCreatingConversionClient() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        let queueURL = directoryURL.appendingPathComponent("queue.json")
        let queueStore = ConversionQueueStore(fileURL: queueURL)
        let inspectionDone = expectation(description: "inspection done")
        let conversionDone = expectation(description: "conversion done")
        conversionDone.expectedFulfillmentCount = 2
        var processingWasPersistedBeforeSpawn = false
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { _ in conversionDone.fulfill() }
        )
        let viewModel = ConversionViewModel(
            clientFactory: {
                if let data = try? Data(contentsOf: queueURL) {
                    let decoder = JSONDecoder()
                    decoder.dateDecodingStrategy = .iso8601
                    decoder.userInfo[.requiresVideoQualityIntent] = true
                    if let document = try? decoder.decode(ConversionQueueDocument.self, from: data),
                       document.items.contains(where: { $0.origin == .multiTitle && $0.state == .processing })
                    {
                        processingWasPersistedBeforeSpawn = true
                    }
                }
                return worker
            },
            durableQueueStore: queueStore
        )
        let source = ConversionSource(kind: .discImage, url: sourceURL)

        viewModel.selectSource(source)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        viewModel.startConversionQueue(drafts: makeDiscQueueDrafts(source: source, destinationURL: directoryURL))
        await fulfillment(of: [conversionDone], timeout: 2)
        while viewModel.hasActiveWorker || viewModel.hasQueuedWork { await Task.yield() }

        XCTAssertTrue(processingWasPersistedBeforeSpawn)
        XCTAssertTrue(queueStore.items.allSatisfy { $0.state == .completed })
        XCTAssertTrue(queueStore.items.allSatisfy { $0.attempts.count == 1 && $0.attempts[0].endedAt != nil })
    }

    @MainActor
    func testDurableSourceFolderQueueWritesInspectingBeforeCreatingWorker() async throws {
        try await withTemporaryBatchSources(["feature.mkv"]) { folderURL, _, destinationURL in
            let queueURL = folderURL.appendingPathComponent("queue.json")
            let queueStore = ConversionQueueStore(fileURL: queueURL)
            let scenario = BatchWorkerScenario()
            var inspectingWasPersistedBeforeSpawn = false
            var processingWasPersistedBeforeConversionSpawn = false
            var factoryCalls = 0
            let viewModel = ConversionViewModel(
                clientFactory: {
                    factoryCalls += 1
                    if let data = try? Data(contentsOf: queueURL) {
                        let decoder = JSONDecoder()
                        decoder.dateDecodingStrategy = .iso8601
                        decoder.userInfo[.requiresVideoQualityIntent] = true
                        let document = try? decoder.decode(ConversionQueueDocument.self, from: data)
                        inspectingWasPersistedBeforeSpawn = inspectingWasPersistedBeforeSpawn || document?.items.contains {
                            $0.origin == .sourceFolder && $0.state == .inspecting && !$0.attempts.isEmpty
                        } == true
                        processingWasPersistedBeforeConversionSpawn = processingWasPersistedBeforeConversionSpawn
                            || (factoryCalls > 1 && document?.items.contains {
                                $0.origin == .sourceFolder
                                    && $0.state == .processing
                                    && $0.inspection != nil
                                    && $0.attempts.count == 2
                            } == true)
                    }
                    return scenario.makeClient()
                },
                durableQueueStore: queueStore
            )

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchCompletion(viewModel)

            XCTAssertTrue(inspectingWasPersistedBeforeSpawn)
            XCTAssertTrue(processingWasPersistedBeforeConversionSpawn)
            XCTAssertEqual(queueStore.items.map(\.state), [.completed])
            XCTAssertEqual(queueStore.items[0].attempts.count, 2)
            XCTAssertTrue(queueStore.items.allSatisfy { $0.attempts.allSatisfy { $0.endedAt != nil } })
        }
    }

    @MainActor
    func testDurableSourceFolderQueueFailsClosedWhenStartWritesAreBlocked() async throws {
        try await withTemporaryBatchSources(["feature.mkv"]) { folderURL, _, destinationURL in
            let queueURL = folderURL.appendingPathComponent("queue.json")
            try Data("{\"version\":\(ConversionQueueDocument.currentVersion + 1),\"items\":[]}".utf8).write(to: queueURL)
            let queueStore = ConversionQueueStore(fileURL: queueURL)
            var factoryCalls = 0
            let viewModel = ConversionViewModel(
                clientFactory: {
                    factoryCalls += 1
                    return BatchWorkerScenario().makeClient()
                },
                durableQueueStore: queueStore
            )

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )

            XCTAssertEqual(factoryCalls, 0)
            XCTAssertNotNil(viewModel.durableQueueRuntimeDiagnostic)
            XCTAssertTrue(queueStore.items.isEmpty)
            XCTAssertFalse(viewModel.batchQueue?.hasStarted == true)
        }
    }

    @MainActor
    func testStoppingDuringDurableInspectionWritePreventsWorkerSpawn() async throws {
        try await withTemporaryBatchSources(["first.mkv", "second.m2ts"]) { folderURL, _, destinationURL in
            let queueURL = folderURL.appendingPathComponent("queue.json")
            let writeGate = RuntimePersistenceWriteGate(blockedWriteNumber: 2)
            let queueStore = ConversionQueueStore(
                fileURL: queueURL,
                dataWriter: { data, url in
                    writeGate.writeStarted()
                    try data.write(to: url, options: .atomic)
                }
            )
            var factoryCalls = 0
            let viewModel = ConversionViewModel(
                clientFactory: {
                    factoryCalls += 1
                    return BatchWorkerScenario().makeClient()
                },
                durableQueueStore: queueStore
            )

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            while !writeGate.isBlockedWriteWaiting { await Task.yield() }

            viewModel.stopActiveWorker()
            writeGate.releaseBlockedWrite()
            await viewModel.waitForBatchQueueSettled()

            XCTAssertEqual(factoryCalls, 0)
            XCTAssertEqual(queueStore.items.map(\.state), [.stopped, .notStarted])
            XCTAssertNotNil(queueStore.items.first?.attempts.last?.endedAt)
        }
    }

    @MainActor
    func testStoppingDuringDurableProcessingWritePreventsConversionSpawn() async throws {
        try await withTemporaryBatchSources(["feature.mkv"]) { folderURL, _, destinationURL in
            let queueURL = folderURL.appendingPathComponent("queue.json")
            let writeGate = RuntimePersistenceWriteGate(blockedWriteNumber: 3)
            let queueStore = ConversionQueueStore(
                fileURL: queueURL,
                dataWriter: { data, url in
                    writeGate.writeStarted()
                    try data.write(to: url, options: .atomic)
                }
            )
            let scenario = BatchWorkerScenario()
            var factoryCalls = 0
            let viewModel = ConversionViewModel(
                clientFactory: {
                    factoryCalls += 1
                    return scenario.makeClient()
                },
                durableQueueStore: queueStore
            )

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            while !writeGate.isBlockedWriteWaiting { await Task.yield() }

            viewModel.stopActiveWorker()
            writeGate.releaseBlockedWrite()
            await viewModel.waitForBatchQueueSettled()

            XCTAssertEqual(factoryCalls, 1)
            XCTAssertEqual(queueStore.items.map(\.state), [.stopped])
            XCTAssertEqual(queueStore.items.first?.attempts.count, 2)
            XCTAssertTrue(queueStore.items.first?.attempts.allSatisfy { $0.endedAt != nil } == true)
        }
    }

    @MainActor
    func testStoppingDuringDurableRetryWritePreventsWorkerSpawn() async throws {
        try await withTemporaryBatchSources(["feature.mkv"]) { folderURL, sourceURLs, destinationURL in
            let queueURL = folderURL.appendingPathComponent("queue.json")
            let writeGate = RuntimePersistenceWriteGate(blockedWriteNumber: 5)
            let queueStore = ConversionQueueStore(
                fileURL: queueURL,
                dataWriter: { data, url in
                    writeGate.writeStarted()
                    try data.write(to: url, options: .atomic)
                }
            )
            let scenario = BatchWorkerScenario(failConversionOnceFor: [sourceURLs[0].path])
            let viewModel = ConversionViewModel(
                clientFactory: { scenario.makeClient() },
                durableQueueStore: queueStore
            )

            viewModel.selectSource(folderURL)
            viewModel.startBatchConversion(
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions()
            )
            await waitForBatchCompletion(viewModel)
            let failedItemID = try XCTUnwrap(viewModel.batchQueue?.items.first?.id)
            XCTAssertEqual(scenario.clientCount, 2)

            viewModel.retryBatchItem(failedItemID)
            while !writeGate.isBlockedWriteWaiting { await Task.yield() }
            viewModel.stopActiveWorker()
            writeGate.releaseBlockedWrite()
            await viewModel.waitForBatchQueueSettled()

            XCTAssertEqual(scenario.clientCount, 2)
            XCTAssertEqual(queueStore.items.map(\.state), [.notStarted])
            XCTAssertTrue(queueStore.items.first?.attempts.allSatisfy { $0.endedAt != nil } == true)
            XCTAssertNil(viewModel.durableQueueRuntimeDiagnostic)
        }
    }

    @MainActor
    func testRestoredSourceFolderItemsRemainInert() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("feature.mkv")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8))
        let queueURL = directoryURL.appendingPathComponent("queue.json")
        let source = ConversionSource(kind: .matroska, url: sourceURL)
        let draft = ConversionDraft(
            source: source,
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: directoryURL,
            options: ConversionOptions()
        )
        let writer = ConversionQueueStore(fileURL: queueURL)
        try await writer.replaceItems([
            DurableConversionQueueItem(
                ordinal: 0,
                groupID: UUID(),
                origin: .sourceFolder,
                intent: DurableQueueItemIntent(draft: draft),
                state: .processing,
                attempts: [DurableQueueAttempt(startedAt: Date())]
            ),
        ])

        var factoryCalls = 0
        let restoredStore = ConversionQueueStore(fileURL: queueURL)
        _ = ConversionViewModel(
            clientFactory: {
                factoryCalls += 1
                return BatchWorkerScenario().makeClient()
            },
            durableQueueStore: restoredStore
        )

        XCTAssertEqual(factoryCalls, 0)
        XCTAssertEqual(restoredStore.items.map(\.state), [.interrupted])
    }

    @MainActor
    func testRestoredWaitingRowsNeverAutoRunAndUnavailableAdoptionDoesNotMutate() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let item = makePersistentQueueFixture(ordinal: 0, origin: .singleSource, state: .waiting)
        try await queueStore.replaceItems([item])
        var factoryCalls = 0
        let viewModel = ConversionViewModel(
            clientFactory: {
                factoryCalls += 1
                return TwoPhaseWorkerClient()
            },
            durableQueueStore: queueStore,
            sourceAvailabilityResolver: { _ in false }
        )

        await Task.yield()
        XCTAssertEqual(factoryCalls, 0)
        let adopted = await viewModel.adoptPersistentQueueItem(item.id)
        XCTAssertFalse(adopted)
        XCTAssertEqual(queueStore.items, [item])
        XCTAssertEqual(factoryCalls, 0)
    }

    @MainActor
    func testAdoptionPumpSkipsLowerOrdinalUnadoptedWaitingRow() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let lowerURL = directoryURL.appendingPathComponent("lower.mkv")
        let adoptedURL = directoryURL.appendingPathComponent("adopted.mkv")
        _ = FileManager.default.createFile(atPath: lowerURL.path, contents: Data("video".utf8))
        _ = FileManager.default.createFile(atPath: adoptedURL.path, contents: Data("video".utf8))
        let lowerDraft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: lowerURL),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: directoryURL,
            options: ConversionOptions()
        )
        let adoptedDraft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: adoptedURL),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: directoryURL,
            options: ConversionOptions()
        )
        let lower = DurableConversionQueueItem(ordinal: 0, origin: .singleSource, intent: DurableQueueItemIntent(draft: lowerDraft))
        let adopted = DurableConversionQueueItem(
            ordinal: 1,
            origin: .singleSource,
            intent: DurableQueueItemIntent(draft: adoptedDraft),
            state: .failed,
            failure: DurableQueueFailure(code: "temporary", message: "Temporary", details: nil, retryable: true)
        )
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([lower, adopted])
        let completed = expectation(description: "adopted completed")
        var conversionPaths: [String] = []
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { job in
            conversionPaths.append(job.source.path)
            completed.fulfill()
        })
        let viewModel = ConversionViewModel(clientFactory: { worker }, durableQueueStore: queueStore)

        let adoptionSucceeded = await viewModel.adoptPersistentQueueItem(adopted.id)
        XCTAssertTrue(adoptionSucceeded)
        await fulfillment(of: [completed], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(conversionPaths, [adoptedURL.path])
        XCTAssertEqual(queueStore.items.map(\.state), [.waiting, .completed])
    }

    @MainActor
    func testEditingWaitingItemWithRouteQualityConflictParksItForResolution() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let item = makePersistentQueueFixture(ordinal: 0, state: .waiting)
        try await queueStore.replaceItems([item])
        let viewModel = ConversionViewModel(durableQueueStore: queueStore)
        guard case let .conflict(conflict) = RouteQualityEngine.propose(
            options: item.intent.options,
            edit: .reusableIntermediates(true)
        ) else {
            return XCTFail("Expected route-quality conflict")
        }

        try await viewModel.updatePersistentQueueItem(
            item.id,
            draft: try PersistentQueueItem(item: item).draft,
            routeQualityConflict: conflict
        )

        XCTAssertEqual(queueStore.items.first?.state, .needsChoice)
        XCTAssertEqual(viewModel.persistentQueueItems.first?.status, .needsChoice(conflict))
        XCTAssertEqual(viewModel.persistentQueueResolutionGroups.first?.candidates.map(\.id), [item.id])
    }

    @MainActor
    func testEditingWaitingItemWithConflictPausesRunningQueueAfterCurrent() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let active = makePersistentQueueFixture(ordinal: 0, state: .waiting)
        let held = makePersistentQueueFixture(ordinal: 1, state: .waiting)
        try await queueStore.replaceItems([active, held])
        let firstStarted = expectation(description: "first queue item started")
        let secondStarted = expectation(description: "second queue item started")
        secondStarted.isInverted = true
        let gate = QueueTestGate()
        let worker = ReorderableQueueWorkerClient(
            inspectionDone: {},
            conversionStarted: { _, conversionNumber in
                if conversionNumber == 1 {
                    firstStarted.fulfill()
                } else {
                    secondStarted.fulfill()
                }
            },
            conversionGate: gate
        )
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            durableQueueStore: queueStore,
            sourceAvailabilityResolver: { _ in true }
        )
        guard case let .conflict(conflict) = RouteQualityEngine.propose(
            options: held.intent.options,
            edit: .reusableIntermediates(true)
        ) else {
            return XCTFail("Expected route-quality conflict")
        }

        let startOutcome = await viewModel.startPersistentQueue()
        XCTAssertEqual(startOutcome, .accepted(.running))
        await fulfillment(of: [firstStarted], timeout: 2)
        try await viewModel.updatePersistentQueueItem(
            held.id,
            draft: try PersistentQueueItem(item: held).draft,
            routeQualityConflict: conflict
        )

        XCTAssertEqual(viewModel.persistentQueueRunState, .pauseAfterCurrent)
        XCTAssertEqual(queueStore.items[1].state, .needsChoice)
        await gate.open()
        while viewModel.hasActiveWork { await Task.yield() }

        await fulfillment(of: [secondStarted], timeout: 0.1)
        XCTAssertEqual(queueStore.items.map(\.state), [.completed, .needsChoice])
        XCTAssertEqual(viewModel.persistentQueueRunState, .paused)
    }

    @MainActor
    func testAdoptionRevalidatesAvailabilityBeforeWorkerSpawn() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("feature.mkv")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8))
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: sourceURL),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: directoryURL,
            options: ConversionOptions()
        )
        let item = DurableConversionQueueItem(
            ordinal: 0,
            origin: .singleSource,
            intent: DurableQueueItemIntent(draft: draft),
            state: .failed,
            failure: DurableQueueFailure(code: "temporary", message: "Temporary", details: nil, retryable: true)
        )
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([item])
        var availabilityChecks = 0
        var factoryCalls = 0
        let viewModel = ConversionViewModel(
            clientFactory: {
                factoryCalls += 1
                return TwoPhaseWorkerClient()
            },
            durableQueueStore: queueStore,
            sourceAvailabilityResolver: { _ in
                availabilityChecks += 1
                return availabilityChecks == 1
            }
        )

        let adopted = await viewModel.adoptPersistentQueueItem(item.id)
        XCTAssertTrue(adopted)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(factoryCalls, 0)
        XCTAssertEqual(queueStore.items.first?.failure?.code, "source_unavailable")
        XCTAssertTrue(queueStore.items.first?.failure?.retryable == true)
    }

    @MainActor
    func testUnavailablePersistentQueueItemAdvancesToNextAvailableSource() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let firstURL = directoryURL.appendingPathComponent("missing.mkv")
        let secondURL = directoryURL.appendingPathComponent("available.mkv")
        _ = FileManager.default.createFile(atPath: firstURL.path, contents: Data("video".utf8))
        _ = FileManager.default.createFile(atPath: secondURL.path, contents: Data("video".utf8))
        let inspection = SourceInspection(
            name: "Fixture",
            resolution: "1920x1080",
            frameRate: "24/1",
            interlaced: false,
            sizeBytes: 10
        )
        let drafts = [firstURL, secondURL].map { sourceURL in
            ConversionDraft(
                source: ConversionSource(kind: .matroska, url: sourceURL),
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: ConversionOptions()
            )
        }
        let queueStore = ConversionQueueStore.inMemory()
        let converted = expectation(description: "available source converted")
        var conversionPaths: [String] = []
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { job in
            conversionPaths.append(job.source.path)
            converted.fulfill()
        })
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            durableQueueStore: queueStore,
            sourceAvailabilityResolver: { $0.url != firstURL }
        )

        _ = try await viewModel.appendPersistentQueueDrafts(drafts)
        let startOutcome = await viewModel.startPersistentQueue()
        XCTAssertEqual(startOutcome, .accepted(.running))
        await fulfillment(of: [converted], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(conversionPaths, [secondURL.path])
        XCTAssertEqual(queueStore.items.map(\.state), [.failed, .completed])
        XCTAssertEqual(queueStore.items.first?.failure?.code, "source_unavailable")
    }

    @MainActor
    func testAdoptedRestoredSourceFolderRunsHeadless() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("feature.mkv")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8))
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: sourceURL),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: directoryURL,
            options: ConversionOptions()
        )
        let item = DurableConversionQueueItem(
            ordinal: 0,
            groupID: UUID(),
            origin: .sourceFolder,
            intent: DurableQueueItemIntent(draft: draft),
            state: .failed,
            failure: DurableQueueFailure(code: "temporary", message: "Temporary", details: nil, retryable: true)
        )
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([item])
        let completed = expectation(description: "headless folder completed")
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { _ in completed.fulfill() })
        let viewModel = ConversionViewModel(clientFactory: { worker }, durableQueueStore: queueStore)

        let adopted = await viewModel.adoptPersistentQueueItem(item.id)
        XCTAssertTrue(adopted)
        await fulfillment(of: [completed], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertNil(viewModel.batchQueue)
        XCTAssertEqual(queueStore.items.first?.state, .completed)
    }

    @MainActor
    func testAdoptedResolvedSourceFolderTitleRestoresLifecycleState() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        let title = SourceTitle(
            id: "makemkv:0",
            name: "Main Feature",
            outputName: "Feature",
            durationSeconds: 7_200,
            resolution: "1920x1080",
            frameRate: "24/1",
            mainFeature: true
        )
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24/1",
            interlaced: false,
            titles: [title]
        )
        let draft = ConversionDraft(
            source: ConversionSource(kind: .discImage, url: sourceURL),
            sourceDetails: inspection,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: directoryURL,
            options: ConversionOptions(),
            selectedTitle: title
        )
        let item = DurableConversionQueueItem(
            ordinal: 0,
            groupID: UUID(),
            origin: .sourceFolder,
            intent: DurableQueueItemIntent(draft: draft),
            inspection: inspection,
            state: .failed,
            failure: DurableQueueFailure(code: "temporary", message: "Temporary", details: nil, retryable: true)
        )
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([item])
        let completed = expectation(description: "restored resolved title completed")
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { _ in completed.fulfill() })
        let viewModel = ConversionViewModel(clientFactory: { worker }, durableQueueStore: queueStore)

        let adopted = await viewModel.adoptPersistentQueueItem(item.id)
        XCTAssertTrue(adopted)
        await fulfillment(of: [completed], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(queueStore.items.first?.state, .completed)
        XCTAssertNil(queueStore.items.first?.failure)
    }

    @MainActor
    func testAttentionItemRequiresExplicitSupportedAdoptionChoice() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("feature.mkv")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8))
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: sourceURL),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: directoryURL,
            options: ConversionOptions()
        )
        let item = DurableConversionQueueItem(
            ordinal: 0,
            origin: .singleSource,
            intent: DurableQueueItemIntent(draft: draft),
            state: .attention,
            decision: DurableQueueDecision(
                identifier: "subtitle_decision_required",
                prompt: "Retry without subtitles?",
                choices: [WorkerRecoveryChoice.retryWithoutSubtitles.rawValue]
            )
        )
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([item])
        let viewModel = ConversionViewModel(durableQueueStore: queueStore)

        let implicitAdoption = await viewModel.adoptPersistentQueueItem(item.id)
        XCTAssertFalse(implicitAdoption)
        let explicitAdoption = await viewModel.adoptPersistentQueueItem(
            item.id,
            recoveryChoice: .retryWithoutSubtitles
        )

        XCTAssertTrue(explicitAdoption)
        XCTAssertNotEqual(queueStore.items.first?.state, .attention)
        XCTAssertEqual(queueStore.items.first?.intent.options.encoding.subtitles.mode, .off)
    }

    @MainActor
    func testDurableSingleDecisionParksItemContinuesPeerAndRetriesExplicitly() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("feature.mkv")
        let peerURL = directoryURL.appendingPathComponent("peer.mkv")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8))
        _ = FileManager.default.createFile(atPath: peerURL.path, contents: Data("peer".utf8))
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24/1",
            interlaced: false
        )
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: sourceURL),
            sourceDetails: inspection,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: directoryURL,
            options: ConversionOptions()
        )
        let item = DurableConversionQueueItem(
            ordinal: 0,
            origin: .singleSource,
            intent: DurableQueueItemIntent(draft: draft),
            inspection: inspection,
            state: .waiting
        )
        let peerDraft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: peerURL),
            sourceDetails: inspection,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: directoryURL,
            options: ConversionOptions()
        )
        let peer = DurableConversionQueueItem(
            ordinal: 1,
            origin: .singleSource,
            intent: DurableQueueItemIntent(draft: peerDraft),
            inspection: inspection,
            state: .waiting
        )
        let decision = WorkerDecision(
            identifier: "subtitle_decision_required",
            prompt: "Retry without subtitles?",
            choices: [WorkerRecoveryChoice.retryWithoutSubtitles.rawValue],
            details: nil
        )
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([item, peer])
        let firstStarted = expectation(description: "first conversion started")
        let peerStarted = expectation(description: "eligible peer started")
        let retryStarted = expectation(description: "retry conversion started")
        var conversionCount = 0
        let worker = TwoPhaseWorkerClient(
            onConversionJobReceived: { _ in
                conversionCount += 1
                if conversionCount == 1 {
                    firstStarted.fulfill()
                } else if conversionCount == 2 {
                    peerStarted.fulfill()
                } else {
                    retryStarted.fulfill()
                }
            },
            recoveryDecision: decision
        )
        let viewModel = ConversionViewModel(clientFactory: { worker }, durableQueueStore: queueStore)

        let startOutcome = await viewModel.startPersistentQueue()
        XCTAssertEqual(startOutcome, .accepted(.running))
        await fulfillment(of: [firstStarted], timeout: 2)
        await fulfillment(of: [peerStarted], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(queueStore.items.map(\.state), [.attention, .completed])
        XCTAssertNil(viewModel.state.recoveryDecision)
        let readoptedParkedItem = await viewModel.adoptPersistentQueueItem(
            item.id,
            recoveryChoice: .retryWithoutSubtitles
        )
        XCTAssertTrue(readoptedParkedItem)
        await fulfillment(of: [retryStarted], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(queueStore.items.map(\.state), [.completed, .completed])
        XCTAssertEqual(queueStore.items.first?.intent.options.encoding.subtitles.mode, .off)
        XCTAssertEqual(queueStore.items.first?.attempts.count, 2)
    }

    @MainActor
    func testPersistentQueueAttentionParksWithoutStrandingRunState() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let item = makePersistentQueueFixture(ordinal: 0, state: .waiting)
        try await queueStore.replaceItems([item])
        let conversionStarted = expectation(description: "conversion started")
        let worker = TwoPhaseWorkerClient(
            onConversionJobReceived: { _ in conversionStarted.fulfill() },
            recoveryDecision: WorkerDecision(
                identifier: "subtitle_decision_required",
                prompt: "Retry without subtitles?",
                choices: [WorkerRecoveryChoice.retryWithoutSubtitles.rawValue],
                details: nil
            )
        )
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            durableQueueStore: queueStore,
            sourceAvailabilityResolver: { _ in true }
        )

        let startOutcome = await viewModel.startPersistentQueue()
        XCTAssertEqual(startOutcome, .accepted(.running))
        await fulfillment(of: [conversionStarted], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(queueStore.items.first?.state, .attention)
        XCTAssertNil(viewModel.state.recoveryDecision)
        XCTAssertEqual(viewModel.persistentQueueRunState, .idle)
        let rejectedRestart = await viewModel.startPersistentQueue()
        XCTAssertEqual(rejectedRestart, .rejected(.noEligibleItems))
        XCTAssertEqual(viewModel.persistentQueueRunState, .idle)
    }

    @MainActor
    func testAdoptedMixedOriginsSerializeThroughGlobalPump() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let firstURL = directoryURL.appendingPathComponent("first.mkv")
        let secondURL = directoryURL.appendingPathComponent("second.mkv")
        _ = FileManager.default.createFile(atPath: firstURL.path, contents: Data("video".utf8))
        _ = FileManager.default.createFile(atPath: secondURL.path, contents: Data("video".utf8))
        let inspection = SourceInspection(
            name: "Fixture",
            resolution: "1920x1080",
            frameRate: "24000/1001",
            interlaced: false
        )
        let makeDraft: (URL) -> ConversionDraft = { sourceURL in
            ConversionDraft(
                source: ConversionSource(kind: .matroska, url: sourceURL),
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: ConversionOptions()
            )
        }
        let failure = DurableQueueFailure(code: "temporary", message: "Temporary", details: nil, retryable: true)
        let single = DurableConversionQueueItem(
            ordinal: 0,
            origin: .singleSource,
            intent: DurableQueueItemIntent(draft: makeDraft(firstURL)),
            inspection: inspection,
            state: .failed,
            failure: failure
        )
        let folder = DurableConversionQueueItem(
            ordinal: 1,
            groupID: UUID(),
            origin: .sourceFolder,
            intent: DurableQueueItemIntent(draft: makeDraft(secondURL)),
            inspection: inspection,
            state: .failed,
            failure: failure
        )
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([single, folder])
        let conversions = expectation(description: "serialized conversions")
        conversions.expectedFulfillmentCount = 2
        var conversionPaths: [String] = []
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { job in
            conversionPaths.append(job.source.path)
            conversions.fulfill()
        })
        let viewModel = ConversionViewModel(clientFactory: { worker }, durableQueueStore: queueStore)

        let adoptedSingle = await viewModel.adoptPersistentQueueItem(single.id)
        let adoptedFolder = await viewModel.adoptPersistentQueueItem(folder.id)
        XCTAssertTrue(adoptedSingle)
        XCTAssertTrue(adoptedFolder)
        await fulfillment(of: [conversions], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(conversionPaths, [firstURL.path, secondURL.path])
        XCTAssertEqual(queueStore.items.map(\.state), [.completed, .completed])
    }

    @MainActor
    func testAdoptedInspectedMultiTitleRunsHeadlessly() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        let title = SourceTitle(
            id: "0",
            name: "Feature",
            outputName: "Feature",
            durationSeconds: 7_200,
            resolution: "1920x1080",
            frameRate: "24000/1001",
            mainFeature: true
        )
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24000/1001",
            interlaced: false,
            titles: [title]
        )
        let draft = ConversionDraft(
            source: ConversionSource(kind: .discImage, url: sourceURL),
            sourceDetails: inspection,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: directoryURL,
            options: ConversionOptions(),
            selectedTitle: title
        )
        let item = DurableConversionQueueItem(
            ordinal: 0,
            groupID: UUID(),
            origin: .multiTitle,
            intent: DurableQueueItemIntent(draft: draft),
            inspection: inspection,
            state: .interrupted
        )
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([item])
        let converted = expectation(description: "converted")
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { job in
            XCTAssertEqual(job.source.titleID, title.id)
            converted.fulfill()
        })
        let viewModel = ConversionViewModel(clientFactory: { worker }, durableQueueStore: queueStore)

        let adopted = await viewModel.adoptPersistentQueueItem(item.id)
        XCTAssertTrue(adopted)
        await fulfillment(of: [converted], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(queueStore.items.first?.state, .completed)
    }

    @MainActor
    func testPartialMultiTitleAdoptionPreservesSourceForUnfinishedSibling() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        let titles = ["One", "Two", "Three"].enumerated().map { offset, name in
            SourceTitle(
                id: "title-\(offset)",
                name: name,
                outputName: name,
                durationSeconds: 100 - Double(offset),
                resolution: "1920x1080",
                frameRate: "24/1",
                mainFeature: offset == 0
            )
        }
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24/1",
            interlaced: false,
            titles: titles
        )
        let groupID = UUID()
        let items = titles.enumerated().map { offset, title in
            var options = ConversionOptions()
            options.job.removeOriginalAfterSuccess = offset == titles.count - 1
            let draft = ConversionDraft(
                source: ConversionSource(kind: .discImage, url: sourceURL),
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: options,
                selectedTitle: title
            )
            return DurableConversionQueueItem(
                ordinal: offset,
                groupID: groupID,
                origin: .multiTitle,
                intent: DurableQueueItemIntent(draft: draft),
                inspection: inspection,
                state: offset == 0 ? .completed : .interrupted,
                result: offset == 0 ? DurableQueueResult(outputPath: "/Movies/One_AVP.mov") : nil
            )
        }
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems(items)
        let converted = expectation(description: "selected title converted")
        var removeOriginal: Bool?
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { job in
            removeOriginal = job.job?.removeOriginal
            converted.fulfill()
        })
        let viewModel = ConversionViewModel(clientFactory: { worker }, durableQueueStore: queueStore)

        let adopted = await viewModel.adoptPersistentQueueItem(items[2].id)
        XCTAssertTrue(adopted)
        await fulfillment(of: [converted], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(removeOriginal, false)
        XCTAssertEqual(queueStore.items.map(\.state), [.completed, .interrupted, .completed])
    }

    @MainActor
    func testMultiTitleGroupDoesNotBorrowRemovalIntentFromDifferentSource() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let firstURL = directoryURL.appendingPathComponent("first.iso")
        let secondURL = directoryURL.appendingPathComponent("second.iso")
        _ = FileManager.default.createFile(atPath: firstURL.path, contents: Data("disc".utf8))
        _ = FileManager.default.createFile(atPath: secondURL.path, contents: Data("disc".utf8))
        let title = SourceTitle(
            id: "One",
            name: "One",
            outputName: "One",
            durationSeconds: 100,
            resolution: "1920x1080",
            frameRate: "24/1",
            mainFeature: true
        )
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24/1",
            interlaced: false,
            titles: [title]
        )
        var removalOptions = ConversionOptions()
        removalOptions.job.removeOriginalAfterSuccess = true
        let groupID = UUID()
        let completed = DurableConversionQueueItem(
            ordinal: 0,
            groupID: groupID,
            origin: .multiTitle,
            intent: DurableQueueItemIntent(draft: ConversionDraft(
                source: ConversionSource(kind: .discImage, url: firstURL),
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: removalOptions,
                selectedTitle: title
            )),
            inspection: inspection,
            state: .completed,
            result: DurableQueueResult(outputPath: "/Movies/first.mov")
        )
        let adoptedItem = DurableConversionQueueItem(
            ordinal: 1,
            groupID: groupID,
            origin: .multiTitle,
            intent: DurableQueueItemIntent(draft: ConversionDraft(
                source: ConversionSource(kind: .discImage, url: secondURL),
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: ConversionOptions(),
                selectedTitle: title
            )),
            inspection: inspection,
            state: .interrupted
        )
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([completed, adoptedItem])
        let converted = expectation(description: "second source converted")
        var removeOriginal: Bool?
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { job in
            removeOriginal = job.job?.removeOriginal
            converted.fulfill()
        })
        let viewModel = ConversionViewModel(clientFactory: { worker }, durableQueueStore: queueStore)

        let adopted = await viewModel.adoptPersistentQueueItem(adoptedItem.id)
        XCTAssertTrue(adopted)
        await fulfillment(of: [converted], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(removeOriginal, false)
        XCTAssertEqual(queueStore.items.map(\.state), [.completed, .completed])
    }

    @MainActor
    func testAdoptingDifferentGroupDuringFailureDoesNotRebindActiveGroup() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        let titles = ["A1", "A2", "B1"].enumerated().map { offset, name in
            SourceTitle(
                id: name,
                name: name,
                outputName: name,
                durationSeconds: 100 - Double(offset),
                resolution: "1920x1080",
                frameRate: "24/1",
                mainFeature: offset == 0
            )
        }
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24/1",
            interlaced: false,
            titles: titles
        )
        let groupA = UUID()
        let groupB = UUID()
        let failure = DurableQueueFailure(code: "temporary", message: "Temporary", details: nil, retryable: true)
        let items = titles.enumerated().map { offset, title in
            let draft = ConversionDraft(
                source: ConversionSource(kind: .discImage, url: sourceURL),
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: ConversionOptions(),
                selectedTitle: title
            )
            return DurableConversionQueueItem(
                ordinal: offset,
                groupID: offset < 2 ? groupA : groupB,
                origin: .multiTitle,
                intent: DurableQueueItemIntent(draft: draft),
                inspection: inspection,
                state: .failed,
                failure: failure
            )
        }
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems(items)
        let firstStarted = expectation(description: "group A started")
        let secondStarted = expectation(description: "group B started")
        let gate = QueueTestGate()
        var convertedTitleIDs: [String] = []
        let worker = ReorderableQueueWorkerClient(
            inspectionDone: {},
            conversionStarted: { job, conversionNumber in
                convertedTitleIDs.append(job.source.titleID ?? "")
                if conversionNumber == 1 {
                    firstStarted.fulfill()
                } else if job.source.titleID == "B1" {
                    secondStarted.fulfill()
                }
            },
            failureOnConversionNumber: 1,
            conversionGate: gate
        )
        let viewModel = ConversionViewModel(clientFactory: { worker }, durableQueueStore: queueStore)

        let adoptedA1 = await viewModel.adoptPersistentQueueItem(items[0].id)
        let adoptedA2 = await viewModel.adoptPersistentQueueItem(items[1].id)
        XCTAssertTrue(adoptedA1)
        XCTAssertTrue(adoptedA2)
        await fulfillment(of: [firstStarted], timeout: 2)
        let adoptedB1 = await viewModel.adoptPersistentQueueItem(items[2].id)
        XCTAssertTrue(adoptedB1)
        await gate.open()
        await fulfillment(of: [secondStarted], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(convertedTitleIDs, ["A1", "A2", "B1"])
        XCTAssertEqual(queueStore.items.map(\.state), [.failed, .completed, .completed])
    }

    @MainActor
    func testAdoptedTitlePublishesResultWithUnadoptedWaitingSibling() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        let titles = ["One", "Two"].map {
            SourceTitle(
                id: $0,
                name: $0,
                outputName: $0,
                durationSeconds: 100,
                resolution: "1920x1080",
                frameRate: "24/1",
                mainFeature: $0 == "One"
            )
        }
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24/1",
            interlaced: false,
            titles: titles
        )
        let groupID = UUID()
        let items = titles.enumerated().map { offset, title in
            DurableConversionQueueItem(
                ordinal: offset,
                groupID: groupID,
                origin: .multiTitle,
                intent: DurableQueueItemIntent(draft: ConversionDraft(
                    source: ConversionSource(kind: .discImage, url: sourceURL),
                    sourceDetails: inspection,
                    profile: BuiltInProfile.balanced.profile,
                    destinationURL: directoryURL,
                    options: ConversionOptions(),
                    selectedTitle: title
                )),
                inspection: inspection
            )
        }
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems(items)
        let converted = expectation(description: "adopted title converted")
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { _ in converted.fulfill() })
        let viewModel = ConversionViewModel(clientFactory: { worker }, durableQueueStore: queueStore)

        let adopted = await viewModel.adoptPersistentQueueItem(items[0].id)
        XCTAssertTrue(adopted)
        await fulfillment(of: [converted], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(queueStore.items.map(\.state), [.completed, .waiting])
        XCTAssertEqual(viewModel.completedBatchResults?.count, 1)
        XCTAssertEqual(viewModel.completedBatchResults?.first?.outputPath, "/Movies/movie_AVP.mov")
    }

    @MainActor
    func testMultiTitleWithoutGroupFailsClosedInsteadOfStalling() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        let title = SourceTitle(
            id: "One",
            name: "One",
            outputName: "One",
            durationSeconds: 100,
            resolution: "1920x1080",
            frameRate: "24/1",
            mainFeature: true
        )
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24/1",
            interlaced: false,
            titles: [title]
        )
        let item = DurableConversionQueueItem(
            ordinal: 0,
            origin: .multiTitle,
            intent: DurableQueueItemIntent(draft: ConversionDraft(
                source: ConversionSource(kind: .discImage, url: sourceURL),
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: ConversionOptions(),
                selectedTitle: title
            )),
            inspection: inspection,
            state: .failed,
            failure: DurableQueueFailure(code: "temporary", message: "Temporary", details: nil, retryable: true)
        )
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([item])
        var factoryCalls = 0
        let viewModel = ConversionViewModel(
            clientFactory: {
                factoryCalls += 1
                return TwoPhaseWorkerClient()
            },
            durableQueueStore: queueStore
        )

        let adopted = await viewModel.adoptPersistentQueueItem(item.id)
        XCTAssertTrue(adopted)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(factoryCalls, 0)
        XCTAssertNotNil(viewModel.durableQueueRuntimeDiagnostic)
        XCTAssertEqual(viewModel.state.phase, .failed)
    }

    @MainActor
    func testStopForQuitDoesNotLaunchLaterAdoptedSingleSource() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let firstURL = directoryURL.appendingPathComponent("first.mkv")
        let secondURL = directoryURL.appendingPathComponent("second.mkv")
        _ = FileManager.default.createFile(atPath: firstURL.path, contents: Data("video".utf8))
        _ = FileManager.default.createFile(atPath: secondURL.path, contents: Data("video".utf8))
        let inspection = SourceInspection(name: "Fixture", resolution: "1920x1080", frameRate: "24/1", interlaced: false)
        let failure = DurableQueueFailure(code: "temporary", message: "Temporary", details: nil, retryable: true)
        let makeItem: (URL, Int) -> DurableConversionQueueItem = { sourceURL, ordinal in
            let draft = ConversionDraft(
                source: ConversionSource(kind: .matroska, url: sourceURL),
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: ConversionOptions()
            )
            return DurableConversionQueueItem(
                ordinal: ordinal,
                origin: .singleSource,
                intent: DurableQueueItemIntent(draft: draft),
                inspection: inspection,
                state: .failed,
                failure: failure
            )
        }
        let first = makeItem(firstURL, 0)
        let second = makeItem(secondURL, 1)
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([first, second])
        let firstStarted = expectation(description: "first started")
        let secondStarted = expectation(description: "second started after explicit readoption")
        var receivedPaths: [String] = []
        let cancellingWorker = TwoPhaseWorkerClient(
            onConversionJobReceived: { job in
                receivedPaths.append(job.source.path)
                firstStarted.fulfill()
            },
            waitsForConversionCancellation: true
        )
        let resumedWorker = TwoPhaseWorkerClient(onConversionJobReceived: { job in
            receivedPaths.append(job.source.path)
            secondStarted.fulfill()
        })
        var factoryCalls = 0
        let viewModel = ConversionViewModel(
            clientFactory: {
                factoryCalls += 1
                return factoryCalls == 1 ? cancellingWorker : resumedWorker
            },
            durableQueueStore: queueStore
        )

        let adoptedFirst = await viewModel.adoptPersistentQueueItem(first.id)
        let adoptedSecond = await viewModel.adoptPersistentQueueItem(second.id)
        XCTAssertTrue(adoptedFirst)
        XCTAssertTrue(adoptedSecond)
        await fulfillment(of: [firstStarted], timeout: 2)
        await viewModel.stopForQuit()

        XCTAssertEqual(receivedPaths, [firstURL.path])
        XCTAssertFalse(viewModel.hasActiveWork)
        XCTAssertEqual(queueStore.items.map(\.state), [.stopped, .notStarted])

        let readopted = await viewModel.adoptPersistentQueueItem(second.id)
        XCTAssertTrue(readopted)
        await fulfillment(of: [secondStarted], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(receivedPaths, [firstURL.path, secondURL.path])
        XCTAssertEqual(queueStore.items.map(\.state), [.stopped, .completed])
    }

    @MainActor
    func testAdoptionIsRejectedWhileStopSweepIsInFlight() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let activeURL = directoryURL.appendingPathComponent("active.mkv")
        let laterURL = directoryURL.appendingPathComponent("later.mkv")
        _ = FileManager.default.createFile(atPath: activeURL.path, contents: Data("video".utf8))
        _ = FileManager.default.createFile(atPath: laterURL.path, contents: Data("video".utf8))
        let inspection = SourceInspection(name: "Fixture", resolution: "1920x1080", frameRate: "24/1", interlaced: false)
        let failure = DurableQueueFailure(code: "temporary", message: "Temporary", details: nil, retryable: true)
        let makeItem: (URL, Int) -> DurableConversionQueueItem = { sourceURL, ordinal in
            DurableConversionQueueItem(
                ordinal: ordinal,
                origin: .singleSource,
                intent: DurableQueueItemIntent(draft: ConversionDraft(
                    source: ConversionSource(kind: .matroska, url: sourceURL),
                    sourceDetails: inspection,
                    profile: BuiltInProfile.balanced.profile,
                    destinationURL: directoryURL,
                    options: ConversionOptions()
                )),
                inspection: inspection,
                state: .failed,
                failure: failure
            )
        }
        let activeItem = makeItem(activeURL, 0)
        let laterItem = makeItem(laterURL, 1)
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([activeItem, laterItem])
        let activeStarted = expectation(description: "active item started")
        let worker = TwoPhaseWorkerClient(
            onConversionJobReceived: { _ in activeStarted.fulfill() },
            waitsForConversionCancellation: true
        )
        let viewModel = ConversionViewModel(clientFactory: { worker }, durableQueueStore: queueStore)

        let adoptedActive = await viewModel.adoptPersistentQueueItem(activeItem.id)
        XCTAssertTrue(adoptedActive)
        await fulfillment(of: [activeStarted], timeout: 2)
        viewModel.stopActiveWorker()
        let adoptedDuringStop = await viewModel.adoptPersistentQueueItem(laterItem.id)
        await viewModel.waitForBatchQueueSettled()

        XCTAssertFalse(adoptedDuringStop)
        XCTAssertEqual(queueStore.items.map(\.state), [.stopped, .failed])
    }

    @MainActor
    func testStoppingPlainInspectionDoesNotBlockLaterQueueAdoption() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let inspectionURL = directoryURL.appendingPathComponent("inspect.mkv")
        let queuedURL = directoryURL.appendingPathComponent("queued.mkv")
        _ = FileManager.default.createFile(atPath: inspectionURL.path, contents: Data("video".utf8))
        _ = FileManager.default.createFile(atPath: queuedURL.path, contents: Data("video".utf8))
        let inspection = SourceInspection(
            name: "Queued",
            resolution: "1920x1080",
            frameRate: "24/1",
            interlaced: false
        )
        let queuedItem = DurableConversionQueueItem(
            ordinal: 0,
            origin: .singleSource,
            intent: DurableQueueItemIntent(draft: ConversionDraft(
                source: ConversionSource(kind: .matroska, url: queuedURL),
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: ConversionOptions()
            )),
            inspection: inspection,
            state: .failed,
            failure: DurableQueueFailure(code: "temporary", message: "Temporary", details: nil, retryable: true)
        )
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([queuedItem])
        let inspectionStarted = expectation(description: "inspection started")
        let queuedConversionStarted = expectation(description: "queued conversion started")
        let inspectionWorker = CancellableInspectionWorkerClient(onStarted: { inspectionStarted.fulfill() })
        let queuedWorker = TwoPhaseWorkerClient(onConversionJobReceived: { _ in queuedConversionStarted.fulfill() })
        var factoryCalls = 0
        let viewModel = ConversionViewModel(
            clientFactory: {
                factoryCalls += 1
                return factoryCalls == 1 ? inspectionWorker : queuedWorker
            },
            durableQueueStore: queueStore
        )

        viewModel.selectSource(inspectionURL)
        await fulfillment(of: [inspectionStarted], timeout: 2)
        let adoptedWhileInspecting = await viewModel.adoptPersistentQueueItem(queuedItem.id)
        XCTAssertFalse(adoptedWhileInspecting)
        viewModel.stopActiveWorker()
        await viewModel.waitForBatchQueueSettled()

        let adopted = await viewModel.adoptPersistentQueueItem(queuedItem.id)
        XCTAssertTrue(adopted)
        await fulfillment(of: [queuedConversionStarted], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(queueStore.items.first?.state, .completed)
    }

    @MainActor
    func testRemoveRestoreRequiresExplicitReadoption() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("feature.mkv")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8))
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: sourceURL),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: directoryURL,
            options: ConversionOptions()
        )
        let item = DurableConversionQueueItem(ordinal: 0, origin: .singleSource, intent: DurableQueueItemIntent(draft: draft))
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([item])
        var factoryCalls = 0
        let viewModel = ConversionViewModel(
            clientFactory: {
                factoryCalls += 1
                return TwoPhaseWorkerClient()
            },
            durableQueueStore: queueStore
        )

        let adopted = await viewModel.adoptPersistentQueueItem(item.id)
        XCTAssertTrue(adopted)
        let token = try await viewModel.removePersistentQueueItems([item.id])
        try await viewModel.restorePersistentQueueItems(token)
        await Task.yield()

        XCTAssertEqual(factoryCalls, 0)
        XCTAssertEqual(queueStore.items.first?.state, .waiting)
    }

    @MainActor
    func testPersistentQueueAppendThenStartUsesExistingDurableRow() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("feature.mkv")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8))
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24/1",
            interlaced: false
        )
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: sourceURL),
            sourceDetails: inspection,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: directoryURL,
            options: ConversionOptions()
        )
        let queueStore = ConversionQueueStore.inMemory()
        let completed = expectation(description: "persistent queue conversion completed")
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { _ in completed.fulfill() })
        let viewModel = ConversionViewModel(clientFactory: { worker }, durableQueueStore: queueStore)

        let appendResult = try await viewModel.appendPersistentQueueDrafts([draft])
        XCTAssertEqual(appendResult.addedCount, 1)
        XCTAssertEqual(queueStore.items.count, 1)
        XCTAssertEqual(queueStore.items.first?.state, .waiting)
        XCTAssertFalse(viewModel.hasActiveWork)

        let startOutcome = await viewModel.startPersistentQueue()
        XCTAssertEqual(startOutcome, .accepted(.running))
        await fulfillment(of: [completed], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(queueStore.items.count, 1)
        XCTAssertEqual(queueStore.items.first?.state, .completed)
    }

    @MainActor
    func testPersistentQueueDoesNotSkipUnresolvedChoiceToRunLaterItem() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let held = makePersistentQueueFixture(ordinal: 0, state: .needsChoice)
        let waiting = makePersistentQueueFixture(ordinal: 1, state: .waiting)
        try await queueStore.replaceItems([held, waiting])
        var factoryCalls = 0
        let viewModel = ConversionViewModel(
            clientFactory: {
                factoryCalls += 1
                return TwoPhaseWorkerClient()
            },
            durableQueueStore: queueStore
        )

        let startOutcome = await viewModel.startPersistentQueue()

        XCTAssertEqual(startOutcome, .rejected(.unresolvedChoices))
        XCTAssertEqual(viewModel.persistentQueueRunState, .idle)
        XCTAssertEqual(queueStore.items.map(\.state), [.needsChoice, .waiting])
        XCTAssertEqual(factoryCalls, 0)
    }

    @MainActor
    func testPersistentQueueCommandsRejectInvalidTransitions() async throws {
        let viewModel = ConversionViewModel(durableQueueStore: .inMemory())

        let startOutcome = await viewModel.startPersistentQueue()
        XCTAssertEqual(startOutcome, .rejected(.noEligibleItems))
        XCTAssertEqual(viewModel.persistentQueueRunState, .idle)
        XCTAssertEqual(viewModel.pausePersistentQueueAfterCurrent(), .rejected(.queueIsNotRunning))
        XCTAssertEqual(viewModel.stopCurrentPersistentQueueItem(), .rejected(.queueIsNotRunning))
        XCTAssertEqual(viewModel.stopAllPersistentQueue(), .rejected(.noActiveItem))
    }

    @MainActor
    func testResumeSkipsParkedFailureAndRunsEligiblePeer() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        var failed = makePersistentQueueFixture(ordinal: 0, state: .failed)
        failed.failure = DurableQueueFailure(
            code: "temporary_failure",
            message: "Temporary failure",
            details: nil,
            retryable: true
        )
        let waiting = makePersistentQueueFixture(ordinal: 1, state: .waiting)
        try await queueStore.replaceItems([failed, waiting])
        let completed = expectation(description: "eligible peer completed")
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { _ in completed.fulfill() })
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            durableQueueStore: queueStore,
            sourceAvailabilityResolver: { _ in true }
        )

        let startOutcome = await viewModel.startPersistentQueue()
        XCTAssertEqual(startOutcome, .accepted(.running))
        await fulfillment(of: [completed], timeout: 2)
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(queueStore.items.map(\.state), [.failed, .completed])
    }

    @MainActor
    func testPauseAfterCurrentKeepsPendingRowsWaitingUntilResume() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let first = makePersistentQueueFixture(ordinal: 0, state: .waiting)
        let second = makePersistentQueueFixture(ordinal: 1, state: .waiting)
        try await queueStore.replaceItems([first, second])
        let firstStarted = expectation(description: "first queue item started")
        let secondStarted = expectation(description: "second queue item started")
        secondStarted.isInverted = true
        let gate = QueueTestGate()
        let worker = ReorderableQueueWorkerClient(
            inspectionDone: {},
            conversionStarted: { _, conversionNumber in
                if conversionNumber == 1 {
                    firstStarted.fulfill()
                } else {
                    secondStarted.fulfill()
                }
            },
            conversionGate: gate
        )
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            durableQueueStore: queueStore,
            sourceAvailabilityResolver: { _ in true }
        )

        let startOutcome = await viewModel.startPersistentQueue()
        XCTAssertEqual(startOutcome, .accepted(.running))
        await fulfillment(of: [firstStarted], timeout: 2)
        XCTAssertEqual(viewModel.pausePersistentQueueAfterCurrent(), .accepted(.pauseAfterCurrent))
        XCTAssertEqual(viewModel.pausePersistentQueueAfterCurrent(), .noChange(.pauseAfterCurrent))
        await gate.open()
        while viewModel.hasActiveWork { await Task.yield() }

        await fulfillment(of: [secondStarted], timeout: 0.1)
        XCTAssertEqual(queueStore.items.map(\.state), [.completed, .waiting])
        XCTAssertEqual(viewModel.persistentQueueRunState, .paused)
    }

    @MainActor
    func testPauseAfterCurrentRejectsAttentionRetryUntilActiveItemFinishes() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let active = makePersistentQueueFixture(ordinal: 0, state: .waiting)
        let waiting = makePersistentQueueFixture(ordinal: 1, state: .waiting)
        var attention = makePersistentQueueFixture(ordinal: 2, state: .attention)
        attention.decision = DurableQueueDecision(
            identifier: "subtitle_decision_required",
            prompt: "Retry without subtitles?",
            choices: [WorkerRecoveryChoice.retryWithoutSubtitles.rawValue]
        )
        try await queueStore.replaceItems([active, waiting, attention])
        let firstStarted = expectation(description: "active item started")
        let secondStarted = expectation(description: "waiting item started")
        secondStarted.isInverted = true
        let gate = QueueTestGate()
        let worker = ReorderableQueueWorkerClient(
            inspectionDone: {},
            conversionStarted: { _, conversionNumber in
                if conversionNumber == 1 {
                    firstStarted.fulfill()
                } else {
                    secondStarted.fulfill()
                }
            },
            conversionGate: gate
        )
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            durableQueueStore: queueStore,
            sourceAvailabilityResolver: { _ in true }
        )

        let startOutcome = await viewModel.startPersistentQueue()
        XCTAssertEqual(startOutcome, .accepted(.running))
        await fulfillment(of: [firstStarted], timeout: 2)
        XCTAssertEqual(viewModel.pausePersistentQueueAfterCurrent(), .accepted(.pauseAfterCurrent))

        let retryAccepted = await viewModel.adoptPersistentQueueItem(
            attention.id,
            recoveryChoice: .retryWithoutSubtitles
        )
        XCTAssertFalse(retryAccepted)
        XCTAssertEqual(viewModel.persistentQueueRunState, .pauseAfterCurrent)
        XCTAssertEqual(queueStore.items[2].state, .attention)

        await gate.open()
        while viewModel.hasActiveWork { await Task.yield() }

        await fulfillment(of: [secondStarted], timeout: 0.1)
        XCTAssertEqual(queueStore.items.map(\.state), [.completed, .waiting, .attention])
        XCTAssertEqual(viewModel.persistentQueueRunState, .paused)
    }

    @MainActor
    func testStopCurrentPausesWithoutDemotingWaitingPeers() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let first = makePersistentQueueFixture(ordinal: 0, state: .waiting)
        let second = makePersistentQueueFixture(ordinal: 1, state: .waiting)
        try await queueStore.replaceItems([first, second])
        let started = expectation(description: "queue item started")
        let worker = CancellableInspectionWorkerClient(onStarted: { started.fulfill() })
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            durableQueueStore: queueStore,
            sourceAvailabilityResolver: { _ in true }
        )

        let startOutcome = await viewModel.startPersistentQueue()
        XCTAssertEqual(startOutcome, .accepted(.running))
        await fulfillment(of: [started], timeout: 2)
        XCTAssertEqual(viewModel.stopCurrentPersistentQueueItem(), .accepted(.paused))
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(queueStore.items.map(\.state), [.stopped, .waiting])
        XCTAssertEqual(viewModel.persistentQueueRunState, .paused)
        XCTAssertEqual(viewModel.stopCurrentPersistentQueueItem(), .noChange(.paused))
    }

    @MainActor
    func testStopCurrentIdentifiesActiveSourceFolderItem() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let sourceFolderItem = makePersistentQueueFixture(
            ordinal: 0,
            groupID: UUID(),
            origin: .sourceFolder,
            state: .waiting
        )
        let waitingPeer = makePersistentQueueFixture(ordinal: 1, state: .waiting)
        try await queueStore.replaceItems([sourceFolderItem, waitingPeer])
        let started = expectation(description: "source folder queue item started")
        let worker = CancellableInspectionWorkerClient(onStarted: { started.fulfill() })
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            durableQueueStore: queueStore,
            sourceAvailabilityResolver: { _ in true }
        )

        let startOutcome = await viewModel.startPersistentQueue()
        XCTAssertEqual(startOutcome, .accepted(.running))
        await fulfillment(of: [started], timeout: 2)
        XCTAssertEqual(viewModel.stopCurrentPersistentQueueItem(), .accepted(.paused))
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(queueStore.items.map(\.state), [.stopped, .waiting])
        XCTAssertEqual(viewModel.persistentQueueRunState, .paused)
    }

    @MainActor
    func testPersistentQueueProjectionIncludesEveryOriginAndPreservesSelection() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let viewModel = ConversionViewModel(durableQueueStore: queueStore)
        let groupID = UUID()
        let items = [
            makePersistentQueueFixture(ordinal: 0, origin: .singleSource, state: .waiting),
            makePersistentQueueFixture(ordinal: 1, groupID: groupID, origin: .multiTitle, state: .processing),
            makePersistentQueueFixture(ordinal: 2, groupID: UUID(), origin: .sourceFolder, state: .interrupted),
        ]

        try await queueStore.replaceItems(items)
        viewModel.selectPersistentQueueItem(items[2].id)
        try await queueStore.updateWaitingItemIntent(items[0].id, intent: items[0].intent)

        XCTAssertEqual(viewModel.persistentQueueItems.map(\.id), items.map(\.id))
        XCTAssertEqual(viewModel.persistentQueueItems.map(\.origin), [.singleSource, .multiTitle, .sourceFolder])
        XCTAssertEqual(viewModel.selectedPersistentQueueItemID, items[2].id)
        XCTAssertTrue(viewModel.selectedPersistentQueueItem?.isRestored == true)
        XCTAssertEqual(viewModel.queueItems, [])
        XCTAssertNil(viewModel.batchQueue)
    }

    @MainActor
    func testPersistentQueueProjectionPreservesEveryDurableState() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let viewModel = ConversionViewModel(durableQueueStore: queueStore)
        let states = DurableQueueItemState.allCases
        let items = states.enumerated().map { offset, state in
            var item = makePersistentQueueFixture(ordinal: offset, state: state)
            if state == .attention {
                item.decision = DurableQueueDecision(
                    identifier: "subtitle_decision_required",
                    prompt: "Retry without subtitles?",
                    choices: ["retry_without_subtitles"],
                    staleAfterRestore: true
                )
            }
            if state == .needsChoice {
                item.routeQualityConflict = DurableRouteQualityConflict(conflict: makeRouteQualityConflict())
            }
            if state == .failed {
                item.failure = DurableQueueFailure(
                    code: "fixture_failure",
                    message: "Fixture failure",
                    details: nil,
                    retryable: true
                )
            }
            if state == .completed {
                item.result = DurableQueueResult(outputPath: "/tmp/completed.mov")
            }
            return item
        }

        try await queueStore.replaceItems(items)

        XCTAssertEqual(viewModel.persistentQueueItems.count, states.count)
        for (projected, state) in zip(viewModel.persistentQueueItems, states) {
            switch (projected.status, state) {
            case (.waiting, .waiting),
                 (.needsChoice, .needsChoice),
                 (.inspecting, .inspecting),
                 (.processing, .processing),
                 (.stopping, .stopping),
                 (.interrupted, .interrupted),
                 (.attention, .attention),
                 (.failed, .failed),
                 (.completed, .completed),
                 (.stopped, .stopped),
                 (.notStarted, .notStarted):
                break
            default:
                XCTFail("Unexpected projection \(projected.status) for state \(state)")
            }
        }
        XCTAssertTrue(viewModel.persistentQueueItems.first(where: { item in
            if case .attention = item.status {
                return true
            }
            return false
        })?.isRestored == true)
    }

    @MainActor
    func testPersistentQueueProjectionFailsClosedForInvalidDurableItem() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let viewModel = ConversionViewModel(durableQueueStore: queueStore)
        let validItem = makePersistentQueueFixture(ordinal: 0, state: .waiting)
        var invalidItem = makePersistentQueueFixture(ordinal: 1, state: .completed)
        invalidItem.result = nil

        viewModel.publishPersistentQueueProjection(items: [validItem, invalidItem])

        XCTAssertEqual(viewModel.persistentQueueItems, [])
        XCTAssertNil(viewModel.selectedPersistentQueueItemID)
        XCTAssertEqual(viewModel.persistentQueueProjectionError, .missingResult(invalidItem.id))
    }

    @MainActor
    func testPersistentQueueProjectionSelectionFallsBackAfterRemoval() async throws {
        let queueStore = ConversionQueueStore.inMemory()
        let viewModel = ConversionViewModel(durableQueueStore: queueStore)
        let first = makePersistentQueueFixture(ordinal: 0, state: .waiting)
        let second = makePersistentQueueFixture(ordinal: 1, state: .waiting)
        try await queueStore.replaceItems([first, second])
        viewModel.selectPersistentQueueItem(second.id)

        let token = try await viewModel.removePersistentQueueItems([second.id])

        XCTAssertEqual(viewModel.selectedPersistentQueueItemID, first.id)
        XCTAssertEqual(viewModel.persistentQueueItems.map(\.id), [first.id])

        try await viewModel.restorePersistentQueueItems(token)
        XCTAssertEqual(viewModel.persistentQueueItems.map(\.id), [first.id, second.id])
        XCTAssertEqual(viewModel.selectedPersistentQueueItemID, first.id)
    }

    @MainActor
    func testDurableTitleQueuePersistsSynchronousLaunchFailure() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        let queueStore = ConversionQueueStore.inMemory()
        let inspectionDone = expectation(description: "inspection done")
        let factoryCalls = RuntimePersistenceTestCounter()
        let worker = TwoPhaseWorkerClient(onInspectionComplete: { inspectionDone.fulfill() })
        let viewModel = ConversionViewModel(
            clientFactory: {
                guard factoryCalls.incrementAndReturn() == 1 else {
                    throw RuntimePersistenceTestError.workerLaunchFailed
                }
                return worker
            },
            durableQueueStore: queueStore
        )
        let source = ConversionSource(kind: .discImage, url: sourceURL)

        viewModel.selectSource(source)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        viewModel.startConversionQueue(drafts: makeDiscQueueDrafts(source: source, destinationURL: directoryURL))
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(queueStore.items.map(\.state), [.failed, .failed])
        XCTAssertEqual(queueStore.items.first?.failure?.message, RuntimePersistenceTestError.workerLaunchFailed.localizedDescription)
        XCTAssertNotNil(queueStore.items.first?.attempts.last?.endedAt)
    }

    @MainActor
    func testRestoredDurableTitleItemsRemainInert() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        let queueURL = directoryURL.appendingPathComponent("queue.json")
        let groupID = UUID()
        let source = ConversionSource(kind: .discImage, url: sourceURL)
        let store = ConversionQueueStore(fileURL: queueURL)
        try await store.replaceItems([
            DurableConversionQueueItem(
                ordinal: 0,
                groupID: groupID,
                origin: .multiTitle,
                intent: DurableQueueItemIntent(draft: makeDiscQueueDrafts(source: source, destinationURL: directoryURL)[0]),
                state: .processing
            ),
        ])

        var factoryCalls = 0
        let restoredStore = ConversionQueueStore(fileURL: queueURL)
        _ = ConversionViewModel(
            clientFactory: {
                factoryCalls += 1
                return TwoPhaseWorkerClient()
            },
            durableQueueStore: restoredStore
        )

        XCTAssertEqual(factoryCalls, 0)
        XCTAssertEqual(restoredStore.items.map(\.state), [DurableQueueItemState.interrupted])
    }

    @MainActor
    func testDurableTitleQueueFailsClosedBeforeSpawnWhenProcessingWriteFails() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        let queueURL = directoryURL.appendingPathComponent("queue.json")
        let writeCount = RuntimePersistenceTestCounter()
        let queueStore = ConversionQueueStore(
            fileURL: queueURL,
            dataWriter: { data, url in
                if writeCount.incrementAndReturn() > 1 {
                    throw RuntimePersistenceTestError.writeFailed
                }
                try data.write(to: url, options: .atomic)
            }
        )
        let inspectionDone = expectation(description: "inspection done")
        var factoryCalls = 0
        let worker = TwoPhaseWorkerClient(onInspectionComplete: { inspectionDone.fulfill() })
        let viewModel = ConversionViewModel(
            clientFactory: {
                factoryCalls += 1
                return worker
            },
            durableQueueStore: queueStore
        )
        let source = ConversionSource(kind: .discImage, url: sourceURL)

        viewModel.selectSource(source)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        viewModel.startConversionQueue(drafts: makeDiscQueueDrafts(source: source, destinationURL: directoryURL))
        while viewModel.durableQueueRuntimeDiagnostic == nil { await Task.yield() }

        XCTAssertEqual(factoryCalls, 1)
        XCTAssertNotNil(viewModel.durableQueueRuntimeDiagnostic)
        XCTAssertEqual(
            queueStore.items.map(\.state),
            [DurableQueueItemState.waiting, DurableQueueItemState.waiting]
        )
    }

    @MainActor
    func testStoppingDuringProcessingWritePreventsWorkerSpawn() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        let queueURL = directoryURL.appendingPathComponent("queue.json")
        let writeGate = RuntimePersistenceWriteGate(blockedWriteNumber: 2)
        let queueStore = ConversionQueueStore(
            fileURL: queueURL,
            dataWriter: { data, url in
                writeGate.writeStarted()
                try data.write(to: url, options: .atomic)
            }
        )
        let inspectionDone = expectation(description: "inspection done")
        var factoryCalls = 0
        let worker = TwoPhaseWorkerClient(onInspectionComplete: { inspectionDone.fulfill() })
        let viewModel = ConversionViewModel(
            clientFactory: {
                factoryCalls += 1
                return worker
            },
            durableQueueStore: queueStore
        )
        let source = ConversionSource(kind: .discImage, url: sourceURL)

        viewModel.selectSource(source)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        viewModel.startConversionQueue(drafts: makeDiscQueueDrafts(source: source, destinationURL: directoryURL))
        while !writeGate.isBlockedWriteWaiting { await Task.yield() }

        viewModel.stopActiveWorker()
        writeGate.releaseBlockedWrite()
        while viewModel.hasActiveWork { await Task.yield() }

        XCTAssertEqual(factoryCalls, 1)
        XCTAssertEqual(queueStore.items.map(\.state), [.stopped, .stopped])
        XCTAssertNotNil(queueStore.items.first?.attempts.last?.endedAt)
    }

    @MainActor
    func testDurableTitleQueueStopPersistsStoppingThenStopped() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let sourceURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        let inspectionDone = expectation(description: "inspection done")
        let conversionStarted = expectation(description: "conversion started")
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { _ in conversionStarted.fulfill() },
            waitsForConversionCancellation: true
        )
        let queueStore = ConversionQueueStore.inMemory()
        let viewModel = ConversionViewModel(clientFactory: { worker }, durableQueueStore: queueStore)
        let source = ConversionSource(kind: .discImage, url: sourceURL)

        viewModel.selectSource(source)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        viewModel.startConversionQueue(drafts: makeDiscQueueDrafts(source: source, destinationURL: directoryURL))
        await fulfillment(of: [conversionStarted], timeout: 2)
        while queueStore.items.first?.state != .processing { await Task.yield() }
        viewModel.stopActiveWorker()
        while queueStore.items.first?.state != .stopping { await Task.yield() }
        XCTAssertEqual(queueStore.items.map(\.state), [.stopping, .waiting])

        while viewModel.hasActiveWorker || viewModel.hasQueuedWork { await Task.yield() }
        XCTAssertEqual(queueStore.items.map(\.state), [.stopped, .stopped])
        XCTAssertNotNil(queueStore.items[0].attempts.last?.endedAt)
    }

    @MainActor
    func testMultiTitleQueuePublishesPartialResultsAfterLaterFailure() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let sourceURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        defer { try? FileManager.default.removeItem(at: directoryURL) }

        let inspectionDone = expectation(description: "inspection done")
        let conversionsDone = expectation(description: "queued conversions attempted")
        conversionsDone.expectedFulfillmentCount = 2
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { _ in conversionsDone.fulfill() },
            failureOnConversionNumber: 2
        )
        let viewModel = ConversionViewModel { worker }
        let source = ConversionSource(kind: .discImage, url: sourceURL)

        viewModel.selectSource(source)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }

        let titles = [
            SourceTitle(
                id: "makemkv:0",
                name: "Main Movie",
                outputName: "Feature",
                durationSeconds: 7_200,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: true
            ),
            SourceTitle(
                id: "makemkv:2",
                name: "3D Video 1",
                outputName: "Feature - 3D Video 1",
                durationSeconds: 600,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: false
            ),
        ]
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24000/1001",
            interlaced: false,
            titles: titles
        )
        let drafts = titles.map { title in
            ConversionDraft(
                source: source,
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: directoryURL,
                options: ConversionOptions(),
                selectedTitle: title
            )
        }

        viewModel.startConversionQueue(drafts: drafts)

        await fulfillment(of: [conversionsDone], timeout: 2)
        while viewModel.hasActiveWorker || viewModel.hasQueuedWork { await Task.yield() }

        XCTAssertEqual(viewModel.state.phase, .failed)
        XCTAssertEqual(viewModel.completedBatchResults?.count, 1)
        XCTAssertEqual(viewModel.restoredDurableQueueItems.map(\.state), [.completed, .failed])
        XCTAssertEqual(viewModel.restoredDurableQueueItems[1].failure?.code, "conversion_failed")
        XCTAssertNotNil(viewModel.restoredDurableQueueItems[1].attempts.last?.endedAt)
        if case .completed = viewModel.queueItems[0].status {} else {
            XCTFail("The first queued conversion should remain completed.")
        }
        if case .failed = viewModel.queueItems[1].status {} else {
            XCTFail("The second queued conversion should be marked failed.")
        }
    }

    @MainActor
    func testQueuedRecoveryRestartFailureClearsQueueAndRunsDeferredUpdate() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let sourceURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        defer { try? FileManager.default.removeItem(at: directoryURL) }

        let inspectionDone = expectation(description: "inspection done")
        let conversionStarted = expectation(description: "conversion started")
        let decision = WorkerDecision(
            identifier: "mkv_creation_decision_required",
            prompt: "Choose how to continue.",
            choices: ["retry_continue_on_error", "cancel"],
            details: nil
        )
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { _ in conversionStarted.fulfill() },
            recoveryDecision: decision
        )
        let viewModel = ConversionViewModel { worker }
        let source = ConversionSource(kind: .discImage, url: sourceURL)

        viewModel.selectSource(source)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        viewModel.startConversionQueue(drafts: makeDiscQueueDrafts(source: source, destinationURL: directoryURL))
        await fulfillment(of: [conversionStarted], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }

        XCTAssertEqual(viewModel.state.phase, .decisionRequired)
        XCTAssertTrue(viewModel.hasQueuedWork)
        let previewViewModel = PreviewViewModel(
            clientFactory: { worker },
            cache: PreviewCache(rootURL: directoryURL.appendingPathComponent("Previews", isDirectory: true))
        )
        let coordinator = AppWorkCoordinator(conversion: viewModel, preview: previewViewModel)
        XCTAssertTrue(coordinator.hasActiveWorker)
        let deferredUpdateRan = expectation(description: "deferred update ran")
        XCTAssertTrue(coordinator.postponeInstallUntilIdle { deferredUpdateRan.fulfill() })

        try FileManager.default.removeItem(at: sourceURL)
        XCTAssertFalse(viewModel.resolveRecoveryChoice(.retryContinueOnError))
        await fulfillment(of: [deferredUpdateRan], timeout: 2)

        XCTAssertEqual(viewModel.state.phase, .failed)
        XCTAssertFalse(viewModel.hasQueuedWork)
        XCTAssertTrue(viewModel.canSelectSource)
        if case .failed = viewModel.queueItems[0].status {} else {
            XCTFail("The active queue item should be marked failed.")
        }
        if case .cancelled = viewModel.queueItems[1].status {} else {
            XCTFail("The remaining queue item should remain resumable when its shared source becomes unavailable.")
        }
        XCTAssertTrue(viewModel.restoredDurableQueueItems[1].attempts.isEmpty)
    }

    @MainActor
    func testClearingDecisionPausedQueueRunsDeferredActions() async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let sourceURL = directoryURL.appendingPathComponent("Feature.iso")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("disc".utf8))
        defer { try? FileManager.default.removeItem(at: directoryURL) }

        let inspectionDone = expectation(description: "inspection done")
        let conversionStarted = expectation(description: "conversion started")
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { _ in conversionStarted.fulfill() },
            recoveryDecision: WorkerDecision(
                identifier: "mkv_creation_decision_required",
                prompt: "Choose how to continue.",
                choices: ["retry_continue_on_error", "cancel"],
                details: nil
            )
        )
        let viewModel = ConversionViewModel { worker }
        let source = ConversionSource(kind: .discImage, url: sourceURL)

        viewModel.selectSource(source)
        await fulfillment(of: [inspectionDone], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }
        viewModel.startConversionQueue(drafts: makeDiscQueueDrafts(source: source, destinationURL: directoryURL))
        await fulfillment(of: [conversionStarted], timeout: 2)
        while viewModel.hasActiveWorker { await Task.yield() }

        let deferredActionRan = expectation(description: "deferred action ran")
        XCTAssertTrue(viewModel.postponeInstallUntilIdle { deferredActionRan.fulfill() })
        viewModel.clearSource()
        await fulfillment(of: [deferredActionRan], timeout: 2)

        XCTAssertNil(viewModel.source)
        XCTAssertFalse(viewModel.hasQueuedWork)
        XCTAssertTrue(viewModel.queueItems.isEmpty)
        XCTAssertEqual(viewModel.restoredDurableQueueItems.map(\.state), [.stopped, .stopped])
    }

    private func makeDiscQueueDrafts(source: ConversionSource, destinationURL: URL) -> [ConversionDraft] {
        let titles = [
            SourceTitle(
                id: "makemkv:0",
                name: "Main Movie",
                outputName: "Feature",
                durationSeconds: 7_200,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: true
            ),
            SourceTitle(
                id: "makemkv:2",
                name: "3D Video 1",
                outputName: "Feature - 3D Video 1",
                durationSeconds: 600,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: false
            ),
        ]
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24000/1001",
            interlaced: false,
            titles: titles
        )
        return titles.map { title in
            ConversionDraft(
                source: source,
                sourceDetails: inspection,
                profile: BuiltInProfile.balanced.profile,
                destinationURL: destinationURL,
                options: ConversionOptions(),
                selectedTitle: title
            )
        }
    }

    @MainActor
    func testOffPeakScheduleRequiresFutureStart() async throws {
        let now = Date(timeIntervalSince1970: 1_000)
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([
            makePersistentQueueFixture(ordinal: 0, state: .waiting),
        ])
        let viewModel = ConversionViewModel(
            diagnosticClock: { now },
            durableQueueStore: queueStore
        )

        do {
            try await viewModel.saveOffPeakSchedule(
                startAt: now,
                endAt: now.addingTimeInterval(60)
            )
            XCTFail("Expected an invalid scheduling window")
        } catch let error as OffPeakScheduleStoreError {
            XCTAssertEqual(error, .invalidWindow)
        }
    }

    @MainActor
    func testOffPeakStartParksMissingDiscPeersAndContinuesAvailableWork() async throws {
        let now = Date(timeIntervalSince1970: 150)
        let scheduleStore = OffPeakScheduleStore.inMemory()
        let schedule = OffPeakQueueSchedule(
            startAt: Date(timeIntervalSince1970: 100),
            endAt: Date(timeIntervalSince1970: 200),
            createdAt: Date(timeIntervalSince1970: 50)
        )
        try await scheduleStore.save(schedule)

        let discURL = URL(fileURLWithPath: "/Volumes/FEATURE")
        let localURL = URL(fileURLWithPath: "/tmp/available.mkv")
        let inspection = SourceInspection(
            name: "Feature",
            resolution: "1920x1080",
            frameRate: "24/1",
            interlaced: false,
            sizeBytes: 10
        )
        let groupID = UUID()
        let discDraft = ConversionDraft(
            source: ConversionSource(
                kind: .physicalDisc,
                url: discURL,
                mediaIdentifier: "disc-a"
            ),
            sourceDetails: inspection,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/Output"),
            options: ConversionOptions()
        )
        let localDraft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: localURL),
            sourceDetails: inspection,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/Output"),
            options: ConversionOptions()
        )
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([
            DurableConversionQueueItem(
                ordinal: 0,
                groupID: groupID,
                origin: .multiTitle,
                intent: DurableQueueItemIntent(draft: discDraft)
            ),
            DurableConversionQueueItem(
                ordinal: 1,
                groupID: groupID,
                origin: .multiTitle,
                intent: DurableQueueItemIntent(draft: discDraft)
            ),
            DurableConversionQueueItem(
                ordinal: 2,
                origin: .singleSource,
                intent: DurableQueueItemIntent(draft: localDraft)
            ),
        ])
        let converted = expectation(description: "available item converted")
        var convertedPaths: [String] = []
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { job in
            convertedPaths.append(job.source.path)
            converted.fulfill()
        })
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            diagnosticClock: { now },
            durableQueueStore: queueStore,
            offPeakScheduleStore: scheduleStore,
            sourceAvailabilityResolver: { $0.kind != .physicalDisc }
        )

        let evaluation = await viewModel.evaluateOffPeakSchedule()
        XCTAssertEqual(evaluation, .start(schedule))
        await fulfillment(of: [converted], timeout: 2)
        for _ in 0..<1_000 where queueStore.items.last?.state != .completed || viewModel.hasActiveWorker {
            await Task.yield()
        }

        XCTAssertEqual(convertedPaths, [localURL.path])
        XCTAssertEqual(queueStore.items.map(\.state), [.failed, .stopped, .completed])
        XCTAssertEqual(queueStore.items.first?.failure?.code, "scheduled_disc_unavailable")
    }

    @MainActor
    func testOffPeakStartReportsUnresolvedChoiceInsteadOfQueueActivity() async throws {
        let now = Date(timeIntervalSince1970: 150)
        let scheduleStore = OffPeakScheduleStore.inMemory()
        let schedule = OffPeakQueueSchedule(
            startAt: Date(timeIntervalSince1970: 100),
            endAt: Date(timeIntervalSince1970: 200),
            createdAt: Date(timeIntervalSince1970: 50)
        )
        try await scheduleStore.save(schedule)
        let queueStore = ConversionQueueStore.inMemory()
        var options = ConversionOptions()
        try options.encoding.selectQualityStep(.maximumDetail)
        guard case let .conflict(conflict) = RouteQualityEngine.propose(
            options: options,
            edit: .reusableIntermediates(true)
        ) else {
            return XCTFail("Expected route-quality conflict")
        }
        var item = makePersistentQueueFixture(ordinal: 0, state: .needsChoice)
        item.routeQualityConflict = DurableRouteQualityConflict(conflict: conflict)
        try await queueStore.replaceItems([item])
        let viewModel = ConversionViewModel(
            diagnosticClock: { now },
            durableQueueStore: queueStore,
            offPeakScheduleStore: scheduleStore
        )

        let evaluation = await viewModel.evaluateOffPeakSchedule()
        XCTAssertEqual(evaluation, .start(schedule))
        XCTAssertEqual(scheduleStore.lastOutcome?.missReason, .unresolvedChoices)
        XCTAssertEqual(
            scheduleStore.lastOutcome?.message,
            "The scheduled window opened, but a queued video still needs a choice before the queue can run."
        )
    }

    @MainActor
    func testOffPeakWindowPausesBeforeStartingNextItem() async throws {
        let clock = LockedTestDate(Date(timeIntervalSince1970: 150))
        let scheduleStore = OffPeakScheduleStore.inMemory()
        let schedule = OffPeakQueueSchedule(
            startAt: Date(timeIntervalSince1970: 100),
            endAt: Date(timeIntervalSince1970: 200),
            createdAt: Date(timeIntervalSince1970: 50)
        )
        try await scheduleStore.save(schedule)
        let queueStore = ConversionQueueStore.inMemory()
        try await queueStore.replaceItems([
            makePersistentQueueFixture(ordinal: 0, state: .waiting),
            makePersistentQueueFixture(ordinal: 1, state: .waiting),
        ])
        var conversionCount = 0
        let firstConverted = expectation(description: "first item converted")
        let worker = TwoPhaseWorkerClient(onConversionJobReceived: { _ in
            conversionCount += 1
            clock.value = schedule.endAt
            firstConverted.fulfill()
        })
        let viewModel = ConversionViewModel(
            clientFactory: { worker },
            diagnosticClock: { clock.value },
            durableQueueStore: queueStore,
            offPeakScheduleStore: scheduleStore,
            sourceAvailabilityResolver: { _ in true }
        )

        let evaluation = await viewModel.evaluateOffPeakSchedule()
        XCTAssertEqual(evaluation, .start(schedule))
        await fulfillment(of: [firstConverted], timeout: 2)
        for _ in 0..<1_000 where viewModel.persistentQueueRunState != .paused || viewModel.hasActiveWorker {
            await Task.yield()
        }

        XCTAssertEqual(conversionCount, 1)
        XCTAssertEqual(viewModel.persistentQueueRunState, .paused)
        XCTAssertEqual(queueStore.items.map(\.state), [.completed, .waiting])
    }

    private func makeCompletedHistoryItems(
        count: Int,
        draft: ConversionDraft
    ) -> [DurableConversionQueueItem] {
        (0 ..< count).map { offset in
            let historicalDraft = ConversionDraft(
                source: ConversionSource(
                    kind: draft.source.kind,
                    url: URL(fileURLWithPath: "/tmp/history-\(offset).\(draft.source.url.pathExtension)")
                ),
                sourceDetails: draft.sourceDetails,
                profile: draft.profile,
                destinationURL: draft.destinationURL,
                options: draft.options,
                selectedTitle: draft.selectedTitle
            )
            return DurableConversionQueueItem(
                ordinal: offset,
                origin: .singleSource,
                intent: DurableQueueItemIntent(draft: historicalDraft),
                state: .completed,
                result: DurableQueueResult(outputPath: "/tmp/history-\(offset).mov")
            )
        }
    }

    private func makePersistentQueueFixture(
        ordinal: Int,
        groupID: UUID? = nil,
        origin: DurableQueueItemOrigin = .singleSource,
        state: DurableQueueItemState,
        id: UUID = UUID()
    ) -> DurableConversionQueueItem {
        let sourceURL = URL(fileURLWithPath: "/tmp/persistent-\(ordinal).mkv")
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: sourceURL),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/Output"),
            options: ConversionOptions()
        )
        return DurableConversionQueueItem(
            id: id,
            ordinal: ordinal,
            groupID: groupID,
            origin: origin,
            intent: DurableQueueItemIntent(draft: draft),
            state: state,
            routeQualityConflict: state == .needsChoice
                ? DurableRouteQualityConflict(conflict: makeRouteQualityConflict())
                : nil
        )
    }

    private func makeRouteQualityConflict() -> RouteQualityConflict {
        var options = ConversionOptions()
        try? options.encoding.selectQualityStep(.maximumDetail)
        guard case let .conflict(conflict) = RouteQualityEngine.propose(
            options: options,
            edit: .reusableIntermediates(true)
        ) else {
            fatalError("Expected reusable-file conflict")
        }
        return conflict
    }

    private func withTemporarySource(_ operation: @MainActor (URL) async throws -> Void) async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let sourceURL = directoryURL.appendingPathComponent("movie.m2ts")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8))
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try await operation(sourceURL)
    }

    private func diagnosticEvents(from archiveURL: URL) throws -> [[String: Any]] {
        let process = Process()
        let output = Pipe()
        let error = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/unzip")
        process.arguments = ["-p", archiveURL.path, "events.jsonl"]
        process.standardOutput = output
        process.standardError = error
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        let errorData = error.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw NSError(
                domain: "ConversionViewModelTests",
                code: Int(process.terminationStatus),
                userInfo: [NSLocalizedDescriptionKey: String(decoding: errorData, as: UTF8.self)]
            )
        }
        return try String(decoding: data, as: UTF8.self)
            .split(separator: "\n")
            .map { line in
                try XCTUnwrap(
                    JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any]
                )
            }
    }
}

private final class LockedTestDate: @unchecked Sendable {
    private let lock = NSLock()
    private var storedValue: Date

    init(_ value: Date) {
        storedValue = value
    }

    var value: Date {
        get {
            lock.withLock { storedValue }
        }
        set {
            lock.withLock { storedValue = newValue }
        }
    }
}

private enum RuntimePersistenceTestError: Error {
    case writeFailed
    case workerLaunchFailed
}

private final class RuntimePersistenceTestCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0

    func incrementAndReturn() -> Int {
        lock.withLock {
            value += 1
            return value
        }
    }
}

private final class RuntimePersistenceWriteGate: @unchecked Sendable {
    private let condition = NSCondition()
    private let blockedWriteNumber: Int
    private var writeCount = 0
    private var blockedWriteWaiting = false
    private var blockedWriteReleased = false

    init(blockedWriteNumber: Int) {
        self.blockedWriteNumber = blockedWriteNumber
    }

    var isBlockedWriteWaiting: Bool {
        condition.withLock { blockedWriteWaiting }
    }

    func writeStarted() {
        condition.lock()
        writeCount += 1
        guard writeCount == blockedWriteNumber else {
            condition.unlock()
            return
        }
        blockedWriteWaiting = true
        condition.broadcast()
        while !blockedWriteReleased {
            condition.wait()
        }
        condition.unlock()
    }

    func releaseBlockedWrite() {
        condition.withLock {
            blockedWriteReleased = true
            condition.broadcast()
        }
    }
}

private final class DiagnosticProbeThreadObservation: @unchecked Sendable {
    private let lock = NSLock()
    private var observations: [Bool] = []

    var observedMainThread: Bool? {
        lock.withLock {
            guard !observations.isEmpty else {
                return nil
            }
            return observations.contains(true)
        }
    }

    func recordCurrentThread() {
        lock.withLock {
            observations.append(Thread.isMainThread)
        }
    }
}

private struct ThreadRecordingStorageProbe: DiagnosticStorageProbing {
    let observation: DiagnosticProbeThreadObservation

    func probe(role: DiagnosticStorageRole, url: URL, capturedAt: Date) -> RawDiagnosticStorageProbe {
        observation.recordCurrentThread()
        return RawDiagnosticStorageProbe(
            capturedAt: capturedAt,
            role: role,
            url: url,
            status: .available,
            isDirectory: role == .destination,
            isReadable: true,
            isWritable: true,
            fileSizeBytes: 0,
            modificationAgeSeconds: 0,
            volumeAvailableBytes: 64 * 1_024 * 1_024 * 1_024,
            volumeTotalBytes: 128 * 1_024 * 1_024 * 1_024,
            volumeReadOnly: false,
            errorKind: nil
        )
    }
}

private actor QueueTestGate {
    private var openState = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func wait() async {
        guard !openState else {
            return
        }
        await withCheckedContinuation { continuation in
            waiters.append(continuation)
        }
    }

    func open() {
        openState = true
        let continuations = waiters
        waiters.removeAll()
        for continuation in continuations {
            continuation.resume()
        }
    }
}

private final class ReorderableQueueWorkerClient: WorkerProcessRunning, @unchecked Sendable {
    private let lock = NSLock()
    private let inspectionDone: () -> Void
    private let conversionStarted: (WorkerJobSpec, Int) -> Void
    private let blockedConversionNumber: Int
    private let failureOnConversionNumber: Int?
    private let conversionGate: QueueTestGate
    private var conversionCount = 0

    init(
        inspectionDone: @escaping () -> Void,
        conversionStarted: @escaping (WorkerJobSpec, Int) -> Void,
        blockedConversionNumber: Int = 1,
        failureOnConversionNumber: Int? = nil,
        conversionGate: QueueTestGate
    ) {
        self.inspectionDone = inspectionDone
        self.conversionStarted = conversionStarted
        self.blockedConversionNumber = blockedConversionNumber
        self.failureOnConversionNumber = failureOnConversionNumber
        self.conversionGate = conversionGate
    }

    func run(
        job: WorkerJobSpec,
        onEvent: @escaping (WorkerEvent) async throws -> Void
    ) async throws -> WorkerRunResult {
        let ready = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .workerReady,
            jobID: job.jobID,
            sequence: 0,
            payload: WorkerEventPayload(workerVersion: "test", processGroupID: 1)
        )
        try await onEvent(ready)
        if job.operation == "inspect_source" {
            let completed = WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .jobCompleted,
                jobID: job.jobID,
                sequence: 1,
                payload: WorkerEventPayload(
                    result: SourceInspection(
                        name: "Feature",
                        resolution: "1920x1080",
                        frameRate: "24/1",
                        interlaced: false,
                        sizeBytes: 10
                    )
                )
            )
            try await onEvent(completed)
            inspectionDone()
            return WorkerRunResult(terminalEvent: completed, exitStatus: 0, diagnostics: "")
        }

        let conversionNumber = lock.withLock {
            conversionCount += 1
            return conversionCount
        }
        conversionStarted(job, conversionNumber)
        if conversionNumber == blockedConversionNumber {
            await conversionGate.wait()
        }
        if conversionNumber == failureOnConversionNumber {
            let failed = WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .jobFailed,
                jobID: job.jobID,
                sequence: 1,
                payload: WorkerEventPayload(error: WorkerFailure(
                    code: "conversion_failed",
                    message: "The queued conversion failed.",
                    details: nil,
                    retryable: false
                ))
            )
            try await onEvent(failed)
            return WorkerRunResult(terminalEvent: failed, exitStatus: 1, diagnostics: "")
        }
        let completed = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .jobCompleted,
            jobID: job.jobID,
            sequence: 1,
            payload: WorkerEventPayload(
                conversionResult: ConversionResult(
                    outputPath: "/Movies/\(job.source.titleID ?? "movie")_AVP.mov",
                    titleID: job.source.titleID
                )
            )
        )
        try await onEvent(completed)
        return WorkerRunResult(terminalEvent: completed, exitStatus: 0, diagnostics: "")
    }

    func cancel() {}
}

private final class CancellableInspectionWorkerClient: WorkerProcessRunning, @unchecked Sendable {
    private let onStarted: () -> Void
    private let lock = NSLock()
    private var cancellationContinuation: CheckedContinuation<Void, Never>?
    private var cancellationRequested = false

    init(onStarted: @escaping () -> Void) {
        self.onStarted = onStarted
    }

    func run(
        job: WorkerJobSpec,
        onEvent: @escaping (WorkerEvent) async throws -> Void
    ) async throws -> WorkerRunResult {
        let ready = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .workerReady,
            jobID: job.jobID,
            sequence: 0,
            payload: WorkerEventPayload(workerVersion: "test", processGroupID: 1)
        )
        try await onEvent(ready)
        onStarted()
        await waitForCancellation()
        let cancelled = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .jobCancelled,
            jobID: job.jobID,
            sequence: 1,
            payload: WorkerEventPayload(message: "Inspection stopped.")
        )
        try await onEvent(cancelled)
        return WorkerRunResult(terminalEvent: cancelled, exitStatus: SIGTERM, diagnostics: "")
    }

    func cancel() {
        let continuation: CheckedContinuation<Void, Never>?
        lock.lock()
        cancellationRequested = true
        continuation = cancellationContinuation
        cancellationContinuation = nil
        lock.unlock()
        continuation?.resume()
    }

    private func waitForCancellation() async {
        await withCheckedContinuation { continuation in
            lock.lock()
            if cancellationRequested {
                lock.unlock()
                continuation.resume()
                return
            }
            cancellationContinuation = continuation
            lock.unlock()
        }
    }
}

private final class TwoPhaseWorkerClient: WorkerProcessRunning, @unchecked Sendable {
    private let lock = NSLock()
    private var conversionCancellationContinuation: CheckedContinuation<Void, Never>?
    private var conversionCancellationRequested = false
    private var pendingRecoveryDecision: WorkerDecision?
    private var conversionCount = 0
    private let inspectionResult: SourceInspection
    private let failureOnConversionNumber: Int?
    private let conversionFailure: WorkerFailure

    var onInspectionComplete: (() -> Void)?
    var onConversionJobReceived: ((WorkerJobSpec) -> Void)?
    private let waitsForConversionCancellation: Bool

    init(
        onInspectionComplete: (() -> Void)? = nil,
        onConversionJobReceived: ((WorkerJobSpec) -> Void)? = nil,
        waitsForConversionCancellation: Bool = false,
        recoveryDecision: WorkerDecision? = nil,
        failureOnConversionNumber: Int? = nil,
        conversionFailure: WorkerFailure = WorkerFailure(
            code: "conversion_failed",
            message: "The queued conversion failed.",
            details: nil,
            retryable: false
        ),
        inspectionResult: SourceInspection = SourceInspection(
            name: "movie",
            resolution: "1920x1080",
            frameRate: "24/1",
            interlaced: false,
            sizeBytes: 10
        )
    ) {
        self.onInspectionComplete = onInspectionComplete
        self.onConversionJobReceived = onConversionJobReceived
        self.waitsForConversionCancellation = waitsForConversionCancellation
        pendingRecoveryDecision = recoveryDecision
        self.failureOnConversionNumber = failureOnConversionNumber
        self.conversionFailure = conversionFailure
        self.inspectionResult = inspectionResult
    }

    func run(job: WorkerJobSpec, onEvent: @escaping (WorkerEvent) async throws -> Void) async throws -> WorkerRunResult {
        let isConversion: Bool
        let conversionNumber: Int
        let recoveryDecision: WorkerDecision?
        lock.lock()
        isConversion = job.operation == "convert_source"
        if isConversion {
            conversionCount += 1
        }
        conversionNumber = conversionCount
        recoveryDecision = isConversion ? pendingRecoveryDecision : nil
        if isConversion {
            pendingRecoveryDecision = nil
        }
        lock.unlock()

        let ready = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .workerReady,
            jobID: job.jobID,
            sequence: 0,
            payload: WorkerEventPayload(workerVersion: "test", processGroupID: 1)
        )

        if isConversion {
            onConversionJobReceived?(job)
            try await onEvent(ready)
            if conversionNumber == failureOnConversionNumber {
                let failed = WorkerEvent(
                    protocolVersion: WorkerJobSpec.protocolVersion,
                    type: .jobFailed,
                    jobID: job.jobID,
                    sequence: 1,
                    payload: WorkerEventPayload(error: conversionFailure)
                )
                try await onEvent(failed)
                return WorkerRunResult(terminalEvent: failed, exitStatus: 1, diagnostics: "")
            }
            if let recoveryDecision {
                let decisionRequired = WorkerEvent(
                    protocolVersion: WorkerJobSpec.protocolVersion,
                    type: .jobDecisionRequired,
                    jobID: job.jobID,
                    sequence: 1,
                    payload: WorkerEventPayload(decision: recoveryDecision)
                )
                try await onEvent(decisionRequired)
                return WorkerRunResult(terminalEvent: decisionRequired, exitStatus: 3, diagnostics: "")
            }
            if waitsForConversionCancellation {
                await waitForConversionCancellation()
                let cancelled = WorkerEvent(
                    protocolVersion: WorkerJobSpec.protocolVersion,
                    type: .jobCancelled,
                    jobID: job.jobID,
                    sequence: 1,
                    payload: WorkerEventPayload(message: "Conversion stopped.")
                )
                try await onEvent(cancelled)
                return WorkerRunResult(terminalEvent: cancelled, exitStatus: SIGTERM, diagnostics: "")
            }

            let completed = WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .jobCompleted,
                jobID: job.jobID,
                sequence: 1,
                payload: WorkerEventPayload(
                    conversionResult: ConversionResult(outputPath: "/Movies/movie_AVP.mov")
                )
            )
            try await onEvent(completed)
            return WorkerRunResult(terminalEvent: completed, exitStatus: 0, diagnostics: "")
        } else {
            let completed = WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .jobCompleted,
                jobID: job.jobID,
                sequence: 1,
                payload: WorkerEventPayload(result: inspectionResult)
            )
            try await onEvent(ready)
            try await onEvent(completed)
            onInspectionComplete?()
            return WorkerRunResult(terminalEvent: completed, exitStatus: 0, diagnostics: "")
        }
    }

    func cancel() {
        let continuation: CheckedContinuation<Void, Never>?
        lock.lock()
        conversionCancellationRequested = true
        continuation = conversionCancellationContinuation
        conversionCancellationContinuation = nil
        lock.unlock()
        continuation?.resume()
    }

    private func waitForConversionCancellation() async {
        await withCheckedContinuation { continuation in
            lock.lock()
            if conversionCancellationRequested {
                lock.unlock()
                continuation.resume()
                return
            }
            conversionCancellationContinuation = continuation
            lock.unlock()
        }
    }
}

private final class ControlledWorkerClient: WorkerProcessRunning, @unchecked Sendable {
    private let terminalDelivered: XCTestExpectation
    private let observabilityEvent: ObservabilityEvent?
    private let lock = NSLock()
    private var cancellationContinuation: CheckedContinuation<Void, Never>?
    private var cancellationRequested = false

    init(
        terminalDelivered: XCTestExpectation,
        observabilityEvent: ObservabilityEvent? = nil
    ) {
        self.terminalDelivered = terminalDelivered
        self.observabilityEvent = observabilityEvent
    }

    func run(
        job: WorkerJobSpec,
        onEvent: @escaping (WorkerEvent) async throws -> Void
    ) async throws -> WorkerRunResult {
        let ready = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .workerReady,
            jobID: job.jobID,
            sequence: 0,
            payload: WorkerEventPayload(workerVersion: "test", processGroupID: 1)
        )
        let observability = observabilityEvent.map {
            WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .observability,
                jobID: job.jobID,
                sequence: 1,
                payload: WorkerEventPayload(observabilityEvent: $0)
            )
        }
        let completed = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .jobCompleted,
            jobID: job.jobID,
            sequence: observability == nil ? 1 : 2,
            payload: WorkerEventPayload(
                result: SourceInspection(
                    name: "movie",
                    resolution: "1920x1080",
                    frameRate: "24/1",
                    interlaced: false,
                    sizeBytes: 10
                )
            )
        )

        try await onEvent(ready)
        if let observability {
            try await onEvent(observability)
        }
        try await onEvent(completed)
        terminalDelivered.fulfill()
        await waitForCancellation()
        return WorkerRunResult(terminalEvent: completed, exitStatus: SIGTERM, diagnostics: "")
    }

    func cancel() {
        let continuation: CheckedContinuation<Void, Never>?
        lock.lock()
        cancellationRequested = true
        continuation = cancellationContinuation
        cancellationContinuation = nil
        lock.unlock()
        continuation?.resume()
    }

    private func waitForCancellation() async {
        await withCheckedContinuation { continuation in
            lock.lock()
            if cancellationRequested {
                lock.unlock()
                continuation.resume()
                return
            }
            cancellationContinuation = continuation
            lock.unlock()
        }
    }
}

private final class RecordingObservabilityEventStore: ObservabilityEventPersisting, @unchecked Sendable {
    private let lock = NSLock()
    private var recordedEvents: [ObservabilityEvent] = []

    var events: [ObservabilityEvent] {
        lock.lock()
        defer { lock.unlock() }
        return recordedEvents
    }

    func append(_ event: ObservabilityEvent) {
        lock.lock()
        recordedEvents.append(event)
        lock.unlock()
    }

    func snapshot() -> ObservabilityEventPersistenceSnapshot {
        .disabled
    }
}

private func makeTestObservabilityEvent(kind: String) throws -> ObservabilityEvent {
    let fixtureURL = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .appendingPathComponent("tests/fixtures/observability_event_v1.json")
    let fixtureData = try XCTUnwrap(FileManager.default.contents(atPath: fixtureURL.path))
    var fixture = try XCTUnwrap(
        JSONSerialization.jsonObject(with: fixtureData) as? [String: Any]
    )
    fixture["kind"] = kind
    var context = try XCTUnwrap(fixture["context"] as? [String: Any])
    context["process"] = ["pid": 42, "process_group_id": 42]
    fixture["context"] = context
    return try JSONDecoder().decode(
        ObservabilityEvent.self,
        from: JSONSerialization.data(withJSONObject: fixture, options: [.sortedKeys])
    )
}

private struct BatchJobRecord: Equatable {
    let operation: String
    let sourcePath: String
    let titleID: String?
    let removeOriginal: Bool?
    let startStage: Int?
    let continueOnError: Bool?
}

private struct BatchFactoryError: LocalizedError {
    var errorDescription: String? {
        "Synthetic worker launch failure"
    }
}

private enum BatchWorkerBehavior: Equatable {
    case succeed
    case failInspection
    case failConversion
    case decision(WorkerDecision)
    case waitForCancellation
    case completeThenWaitForCancellation
}

private final class BatchWorkerScenario: @unchecked Sendable {
    private let lock = NSLock()
    private var remainingInspectionFailures: Set<String>
    private var remainingFailures: Set<String>
    private var remainingDecisions: [String: WorkerDecision]
    private var remainingCancellationPaths: Set<String>
    private var remainingCompletedCancellationPaths: Set<String>
    private let inspectionNames: [String: String]
    private var inspectionResults: [String: [SourceInspection]]
    private let completedConversionDelivered: (() -> Void)?
    private var storedRecords: [BatchJobRecord] = []
    private var storedClientCount = 0
    private var activeCount = 0
    private var storedMaximumActiveCount = 0

    init(
        failInspectionOnceFor: Set<String> = [],
        failConversionOnceFor: Set<String> = [],
        decisionConversionOnceFor: [String: WorkerDecision] = [:],
        holdConversionForCancellation: Set<String> = [],
        holdCompletedConversionForCancellation: Set<String> = [],
        inspectionNames: [String: String] = [:],
        inspectionResults: [String: [SourceInspection]] = [:],
        onCompletedConversionDelivered: (() -> Void)? = nil
    ) {
        remainingInspectionFailures = failInspectionOnceFor
        remainingFailures = failConversionOnceFor
        remainingDecisions = decisionConversionOnceFor
        remainingCancellationPaths = holdConversionForCancellation
        remainingCompletedCancellationPaths = holdCompletedConversionForCancellation
        self.inspectionNames = inspectionNames
        self.inspectionResults = inspectionResults
        completedConversionDelivered = onCompletedConversionDelivered
    }

    var records: [BatchJobRecord] {
        lock.withLock { storedRecords }
    }

    var clientCount: Int {
        lock.withLock { storedClientCount }
    }

    var maximumActiveCount: Int {
        lock.withLock { storedMaximumActiveCount }
    }

    func makeClient() -> any WorkerProcessRunning {
        lock.withLock {
            storedClientCount += 1
        }
        return BatchWorkerClient(scenario: self)
    }

    func begin(_ job: WorkerJobSpec) -> BatchWorkerBehavior {
        lock.withLock {
            activeCount += 1
            storedMaximumActiveCount = max(storedMaximumActiveCount, activeCount)
            storedRecords.append(
                BatchJobRecord(
                    operation: job.operation,
                    sourcePath: job.source.path,
                    titleID: job.source.titleID,
                    removeOriginal: job.job?.removeOriginal,
                    startStage: job.job?.startStage,
                    continueOnError: job.job?.continueOnError
                )
            )

            if job.operation == "inspect_source" {
                return remainingInspectionFailures.remove(job.source.path) == nil
                    ? .succeed
                    : .failInspection
            }
            guard job.operation == "convert_source" else {
                return .succeed
            }
            if remainingCancellationPaths.remove(job.source.path) != nil {
                return .waitForCancellation
            }
            if remainingCompletedCancellationPaths.remove(job.source.path) != nil {
                return .completeThenWaitForCancellation
            }
            if let decision = remainingDecisions.removeValue(forKey: job.source.path) {
                return .decision(decision)
            }
            if remainingFailures.remove(job.source.path) != nil {
                return .failConversion
            }
            return .succeed
        }
    }

    func inspectionResult(for sourcePath: String) -> SourceInspection {
        lock.withLock {
            if var results = inspectionResults[sourcePath], !results.isEmpty {
                let result = results.removeFirst()
                inspectionResults[sourcePath] = results
                return result
            }
            return SourceInspection(
                name: inspectionNames[sourcePath]
                    ?? URL(fileURLWithPath: sourcePath).deletingPathExtension().lastPathComponent,
                resolution: "1920x1080",
                frameRate: "24/1",
                interlaced: false,
                sizeBytes: 10
            )
        }
    }

    func end() {
        lock.withLock {
            activeCount -= 1
        }
    }

    func didDeliverCompletedConversion() {
        completedConversionDelivered?()
    }
}

private final class BatchWorkerClient: WorkerProcessRunning, @unchecked Sendable {
    private let scenario: BatchWorkerScenario
    private let lock = NSLock()
    private var cancellationContinuation: CheckedContinuation<Void, Never>?
    private var cancellationRequested = false

    init(scenario: BatchWorkerScenario) {
        self.scenario = scenario
    }

    func run(
        job: WorkerJobSpec,
        onEvent: @escaping (WorkerEvent) async throws -> Void
    ) async throws -> WorkerRunResult {
        let behavior = scenario.begin(job)
        defer { scenario.end() }

        let ready = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .workerReady,
            jobID: job.jobID,
            sequence: 0,
            payload: WorkerEventPayload(workerVersion: "batch-test", processGroupID: 1)
        )
        try await onEvent(ready)

        let terminalEvent: WorkerEvent
        let exitStatus: Int32
        if job.operation == "inspect_source" {
            if behavior == .failInspection {
                terminalEvent = WorkerEvent(
                    protocolVersion: WorkerJobSpec.protocolVersion,
                    type: .jobFailed,
                    jobID: job.jobID,
                    sequence: 1,
                    payload: WorkerEventPayload(
                        error: WorkerFailure(
                            code: "synthetic_inspection_failure",
                            message: "Synthetic inspection failure",
                            details: "Test inspection failure for \(job.source.path)",
                            retryable: true
                        )
                    )
                )
                exitStatus = 2
            } else {
                terminalEvent = WorkerEvent(
                    protocolVersion: WorkerJobSpec.protocolVersion,
                    type: .jobCompleted,
                    jobID: job.jobID,
                    sequence: 1,
                    payload: WorkerEventPayload(
                        result: scenario.inspectionResult(for: job.source.path)
                    )
                )
                exitStatus = 0
            }
        } else {
            switch behavior {
            case .succeed:
                let destinationPath = job.destination?.path ?? "/Movies"
                let outputStem = URL(fileURLWithPath: job.source.path)
                    .deletingPathExtension()
                    .lastPathComponent
                terminalEvent = WorkerEvent(
                    protocolVersion: WorkerJobSpec.protocolVersion,
                    type: .jobCompleted,
                    jobID: job.jobID,
                    sequence: 1,
                    payload: WorkerEventPayload(
                        conversionResult: ConversionResult(
                            outputPath: URL(fileURLWithPath: destinationPath, isDirectory: true)
                                .appendingPathComponent("\(outputStem)_AVP.mov")
                                .path
                        )
                    )
                )
                exitStatus = 0
            case .failConversion:
                terminalEvent = WorkerEvent(
                    protocolVersion: WorkerJobSpec.protocolVersion,
                    type: .jobFailed,
                    jobID: job.jobID,
                    sequence: 1,
                    payload: WorkerEventPayload(
                        error: WorkerFailure(
                            code: "synthetic_failure",
                            message: "Synthetic conversion failure",
                            details: "Test failure for \(job.source.path)",
                            retryable: true
                        )
                    )
                )
                exitStatus = 2
            case .failInspection:
                preconditionFailure("Inspection failures cannot occur during conversion")
            case let .decision(decision):
                terminalEvent = WorkerEvent(
                    protocolVersion: WorkerJobSpec.protocolVersion,
                    type: .jobDecisionRequired,
                    jobID: job.jobID,
                    sequence: 1,
                    payload: WorkerEventPayload(decision: decision)
                )
                exitStatus = 3
            case .waitForCancellation:
                await waitForCancellation()
                terminalEvent = WorkerEvent(
                    protocolVersion: WorkerJobSpec.protocolVersion,
                    type: .jobCancelled,
                    jobID: job.jobID,
                    sequence: 1,
                    payload: WorkerEventPayload(message: "Conversion stopped.")
                )
                exitStatus = SIGTERM
            case .completeThenWaitForCancellation:
                let destinationPath = job.destination?.path ?? "/Movies"
                let outputStem = URL(fileURLWithPath: job.source.path)
                    .deletingPathExtension()
                    .lastPathComponent
                let completed = WorkerEvent(
                    protocolVersion: WorkerJobSpec.protocolVersion,
                    type: .jobCompleted,
                    jobID: job.jobID,
                    sequence: 1,
                    payload: WorkerEventPayload(
                        conversionResult: ConversionResult(
                            outputPath: URL(fileURLWithPath: destinationPath, isDirectory: true)
                                .appendingPathComponent("\(outputStem)_AVP.mov")
                                .path
                        )
                    )
                )
                try await onEvent(completed)
                scenario.didDeliverCompletedConversion()
                await waitForCancellation()
                return WorkerRunResult(terminalEvent: completed, exitStatus: 0, diagnostics: "")
            }
        }

        try await onEvent(terminalEvent)
        return WorkerRunResult(terminalEvent: terminalEvent, exitStatus: exitStatus, diagnostics: "")
    }

    func cancel() {
        let continuation = lock.withLock { () -> CheckedContinuation<Void, Never>? in
            cancellationRequested = true
            let continuation = cancellationContinuation
            cancellationContinuation = nil
            return continuation
        }
        continuation?.resume()
    }

    private func waitForCancellation() async {
        await withCheckedContinuation { continuation in
            let shouldResume = lock.withLock { () -> Bool in
                if cancellationRequested {
                    return true
                }
                cancellationContinuation = continuation
                return false
            }
            if shouldResume {
                continuation.resume()
            }
        }
    }
}

private final class BlockingDiagnosticArchiveWriter: @unchecked Sendable {
    private let lock = NSLock()
    private var started = false
    private var cancelled = false

    var hasStarted: Bool {
        lock.withLock { started }
    }

    var observedCancellation: Bool {
        lock.withLock { cancelled }
    }

    func write(_ data: Data, to url: URL) throws {
        _ = data
        _ = url
        lock.withLock { started = true }
        while !Task.isCancelled {
            Thread.sleep(forTimeInterval: 0.005)
        }
        lock.withLock { cancelled = true }
        throw CancellationError()
    }
}

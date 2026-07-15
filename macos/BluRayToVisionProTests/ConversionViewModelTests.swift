import Darwin
import Foundation
import XCTest
@testable import BluRayToVisionPro

final class ConversionViewModelTests: XCTestCase {
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
        while viewModel.hasActiveWorker { await Task.yield() }

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
            outputLength: .fullMovie,
            samplePosition: .beginning,
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
        let viewModel = ConversionViewModel { worker }
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
            outputLength: .fullMovie,
            samplePosition: .beginning,
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
        let viewModel = ConversionViewModel {
            TwoPhaseWorkerClient(onInspectionComplete: { inspectionDone.fulfill() })
        }
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
                outputLength: .fullMovie,
                samplePosition: .beginning,
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
        let viewModel = ConversionViewModel { worker }
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
                outputLength: .fullMovie,
                samplePosition: .beginning,
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
                outputLength: .fullMovie,
                samplePosition: .beginning,
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
                outputLength: .fullMovie,
                samplePosition: .beginning,
                options: ConversionOptions()
            )

            viewModel.startConversion(draft: draft)
            await fulfillment(of: [firstConversion], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            XCTAssertTrue(viewModel.resolveRecoveryChoice(.retryWithoutSubtitles))
            await fulfillment(of: [recoveryConversion], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }

            XCTAssertEqual(conversionJobs[1].job?.startStage, ConversionStage.extractSubtitles.rawValue)
            XCTAssertTrue(conversionJobs[1].encoding?.skipSubtitles == true)
            XCTAssertTrue(draft.options.encoding.includeSubtitles)
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
                    outputLength: .fullMovie,
                    samplePosition: .beginning,
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
                outputLength: .fullMovie,
                samplePosition: .beginning,
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
                outputLength: .fullMovie,
                samplePosition: .beginning,
                options: ConversionOptions()
            )
            viewModel.startConversion(draft: draft)
            await fulfillment(of: [conversionStarted], timeout: 2)

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
                outputLength: .fullMovie,
                samplePosition: .beginning,
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
                outputLength: .fullMovie,
                samplePosition: .beginning,
                options: ConversionOptions()
            )

            viewModel.startConversion(draft: draft, jobID: expectedJobID)

            await fulfillment(of: [conversionStarted], timeout: 2)
            while viewModel.hasActiveWorker { await Task.yield() }
            XCTAssertEqual(viewModel.state.phase, .completed)
        }
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
            XCTFail("The pending queue item should be marked cancelled.")
        }
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

    private func withTemporarySource(_ operation: @MainActor (URL) async throws -> Void) async throws {
        let directoryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let sourceURL = directoryURL.appendingPathComponent("movie.m2ts")
        _ = FileManager.default.createFile(atPath: sourceURL.path, contents: Data("video".utf8))
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try await operation(sourceURL)
    }
}

private extension ConversionDraft {
    init(
        source: ConversionSource,
        sourceDetails: SourceInspection?,
        profile: EncodingProfile,
        destinationURL: URL,
        outputLength: OutputLength,
        samplePosition: SamplePosition,
        options: ConversionOptions
    ) {
        self.init(
            source: source,
            sourceDetails: sourceDetails,
            profile: profile,
            destinationURL: destinationURL,
            options: options
        )
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
    private let lock = NSLock()
    private var cancellationContinuation: CheckedContinuation<Void, Never>?
    private var cancellationRequested = false

    init(terminalDelivered: XCTestExpectation) {
        self.terminalDelivered = terminalDelivered
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
        let completed = WorkerEvent(
            protocolVersion: WorkerJobSpec.protocolVersion,
            type: .jobCompleted,
            jobID: job.jobID,
            sequence: 1,
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

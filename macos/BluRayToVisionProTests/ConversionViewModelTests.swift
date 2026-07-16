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
        let worker = TwoPhaseWorkerClient(
            onInspectionComplete: { inspectionDone.fulfill() },
            onConversionJobReceived: { spec in
                XCTAssertEqual(URL(fileURLWithPath: spec.source.path).pathExtension, "iso")
                XCTAssertEqual(spec.operation, "convert_source")
                conversionStarted.fulfill()
            }
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
            XCTAssertTrue(viewModel.resolveRecoveryChoice(.cancel))

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
    func testBatchFailureContinuesAndExplicitRetryUsesStoredDraft() async throws {
        try await withTemporaryBatchSources(["first.mkv", "second.m2ts"]) { folderURL, sourceURLs, destinationURL in
            var options = ConversionOptions()
            options.encoding.hevcQuality = 83
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
            XCTAssertEqual(queue.items[0].draft?.options.encoding.hevcQuality, 83)
            XCTAssertEqual(
                scenario.records.filter { $0.sourcePath == firstPath }.map(\.operation),
                ["inspect_source", "convert_source", "inspect_source", "convert_source"]
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
    func testDeferredFactoryFailureCanBeStoppedBeforeNextItemLaunches() async throws {
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
            XCTAssertEqual(queue.items.map(\.status), [.stopped, .notStarted])
            XCTAssertEqual(factoryCalls, 1)
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
    private func waitForBatchCompletion(_ viewModel: ConversionViewModel) async {
        let deadline = Date().addingTimeInterval(5)
        while Date() < deadline {
            if viewModel.batchQueue?.completionID != nil, !viewModel.hasActiveWork {
                return
            }
            await Task.yield()
        }
        XCTFail("Timed out waiting for the batch to finish")
    }

    @MainActor
    private func waitForBatchStatus(
        _ viewModel: ConversionViewModel,
        status: ConversionQueueItemStatus
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
    private var callCount = 0
    private var conversionCancellationContinuation: CheckedContinuation<Void, Never>?
    private var conversionCancellationRequested = false
    private var pendingRecoveryDecision: WorkerDecision?

    var onInspectionComplete: (() -> Void)?
    var onConversionJobReceived: ((WorkerJobSpec) -> Void)?
    private let waitsForConversionCancellation: Bool

    init(
        onInspectionComplete: (() -> Void)? = nil,
        onConversionJobReceived: ((WorkerJobSpec) -> Void)? = nil,
        waitsForConversionCancellation: Bool = false,
        recoveryDecision: WorkerDecision? = nil
    ) {
        self.onInspectionComplete = onInspectionComplete
        self.onConversionJobReceived = onConversionJobReceived
        self.waitsForConversionCancellation = waitsForConversionCancellation
        pendingRecoveryDecision = recoveryDecision
    }

    func run(job: WorkerJobSpec, onEvent: @escaping (WorkerEvent) async throws -> Void) async throws -> WorkerRunResult {
        let isConversion: Bool
        let recoveryDecision: WorkerDecision?
        lock.lock()
        callCount += 1
        isConversion = callCount > 1
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
            let result = SourceInspection(name: "movie", resolution: "1920x1080", frameRate: "24/1", interlaced: false, sizeBytes: 10)
            let completed = WorkerEvent(
                protocolVersion: WorkerJobSpec.protocolVersion,
                type: .jobCompleted,
                jobID: job.jobID,
                sequence: 1,
                payload: WorkerEventPayload(result: result)
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

private struct BatchJobRecord: Equatable {
    let operation: String
    let sourcePath: String
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
}

private final class BatchWorkerScenario: @unchecked Sendable {
    private let lock = NSLock()
    private var remainingInspectionFailures: Set<String>
    private var remainingFailures: Set<String>
    private var remainingDecisions: [String: WorkerDecision]
    private let cancellationPaths: Set<String>
    private let inspectionNames: [String: String]
    private var storedRecords: [BatchJobRecord] = []
    private var storedClientCount = 0
    private var activeCount = 0
    private var storedMaximumActiveCount = 0

    init(
        failInspectionOnceFor: Set<String> = [],
        failConversionOnceFor: Set<String> = [],
        decisionConversionOnceFor: [String: WorkerDecision] = [:],
        holdConversionForCancellation: Set<String> = [],
        inspectionNames: [String: String] = [:]
    ) {
        remainingInspectionFailures = failInspectionOnceFor
        remainingFailures = failConversionOnceFor
        remainingDecisions = decisionConversionOnceFor
        cancellationPaths = holdConversionForCancellation
        self.inspectionNames = inspectionNames
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
            if cancellationPaths.contains(job.source.path) {
                return .waitForCancellation
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

    func inspectionName(for sourcePath: String) -> String {
        lock.withLock {
            inspectionNames[sourcePath]
                ?? URL(fileURLWithPath: sourcePath).deletingPathExtension().lastPathComponent
        }
    }

    func end() {
        lock.withLock {
            activeCount -= 1
        }
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
                        result: SourceInspection(
                            name: scenario.inspectionName(for: job.source.path),
                            resolution: "1920x1080",
                            frameRate: "24/1",
                            interlaced: false,
                            sizeBytes: 10
                        )
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

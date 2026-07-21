import Foundation
import XCTest
@testable import BluRayToVisionPro

final class DiagnosticReportViewModelTests: XCTestCase {
    @MainActor
    func testNoJobHasNoDiagnosticActionEvidence() {
        let conversionViewModel = ConversionViewModel()

        XCTAssertFalse(conversionViewModel.hasDiagnosticEvidence)
    }

    @MainActor
    func testLocalOnlyFlowCapturesReviewsSavesSharesAndExplicitlyCleansTemporaryCopy() async throws {
        let rootDirectory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: rootDirectory) }
        let workspace = DiagnosticArtifactWorkspace(rootDirectory: rootDirectory)
        let viewModel = DiagnosticReportViewModel(
            uploader: nil,
            workspace: workspace,
            capture: { directory, _ in
                try Self.makeArtifact(in: directory, data: Data("local diagnostics".utf8))
            }
        )

        viewModel.begin()
        viewModel.beginCapture()
        try await waitUntil { viewModel.phase == .review }

        XCTAssertFalse(viewModel.isUploadAvailable)
        let artifact = try XCTUnwrap(viewModel.artifact)
        XCTAssertEqual(viewModel.shareURL, artifact.archiveURL)
        XCTAssertTrue(FileManager.default.fileExists(atPath: artifact.archiveURL.path))

        let savedURL = rootDirectory.appendingPathComponent("saved-diagnostics.zip")
        XCTAssertTrue(viewModel.saveCopy(to: savedURL))
        XCTAssertEqual(viewModel.lastSavedCopyURL, savedURL)
        XCTAssertTrue(FileManager.default.fileExists(atPath: savedURL.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: artifact.archiveURL.path))

        XCTAssertTrue(viewModel.discardLocalCopy())
        XCTAssertEqual(viewModel.phase, .idle)
        XCTAssertNil(viewModel.artifact)
        XCTAssertFalse(FileManager.default.fileExists(atPath: artifact.archiveURL.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: savedURL.path))
    }

    @MainActor
    func testSuccessfulUploadPresentsReceiptBeforeCleaningTemporaryArtifact() async throws {
        let rootDirectory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: rootDirectory) }
        let receipt = DiagnosticReportReceipt(
            supportCode: "BDAVP-0123456789ABCDEF",
            expiresAt: Date(timeIntervalSince1970: 1_790_000_000)
        )
        let uploader = ScriptedDiagnosticUploader(outcomes: [.success(receipt)])
        let viewModel = DiagnosticReportViewModel(
            uploader: uploader,
            workspace: DiagnosticArtifactWorkspace(rootDirectory: rootDirectory),
            capture: { directory, _ in
                try Self.makeArtifact(in: directory, data: Data("upload diagnostics".utf8))
            }
        )

        viewModel.begin()
        viewModel.beginCapture()
        try await waitUntil { viewModel.phase == .review }
        let temporaryURL = try XCTUnwrap(viewModel.artifact?.archiveURL)
        viewModel.uploadCapturedArtifact()
        try await waitUntil {
            if case .success = viewModel.phase { return true }
            return false
        }

        guard case let .success(actualReceipt) = viewModel.phase else {
            return XCTFail("Expected success phase")
        }
        XCTAssertEqual(actualReceipt, receipt)
        XCTAssertNil(viewModel.artifact)
        XCTAssertFalse(FileManager.default.fileExists(atPath: temporaryURL.path))
        let attemptCount = await uploader.attemptCount
        let lastProgress = await uploader.lastProgress
        XCTAssertEqual(attemptCount, 1)
        XCTAssertEqual(lastProgress, 0.65)
    }

    @MainActor
    func testCancellingUploadKeepsArtifactAndDoesNotTouchConversionState() async throws {
        let rootDirectory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: rootDirectory) }
        let uploadStarted = expectation(description: "upload started")
        let uploader = HoldingDiagnosticUploader {
            uploadStarted.fulfill()
        }
        let conversionViewModel = ConversionViewModel()
        let originalConversionState = conversionViewModel.state
        let viewModel = DiagnosticReportViewModel(
            uploader: uploader,
            workspace: DiagnosticArtifactWorkspace(rootDirectory: rootDirectory),
            capture: { directory, _ in
                try Self.makeArtifact(in: directory, data: Data("cancel diagnostics".utf8))
            }
        )

        viewModel.begin()
        viewModel.beginCapture()
        try await waitUntil { viewModel.phase == .review }
        let artifactURL = try XCTUnwrap(viewModel.artifact?.archiveURL)
        viewModel.uploadCapturedArtifact()
        await fulfillment(of: [uploadStarted], timeout: 2)

        viewModel.cancelUpload()
        XCTAssertEqual(viewModel.phase, .cancelling)
        try await waitUntil { viewModel.phase == .cancelled }

        XCTAssertTrue(viewModel.hasLocalArtifact)
        XCTAssertTrue(FileManager.default.fileExists(atPath: artifactURL.path))
        XCTAssertEqual(conversionViewModel.state, originalConversionState)
        XCTAssertTrue(uploader.wasCancelled)
    }

    @MainActor
    func testOfflineFailureKeepsFallbackAndRetryCreatesFreshUploadAttempt() async throws {
        let rootDirectory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: rootDirectory) }
        let receipt = DiagnosticReportReceipt(
            supportCode: "BDAVP-FEDCBA9876543210",
            expiresAt: Date(timeIntervalSince1970: 1_790_000_000)
        )
        let uploader = ScriptedDiagnosticUploader(
            outcomes: [
                .failure(.offline),
                .success(receipt),
            ]
        )
        let viewModel = DiagnosticReportViewModel(
            uploader: uploader,
            workspace: DiagnosticArtifactWorkspace(rootDirectory: rootDirectory),
            capture: { directory, _ in
                try Self.makeArtifact(in: directory, data: Data("retry diagnostics".utf8))
            }
        )

        viewModel.begin()
        viewModel.beginCapture()
        try await waitUntil { viewModel.phase == .review }
        viewModel.uploadCapturedArtifact()
        try await waitUntil {
            if case .failed = viewModel.phase { return true }
            return false
        }

        guard case let .failed(failure) = viewModel.phase else {
            return XCTFail("Expected upload failure")
        }
        XCTAssertEqual(failure.stage, .upload)
        XCTAssertEqual(failure.kind, .offline)
        XCTAssertTrue(viewModel.hasLocalArtifact)
        XCTAssertTrue(viewModel.canRetryUpload)
        XCTAssertNil(viewModel.handoffContext)
        XCTAssertNil(viewModel.gitHubIssueDraft())

        viewModel.retryUpload()
        try await waitUntil {
            if case .success = viewModel.phase { return true }
            return false
        }

        guard case let .success(actualReceipt) = viewModel.phase else {
            return XCTFail("Expected retry success")
        }
        XCTAssertEqual(actualReceipt, receipt)
        let attemptCount = await uploader.attemptCount
        XCTAssertEqual(attemptCount, 2)
    }

    @MainActor
    func testCaptureFailureUsesSafeStateWithoutCreatingFallbackArtifact() async throws {
        let rootDirectory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: rootDirectory) }
        let viewModel = DiagnosticReportViewModel(
            uploader: nil,
            workspace: DiagnosticArtifactWorkspace(rootDirectory: rootDirectory),
            capture: { _, _ in
                throw CocoaError(.fileReadNoPermission, userInfo: [NSFilePathErrorKey: "/private/user/path"])
            }
        )

        viewModel.begin()
        viewModel.beginCapture()
        try await waitUntil {
            if case .failed = viewModel.phase { return true }
            return false
        }

        guard case let .failed(failure) = viewModel.phase else {
            return XCTFail("Expected capture failure")
        }
        XCTAssertEqual(failure.stage, .capture)
        XCTAssertFalse(failure.message.contains("/private/user/path"))
        XCTAssertNil(viewModel.artifact)
    }

    @MainActor
    func testBeginEntersComposingBeforeAnyCaptureRuns() async throws {
        let rootDirectory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: rootDirectory) }
        let captureRan = LockedFlag()
        let viewModel = DiagnosticReportViewModel(
            uploader: nil,
            workspace: DiagnosticArtifactWorkspace(rootDirectory: rootDirectory),
            capture: { directory, _ in
                captureRan.set()
                return try Self.makeArtifact(in: directory, data: Data("d".utf8))
            }
        )

        viewModel.begin()

        XCTAssertEqual(viewModel.phase, .composing)
        XCTAssertNil(viewModel.artifact)
        XCTAssertFalse(captureRan.value)
    }

    @MainActor
    func testCancellingCompositionClearsUncapturedDescription() {
        let viewModel = DiagnosticReportViewModel(
            uploader: nil,
            capture: { _, _ in
                XCTFail("Cancelling composition must not capture diagnostics")
                throw CancellationError()
            }
        )

        viewModel.begin()
        viewModel.userDescription = "discard this draft"
        viewModel.prepareForNewDiagnosticSession()

        XCTAssertEqual(viewModel.phase, .idle)
        XCTAssertEqual(viewModel.userDescription, "")
        XCTAssertNil(viewModel.handoffContext)
        XCTAssertNil(viewModel.artifact)
    }

    @MainActor
    func testCancellingCaptureRemovesPartialArtifactAndReturnsToIdle() async throws {
        let rootDirectory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: rootDirectory) }
        let captureStarted = expectation(description: "capture started")
        let workspace = DiagnosticArtifactWorkspace(rootDirectory: rootDirectory)
        let viewModel = DiagnosticReportViewModel(
            uploader: nil,
            workspace: workspace,
            capture: { directory, _ in
                let partialURL = directory.appendingPathComponent("partial.zip")
                try Data("partial".utf8).write(to: partialURL)
                captureStarted.fulfill()
                try await Task.sleep(nanoseconds: 60_000_000_000)
                return try Self.makeArtifact(in: directory, data: Data("complete".utf8))
            }
        )

        viewModel.userDescription = "temporary details"
        viewModel.begin()
        viewModel.beginCapture()
        await fulfillment(of: [captureStarted], timeout: 2)
        viewModel.cancelCapture()
        try await waitUntil { viewModel.phase == .idle }

        XCTAssertNil(viewModel.artifact)
        XCTAssertEqual(viewModel.userDescription, "")
        XCTAssertTrue((try FileManager.default.contentsOfDirectory(atPath: rootDirectory.path)).isEmpty)
    }

    @MainActor
    func testCapturePassesNormalizedDescriptionWithoutExposingHandoffBeforeUpload() async throws {
        let rootDirectory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: rootDirectory) }
        let recorded = LockedComment()
        let handoff = DiagnosticReportHandoffContext(
            appVersion: "1.2.3",
            appBuild: "456",
            capturedStage: "converting",
            redactedDescription: "playback failed"
        )
        let viewModel = DiagnosticReportViewModel(
            uploader: nil,
            workspace: DiagnosticArtifactWorkspace(rootDirectory: rootDirectory),
            capture: { directory, comment in
                recorded.set(comment)
                return try Self.makeArtifact(
                    in: directory,
                    data: Data("d".utf8),
                    handoff: handoff,
                    userDescription: "playback failed"
                )
            }
        )

        viewModel.userDescription = "  playback failed\r\n  "
        viewModel.begin()
        viewModel.beginCapture()
        try await waitUntil { viewModel.phase == .review }

        XCTAssertEqual(recorded.value?.text, "playback failed")
        XCTAssertEqual(viewModel.artifact?.preview.userDescription, "playback failed")
        XCTAssertEqual(viewModel.userDescription, "")
        XCTAssertNil(viewModel.handoffContext)
        XCTAssertNil(viewModel.gitHubIssueDraft())
    }

    @MainActor
    func testSuccessfulUploadOffersGitHubDraftWithAllowlistedFields() async throws {
        let rootDirectory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: rootDirectory) }
        let receipt = DiagnosticReportReceipt(
            supportCode: "BDAVP-0123456789ABCDEF",
            expiresAt: Date(timeIntervalSince1970: 1_790_000_000)
        )
        let handoff = DiagnosticReportHandoffContext(
            appVersion: "1.2.3",
            appBuild: "456",
            capturedStage: "converting",
            redactedDescription: "audio missing"
        )
        let viewModel = DiagnosticReportViewModel(
            uploader: ScriptedDiagnosticUploader(outcomes: [.success(receipt)]),
            workspace: DiagnosticArtifactWorkspace(rootDirectory: rootDirectory),
            capture: { directory, _ in
                try Self.makeArtifact(in: directory, data: Data("d".utf8), handoff: handoff)
            }
        )

        viewModel.begin()
        viewModel.beginCapture()
        try await waitUntil { viewModel.phase == .review }
        viewModel.uploadCapturedArtifact()
        try await waitUntil {
            if case .success = viewModel.phase { return true }
            return false
        }

        let draft = try XCTUnwrap(viewModel.gitHubIssueDraft())
        XCTAssertEqual(draft.supportCode, receipt.supportCode)
        XCTAssertEqual(draft.redactedDescription, "audio missing")
        let url = try XCTUnwrap(draft.url())
        XCTAssertEqual(url.host, "github.com")
        XCTAssertEqual(url.path, "/cbusillo/BD_to_AVP/issues/new")
    }

    @MainActor
    func testSuccessfulUploadWithIncompleteHandoffDoesNotOfferGitHubDraft() async throws {
        let rootDirectory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: rootDirectory) }
        let receipt = DiagnosticReportReceipt(
            supportCode: "BDAVP-0123456789ABCDEF",
            expiresAt: Date(timeIntervalSince1970: 1_790_000_000)
        )
        let viewModel = DiagnosticReportViewModel(
            uploader: ScriptedDiagnosticUploader(outcomes: [.success(receipt)]),
            workspace: DiagnosticArtifactWorkspace(rootDirectory: rootDirectory),
            capture: { directory, _ in
                try Self.makeArtifact(in: directory, data: Data("d".utf8))
            }
        )

        viewModel.begin()
        viewModel.beginCapture()
        try await waitUntil { viewModel.phase == .review }
        viewModel.uploadCapturedArtifact()
        try await waitUntil {
            if case .success = viewModel.phase { return true }
            return false
        }

        XCTAssertNil(viewModel.gitHubIssueDraft())
    }

    @MainActor
    func testLocalOnlyReachesReviewWithoutSuccessOrHandoff() async throws {
        let rootDirectory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: rootDirectory) }
        let viewModel = DiagnosticReportViewModel(
            uploader: nil,
            workspace: DiagnosticArtifactWorkspace(rootDirectory: rootDirectory),
            capture: { directory, _ in
                try Self.makeArtifact(
                    in: directory,
                    data: Data("d".utf8),
                    userDescription: "note kept locally"
                )
            }
        )

        viewModel.userDescription = "note kept locally"
        viewModel.begin()
        viewModel.beginCapture()
        try await waitUntil { viewModel.phase == .review }

        XCTAssertFalse(viewModel.isUploadAvailable)
        XCTAssertEqual(viewModel.artifact?.preview.userDescription, "note kept locally")
        XCTAssertEqual(viewModel.userDescription, "")
        XCTAssertNil(viewModel.handoffContext)
        XCTAssertNil(viewModel.gitHubIssueDraft())
        if case .success = viewModel.phase {
            XCTFail("Local-only capture must not claim a linked private report")
        }
    }

    @MainActor
    func testDiscardClearsDescriptionAndHandoffBeforeNextCapture() async throws {
        let rootDirectory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: rootDirectory) }
        let viewModel = DiagnosticReportViewModel(
            uploader: nil,
            workspace: DiagnosticArtifactWorkspace(rootDirectory: rootDirectory),
            capture: { directory, comment in
                try Self.makeArtifact(
                    in: directory,
                    data: Data("d".utf8),
                    userDescription: comment?.text
                )
            }
        )

        viewModel.userDescription = "first report"
        viewModel.begin()
        viewModel.beginCapture()
        try await waitUntil { viewModel.phase == .review }
        XCTAssertTrue(viewModel.discardLocalCopy())

        XCTAssertEqual(viewModel.phase, .idle)
        XCTAssertEqual(viewModel.userDescription, "")
        XCTAssertNil(viewModel.handoffContext)

        viewModel.begin()
        viewModel.beginCapture()
        try await waitUntil { viewModel.phase == .review }
        XCTAssertNil(viewModel.artifact?.preview.userDescription)
    }

    private func makeDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private static func makeArtifact(
        in directory: URL,
        data: Data,
        handoff: DiagnosticReportHandoffContext = .empty,
        userDescription: String? = nil
    ) throws -> DiagnosticBundleArtifact {
        let archiveURL = directory.appendingPathComponent("diagnostics.zip", isDirectory: false)
        try data.write(to: archiveURL)
        return DiagnosticBundleArtifact(
            bundleID: UUID(),
            createdAt: Date(timeIntervalSince1970: 1_790_000_000),
            archiveURL: archiveURL,
            suggestedFilename: "diagnostics.zip",
            preview: DiagnosticBundlePreview(
                includedCategories: ["worker lifecycle"],
                excludedCategories: ["source media"],
                files: [
                    DiagnosticBundleFilePreview(
                        name: "manifest.json",
                        uncompressedBytes: data.count,
                        truncated: false
                    ),
                ],
                truncationNotices: [],
                archiveBytes: data.count,
                maximumArchiveBytes: 2 * 1_024 * 1_024,
                userDescription: userDescription
            ),
            handoff: handoff
        )
    }

    @MainActor
    private func waitUntil(
        timeout: TimeInterval = 2,
        condition: @escaping @MainActor () -> Bool
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            if Date() >= deadline {
                XCTFail("Timed out waiting for diagnostic report state")
                return
            }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
    }
}

private final class LockedFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var flag = false

    var value: Bool { lock.withLock { flag } }
    func set() { lock.withLock { flag = true } }
}

private final class LockedComment: @unchecked Sendable {
    private let lock = NSLock()
    private var comment: DiagnosticUserComment??

    var value: DiagnosticUserComment? { lock.withLock { comment ?? nil } }
    func set(_ newValue: DiagnosticUserComment?) { lock.withLock { comment = .some(newValue) } }
}

private actor ScriptedDiagnosticUploader: DiagnosticReportUploading {
    enum Outcome: Sendable {
        case success(DiagnosticReportReceipt)
        case failure(DiagnosticReportClientError)
    }

    private var outcomes: [Outcome]
    private(set) var attemptCount = 0
    private(set) var lastProgress: Double?

    init(outcomes: [Outcome]) {
        self.outcomes = outcomes
    }

    func upload(
        artifact: DiagnosticBundleArtifact,
        progress: @escaping @MainActor @Sendable (Double) -> Void
    ) async throws -> DiagnosticReportReceipt {
        attemptCount += 1
        await progress(0.65)
        lastProgress = 0.65
        guard !outcomes.isEmpty else {
            throw DiagnosticReportClientError.serviceUnavailable
        }
        switch outcomes.removeFirst() {
        case let .success(receipt):
            return receipt
        case let .failure(error):
            throw error
        }
    }
}

private final class HoldingDiagnosticUploader: DiagnosticReportUploading, @unchecked Sendable {
    private let lock = NSLock()
    private let onStart: @Sendable () -> Void
    private var storedWasCancelled = false

    init(onStart: @escaping @Sendable () -> Void) {
        self.onStart = onStart
    }

    var wasCancelled: Bool {
        lock.withLock { storedWasCancelled }
    }

    func upload(
        artifact: DiagnosticBundleArtifact,
        progress: @escaping @MainActor @Sendable (Double) -> Void
    ) async throws -> DiagnosticReportReceipt {
        await progress(0.25)
        onStart()
        do {
            try await Task.sleep(nanoseconds: 60_000_000_000)
            throw DiagnosticReportClientError.serviceUnavailable
        } catch {
            lock.withLock { storedWasCancelled = Task.isCancelled }
            throw error
        }
    }
}

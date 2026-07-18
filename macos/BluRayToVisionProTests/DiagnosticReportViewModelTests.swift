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
            capture: { directory in
                try Self.makeArtifact(in: directory, data: Data("local diagnostics".utf8))
            }
        )

        viewModel.begin()
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
            capture: { directory in
                try Self.makeArtifact(in: directory, data: Data("upload diagnostics".utf8))
            }
        )

        viewModel.begin()
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
            capture: { directory in
                try Self.makeArtifact(in: directory, data: Data("cancel diagnostics".utf8))
            }
        )

        viewModel.begin()
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
            capture: { directory in
                try Self.makeArtifact(in: directory, data: Data("retry diagnostics".utf8))
            }
        )

        viewModel.begin()
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
            capture: { _ in
                throw CocoaError(.fileReadNoPermission, userInfo: [NSFilePathErrorKey: "/private/user/path"])
            }
        )

        viewModel.begin()
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

    private func makeDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private static func makeArtifact(in directory: URL, data: Data) throws -> DiagnosticBundleArtifact {
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
                maximumArchiveBytes: 2 * 1_024 * 1_024
            )
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

import Foundation

enum DiagnosticReportPhase: Equatable {
    case idle
    case capturing
    case review
    case uploading(progress: Double)
    case cancelling
    case success(DiagnosticReportReceipt)
    case cancelled
    case failed(DiagnosticReportFailure)

    var isBusy: Bool {
        switch self {
        case .capturing, .uploading, .cancelling:
            return true
        case .idle, .review, .success, .cancelled, .failed:
            return false
        }
    }
}

struct DiagnosticReportFailure: Equatable {
    enum Stage: Equatable {
        case capture
        case upload
    }

    enum Kind: Equatable {
        case offline
        case rateLimited
        case unavailable
        case rejected
        case general
    }

    let stage: Stage
    let kind: Kind
    let title: String
    let message: String

    static func captureFailure() -> DiagnosticReportFailure {
        DiagnosticReportFailure(
            stage: .capture,
            kind: .general,
            title: "Diagnostics Could Not Be Captured",
            message: "The app could not create a privacy-safe diagnostic copy. Try again while this conversion is still available."
        )
    }

    static func uploadFailure(_ error: Error) -> DiagnosticReportFailure {
        guard let clientError = error as? DiagnosticReportClientError else {
            return DiagnosticReportFailure(
                stage: .upload,
                kind: .unavailable,
                title: "Diagnostics Were Not Sent",
                message: DiagnosticReportClientError.serviceUnavailable.localizedDescription
            )
        }
        let kind: Kind
        let title: String
        switch clientError {
        case .offline:
            kind = .offline
            title = "You Appear to Be Offline"
        case .rateLimited:
            kind = .rateLimited
            title = "Please Try Again Shortly"
        case .bundleRejected, .bundleTooLarge:
            kind = .rejected
            title = "The Bundle Was Not Accepted"
        case .timedOut, .serviceUnavailable, .authorizationExpired:
            kind = .unavailable
            title = "Diagnostics Were Not Sent"
        case .unsafeServerResponse, .cannotReadBundle:
            kind = .general
            title = "Diagnostics Were Not Sent"
        case .cancelled:
            kind = .general
            title = "Upload Cancelled"
        }
        return DiagnosticReportFailure(
            stage: .upload,
            kind: kind,
            title: title,
            message: clientError.localizedDescription
        )
    }
}

struct DiagnosticArtifactWorkspace {
    private let fileManager: FileManager
    private let rootDirectory: URL

    init(
        fileManager: FileManager = .default,
        rootDirectory: URL? = nil
    ) {
        self.fileManager = fileManager
        self.rootDirectory = rootDirectory
            ?? fileManager.temporaryDirectory
                .appendingPathComponent("com.shinycomputers.bd-to-avp", isDirectory: true)
                .appendingPathComponent("Diagnostic Reports", isDirectory: true)
    }

    func makeCaptureDirectory() throws -> URL {
        let directory = rootDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    func removeCaptureDirectory(_ directory: URL?) throws {
        guard let directory, fileManager.fileExists(atPath: directory.path) else {
            return
        }
        try fileManager.removeItem(at: directory)
    }
}

@MainActor
final class DiagnosticReportViewModel: ObservableObject {
    typealias Capture = @MainActor (URL) async throws -> DiagnosticBundleArtifact

    @Published private(set) var phase = DiagnosticReportPhase.idle
    @Published private(set) var artifact: DiagnosticBundleArtifact?
    @Published private(set) var lastSavedCopyURL: URL?
    @Published private(set) var exportErrorMessage: String?

    let isUploadAvailable: Bool

    private let uploader: (any DiagnosticReportUploading)?
    private let workspace: DiagnosticArtifactWorkspace
    private let capture: Capture
    private var captureDirectory: URL?
    private var captureTask: Task<Void, Never>?
    private var uploadTask: Task<Void, Never>?

    init(
        uploader: (any DiagnosticReportUploading)?,
        workspace: DiagnosticArtifactWorkspace = DiagnosticArtifactWorkspace(),
        capture: @escaping Capture
    ) {
        self.uploader = uploader
        self.workspace = workspace
        self.capture = capture
        isUploadAvailable = uploader != nil
    }

    deinit {
        captureTask?.cancel()
        uploadTask?.cancel()
    }

    var hasLocalArtifact: Bool {
        artifact != nil
    }

    var shareURL: URL? {
        artifact?.archiveURL
    }

    var canRetryUpload: Bool {
        guard isUploadAvailable, artifact != nil else {
            return false
        }
        switch phase {
        case .cancelled:
            return true
        case let .failed(failure):
            return failure.stage == .upload
        case .idle, .capturing, .review, .uploading, .cancelling, .success:
            return false
        }
    }

    func begin() {
        guard !phase.isBusy else {
            return
        }
        switch phase {
        case .idle:
            captureNew()
        case let .failed(failure) where failure.stage == .capture && artifact == nil:
            captureNew()
        case .capturing, .review, .uploading, .cancelling, .success, .cancelled, .failed:
            break
        }
    }

    func prepareForNewDiagnosticSession() {
        guard !phase.isBusy, artifact == nil else {
            return
        }
        phase = .idle
        lastSavedCopyURL = nil
        exportErrorMessage = nil
    }

    func captureNew() {
        guard !phase.isBusy, artifact == nil else {
            return
        }
        discardLocalCopySilently()
        lastSavedCopyURL = nil
        exportErrorMessage = nil
        phase = .capturing

        captureTask = Task { [weak self] in
            guard let self else {
                return
            }
            do {
                let directory = try workspace.makeCaptureDirectory()
                captureDirectory = directory
                let artifact = try await capture(directory)
                try Task.checkCancellation()
                self.artifact = artifact
                phase = .review
            } catch is CancellationError {
                discardLocalCopySilently()
                phase = .idle
            } catch {
                discardLocalCopySilently()
                phase = .failed(.captureFailure())
            }
            captureTask = nil
        }
    }

    func cancelCapture() {
        captureTask?.cancel()
    }

    func uploadCapturedArtifact() {
        guard uploadTask == nil,
              let uploader,
              let artifact
        else {
            return
        }
        exportErrorMessage = nil
        phase = .uploading(progress: 0)

        uploadTask = Task { [weak self] in
            guard let self else {
                return
            }
            do {
                let receipt = try await uploader.upload(artifact: artifact) { [weak self] progress in
                    guard let self,
                          case .uploading = self.phase
                    else {
                        return
                    }
                    self.phase = .uploading(progress: min(max(progress, 0), 1))
                }
                phase = .success(receipt)
                cleanupAfterSuccess()
            } catch is CancellationError {
                phase = .cancelled
            } catch DiagnosticReportClientError.cancelled {
                phase = .cancelled
            } catch {
                phase = .failed(.uploadFailure(error))
            }
            uploadTask = nil
        }
    }

    func retryUpload() {
        guard canRetryUpload else {
            return
        }
        uploadCapturedArtifact()
    }

    func cancelUpload() {
        guard uploadTask != nil else {
            return
        }
        phase = .cancelling
        uploadTask?.cancel()
    }

    @discardableResult
    func saveCopy(to destinationURL: URL) -> Bool {
        guard let artifact else {
            return false
        }
        do {
            lastSavedCopyURL = try artifact.saveCopy(to: destinationURL, overwrite: true)
            exportErrorMessage = nil
            return true
        } catch {
            exportErrorMessage = "The diagnostic copy could not be saved. Choose another location and try again."
            return false
        }
    }

    @discardableResult
    func discardLocalCopy() -> Bool {
        guard !phase.isBusy else {
            return false
        }
        guard let artifact else {
            try? workspace.removeCaptureDirectory(captureDirectory)
            captureDirectory = nil
            phase = .idle
            return true
        }
        do {
            try artifact.removeLocalCopy()
            self.artifact = nil
            try? workspace.removeCaptureDirectory(captureDirectory)
            captureDirectory = nil
            exportErrorMessage = nil
            if case .success = phase {
                return true
            }
            phase = .idle
            return true
        } catch {
            exportErrorMessage = "The local diagnostic copy could not be removed. It remains available to save or share."
            return false
        }
    }

    func clearExportError() {
        exportErrorMessage = nil
    }

    private func cleanupAfterSuccess() {
        guard let artifact else {
            return
        }
        do {
            try artifact.removeLocalCopy()
            self.artifact = nil
            try? workspace.removeCaptureDirectory(captureDirectory)
            captureDirectory = nil
        } catch {
            exportErrorMessage = "The report was sent, but its local copy could not be removed. You can discard it below."
        }
    }

    private func discardLocalCopySilently() {
        if let artifact {
            try? artifact.removeLocalCopy()
        }
        try? workspace.removeCaptureDirectory(captureDirectory)
        artifact = nil
        captureDirectory = nil
    }
}

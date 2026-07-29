import AppKit
import AVFoundation
import AVKit
import SwiftUI

struct PreviewPresentationSmokeConfiguration: Equatable {
    let mediaURL: URL
    let resultURL: URL

    static func parse(arguments: [String]) -> PreviewPresentationSmokeConfiguration? {
        guard let argumentIndex = arguments.firstIndex(of: AppDelegate.previewPresentationSmokeArgument),
              arguments.indices.contains(argumentIndex + 2) else {
            return nil
        }
        let mediaPath = arguments[argumentIndex + 1]
        let resultPath = arguments[argumentIndex + 2]
        guard !mediaPath.isEmpty, !resultPath.isEmpty else {
            return nil
        }
        return PreviewPresentationSmokeConfiguration(
            mediaURL: URL(fileURLWithPath: mediaPath).standardizedFileURL,
            resultURL: URL(fileURLWithPath: resultPath).standardizedFileURL
        )
    }
}

@MainActor
final class PreviewPresentationSmokeController: ObservableObject {
    @Published private(set) var lease: PreviewArtifactLease?

    private let configuration: PreviewPresentationSmokeConfiguration
    private let initializationError: String?
    private var completionStarted = false

    init(configuration: PreviewPresentationSmokeConfiguration, fileManager: FileManager = .default) {
        self.configuration = configuration
        let mediaURL = configuration.mediaURL
        let directoryURL = mediaURL.deletingLastPathComponent()
        let rootURL = directoryURL.deletingLastPathComponent()
        let cache = PreviewCache(rootURL: rootURL, fileManager: fileManager)

        guard fileManager.fileExists(atPath: mediaURL.path), cache.contains(mediaURL, in: directoryURL) else {
            lease = nil
            initializationError = "The preview smoke media is missing or outside its isolated workspace."
            return
        }

        let attributes = try? fileManager.attributesOfItem(atPath: mediaURL.path)
        let sizeBytes = (attributes?[.size] as? NSNumber)?.int64Value ?? 0
        let artifact = PreviewArtifact(
            sourcePath: mediaURL.path,
            destinationPath: directoryURL.path,
            outputPath: mediaURL.path,
            sizeBytes: sizeBytes,
            parentJobID: UUID(),
            position: "middle",
            startSeconds: 0,
            durationSeconds: 1,
            sourceDurationSeconds: 1
        )
        lease = PreviewArtifactLease(
            artifact: artifact,
            directoryURL: directoryURL,
            cache: cache
        )
        initializationError = nil
    }

    func viewAttached(playerView: AVPlayerView?) {
        guard !completionStarted else {
            return
        }
        completionStarted = true
        Task {
            await finish(playerView: playerView)
        }
    }

    private func finish(playerView: AVPlayerView?) async {
        guard initializationError == nil, lease != nil else {
            writeReceipt(
                playerPresented: false,
                mediaReady: false,
                cleanupComplete: false,
                message: initializationError ?? "The preview smoke could not create an artifact lease."
            )
            NSApp.terminate(nil)
            return
        }

        guard let playerView else {
            let cleanupComplete = await releaseLeaseAndWaitForCleanup()
            writeReceipt(
                playerPresented: false,
                mediaReady: false,
                cleanupComplete: cleanupComplete,
                message: PreviewPresentationSmokeError.playerViewNotFound.localizedDescription
            )
            NSApp.terminate(nil)
            return
        }

        do {
            try await waitForMediaReady(playerView)
            let cleanupComplete = await releaseLeaseAndWaitForCleanup()
            writeReceipt(
                playerPresented: true,
                mediaReady: true,
                cleanupComplete: cleanupComplete,
                message: cleanupComplete ? nil : "The preview artifact workspace was not removed before the deadline."
            )
        } catch {
            let cleanupComplete = await releaseLeaseAndWaitForCleanup()
            writeReceipt(
                playerPresented: true,
                mediaReady: false,
                cleanupComplete: cleanupComplete,
                message: error.localizedDescription
            )
        }
        NSApp.terminate(nil)
    }

    private func waitForMediaReady(_ playerView: AVPlayerView) async throws {
        for _ in 0..<100 {
            if let currentItem = playerView.player?.currentItem {
                switch currentItem.status {
                case .readyToPlay:
                    return
                case .failed:
                    throw PreviewPresentationSmokeError.mediaFailed(
                        currentItem.error?.localizedDescription ?? "AVPlayer rejected the preview smoke media."
                    )
                case .unknown:
                    break
                @unknown default:
                    break
                }
            }
            try await Task.sleep(for: .milliseconds(100))
        }
        throw PreviewPresentationSmokeError.mediaReadinessTimedOut
    }

    private func releaseLeaseAndWaitForCleanup() async -> Bool {
        guard let cleanupContext = releaseLease() else {
            return false
        }
        return await waitForRemoval(cleanupContext.directoryURL, fileManager: cleanupContext.fileManager)
    }

    private func releaseLease() -> (directoryURL: URL, fileManager: FileManager)? {
        guard let lease else {
            return nil
        }
        let cleanupContext = (directoryURL: lease.directoryURL, fileManager: lease.fileManager)
        self.lease = nil
        return cleanupContext
    }

    private func waitForRemoval(_ directoryURL: URL, fileManager: FileManager) async -> Bool {
        for _ in 0..<50 {
            if !fileManager.fileExists(atPath: directoryURL.path) {
                return true
            }
            try? await Task.sleep(for: .milliseconds(100))
        }
        return !fileManager.fileExists(atPath: directoryURL.path)
    }

    private func writeReceipt(
        playerPresented: Bool,
        mediaReady: Bool,
        cleanupComplete: Bool,
        message: String?
    ) {
        let receipt = PreviewPresentationSmokeReceipt(
            schemaVersion: 1,
            playerPresented: playerPresented,
            mediaReady: mediaReady,
            cleanupComplete: cleanupComplete,
            message: message
        )
        guard let data = try? JSONEncoder().encode(receipt) else {
            return
        }
        try? data.write(to: configuration.resultURL, options: .atomic)
    }
}

struct PreviewPresentationSmokeView: View {
    @StateObject private var controller: PreviewPresentationSmokeController

    init(configuration: PreviewPresentationSmokeConfiguration) {
        _controller = StateObject(
            wrappedValue: PreviewPresentationSmokeController(configuration: configuration)
        )
    }

    var body: some View {
        Group {
            if let lease = controller.lease {
                PreviewPlayerView(
                    lease: lease,
                    videoRoute: nil,
                    generateAgain: {},
                    removePreview: {},
                    startFullConversion: {}
                )
                .background(PreviewPresentationAttachmentProbe(onAttached: controller.viewAttached))
            } else {
                ProgressView("Completing preview smoke…")
                    .onAppear {
                        controller.viewAttached(playerView: nil)
                    }
            }
        }
        .padding(22)
    }
}

private struct PreviewPresentationSmokeReceipt: Codable {
    let schemaVersion: Int
    let playerPresented: Bool
    let mediaReady: Bool
    let cleanupComplete: Bool
    let message: String?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case playerPresented = "player_presented"
        case mediaReady = "media_ready"
        case cleanupComplete = "cleanup_complete"
        case message
    }
}

private enum PreviewPresentationSmokeError: LocalizedError {
    case playerViewNotFound
    case mediaFailed(String)
    case mediaReadinessTimedOut

    var errorDescription: String? {
        switch self {
        case .playerViewNotFound:
            "The existing preview player did not attach an AVPlayerView before the deadline."
        case let .mediaFailed(message):
            message
        case .mediaReadinessTimedOut:
            "The existing preview player did not make the smoke media ready before the deadline."
        }
    }
}

private struct PreviewPresentationAttachmentProbe: NSViewRepresentable {
    let onAttached: @MainActor (AVPlayerView?) -> Void

    func makeNSView(context: Context) -> ProbeView {
        ProbeView(onAttached: onAttached)
    }

    func updateNSView(_ nsView: ProbeView, context: Context) {}

    final class ProbeView: NSView {
        private let onAttached: @MainActor (AVPlayerView?) -> Void
        private var hasAttached = false

        init(onAttached: @escaping @MainActor (AVPlayerView?) -> Void) {
            self.onAttached = onAttached
            super.init(frame: .zero)
        }

        @available(*, unavailable)
        required init?(coder: NSCoder) {
            nil
        }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            guard window != nil, !hasAttached else {
                return
            }
            hasAttached = true
            Task { @MainActor [weak self] in
                for _ in 0..<50 {
                    guard let self else {
                        return
                    }
                    if let contentView = window?.contentView,
                       let playerView = findPlayerView(in: contentView) {
                        onAttached(playerView)
                        return
                    }
                    try? await Task.sleep(for: .milliseconds(100))
                }
                onAttached(nil)
            }
        }

        private func findPlayerView(in view: NSView) -> AVPlayerView? {
            if let playerView = view as? AVPlayerView {
                return playerView
            }
            for subview in view.subviews {
                if let playerView = findPlayerView(in: subview) {
                    return playerView
                }
            }
            return nil
        }
    }
}

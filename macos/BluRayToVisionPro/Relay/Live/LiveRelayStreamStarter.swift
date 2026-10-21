import Darwin
import Foundation

enum LiveRelayStreamStartError: Error, Equatable {
    case workspaceUnavailable
}

/// Starts real live streams: a fresh workspace under the app's caches and a dedicated worker
/// process per stream.
struct LiveRelayStreamStarter: LiveRelayStreamStarting {
    typealias RunnerFactory = @Sendable (_ configuration: LiveRelayStreamConfiguration) throws
        -> any WorkerProcessRunning & Sendable

    /// `~/Library/Caches/<bundle>/LiveHLS`. A launch sweep may remove whatever is left here.
    let workspaceRoot: URL
    let configuration: LiveRelayStreamConfiguration
    let makeRunner: RunnerFactory
    let sleepAssertion: LiveRelaySleepAssertion

    init(
        workspaceRoot: URL,
        configuration: LiveRelayStreamConfiguration = LiveRelayStreamConfiguration(),
        sleepAssertion: LiveRelaySleepAssertion = .system,
        makeRunner: @escaping RunnerFactory
    ) {
        self.workspaceRoot = workspaceRoot.standardizedFileURL
        self.configuration = configuration
        self.sleepAssertion = sleepAssertion
        self.makeRunner = makeRunner
    }

    static func defaultWorkspaceRoot(fileManager: FileManager = .default) throws -> URL {
        let caches = try fileManager.url(for: .cachesDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
        return caches
            .appendingPathComponent(Bundle.main.bundleIdentifier ?? "com.shinycomputers.bd-to-avp", isDirectory: true)
            .appendingPathComponent("LiveHLS", isDirectory: true)
    }

    /// The production starter: the app's own worker, killed `workerExitGrace` after a cancel.
    static func automatic(configuration: LiveRelayStreamConfiguration = LiveRelayStreamConfiguration()) throws
        -> LiveRelayStreamStarter {
        let launch = try WorkerLaunchConfiguration.automatic()
        return LiveRelayStreamStarter(workspaceRoot: try defaultWorkspaceRoot(), configuration: configuration) {
            WorkerProcessClient(configuration: launch, cancellationEscalationDelay: $0.workerExitGrace)
        }
    }

    func start(_ request: LiveStreamRequest, id: UUID) async throws -> any LiveRelayStreaming {
        try Task.checkCancellation()
        let workspaceURL = try makeWorkspaceURL(for: id)
        let stream = LiveRelayStream(
            id: id,
            request: request,
            workspaceURL: workspaceURL,
            runner: try makeRunner(configuration),
            configuration: configuration,
            sleepAssertion: sleepAssertion
        )
        await stream.launch()
        if Task.isCancelled {
            await stream.stop(.cancelled)
            throw CancellationError()
        }
        return stream
    }

    /// A path that does not exist yet, under a private root. The worker creates the directory.
    private func makeWorkspaceURL(for id: UUID) throws -> URL {
        do {
            try FileManager.default.createDirectory(
                at: workspaceRoot,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
        } catch {
            throw LiveRelayStreamStartError.workspaceUnavailable
        }
        var info = stat()
        guard lstat(workspaceRoot.path, &info) == 0, info.st_mode & S_IFMT == S_IFDIR else {
            throw LiveRelayStreamStartError.workspaceUnavailable
        }
        let workspaceURL = workspaceRoot.appendingPathComponent(id.uuidString.lowercased(), isDirectory: true)
        guard lstat(workspaceURL.path, &info) != 0, errno == ENOENT else {
            throw LiveRelayStreamStartError.workspaceUnavailable
        }
        return workspaceURL
    }
}

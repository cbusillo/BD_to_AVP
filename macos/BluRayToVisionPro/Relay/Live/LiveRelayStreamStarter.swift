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

    /// The launch sweep, started once per app run. Sharing awaits it before it enables live
    /// services, so the sweep never races a new workspace or handoff socket of this instance.
    static let leftoverSweep: Task<Void, Never> = Task.detached(priority: .utility) {
        sweepLeftovers()
    }

    /// Removes live workspaces and handoff socket directories that no running app owns. Each one
    /// carries its creator's pid (`LiveLeftoverOwner`); one whose owner is still running, such as
    /// another copy of the app, is kept. Only files are touched.
    static func sweepLeftovers(
        workspaceRoot: URL? = try? defaultWorkspaceRoot(),
        temporaryDirectory: URL = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true),
        isRunning: (pid_t) -> Bool = LiveLeftoverOwner.isRunning,
        now: Date = Date(),
        fileManager: FileManager = .default
    ) {
        if let workspaceRoot, let children = try? fileManager.contentsOfDirectory(at: workspaceRoot, includingPropertiesForKeys: nil) {
            let markers = Set(children.filter { $0.pathExtension == LiveLeftoverOwner.markerExtension }.map(\.lastPathComponent))
            for child in children where child.pathExtension != LiveLeftoverOwner.markerExtension {
                let marker = workspaceRoot.appendingPathComponent(child.lastPathComponent + "." + LiveLeftoverOwner.markerExtension)
                if let owner = LiveLeftoverOwner.read(marker), isRunning(owner) { continue }
                try? fileManager.removeItem(at: child)
                try? fileManager.removeItem(at: marker)
            }
            // A marker whose workspace never appeared, or is already gone.
            for name in markers {
                let marker = workspaceRoot.appendingPathComponent(name)
                let workspace = marker.deletingPathExtension()
                guard !fileManager.fileExists(atPath: workspace.path) else { continue }
                if let owner = LiveLeftoverOwner.read(marker), isRunning(owner) { continue }
                try? fileManager.removeItem(at: marker)
            }
        }
        let handoffs = (try? fileManager.contentsOfDirectory(at: temporaryDirectory, includingPropertiesForKeys: [.contentModificationDateKey])) ?? []
        for child in handoffs where child.lastPathComponent.hasPrefix("bdtoavp-") && child.lastPathComponent.count == 14 {
            if let owner = LiveLeftoverOwner.read(child.appendingPathComponent(LiveLeftoverOwner.handoffMarkerName)) {
                if isRunning(owner) { continue }
            } else {
                // No marker yet: another instance may be between mkdtemp and writing it.
                let modified = (try? child.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? .distantPast
                if now.timeIntervalSince(modified) < 60 { continue }
            }
            try? fileManager.removeItem(at: child)
        }
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
        // A lease that holds an open descriptor hands it to the worker, so the worker never
        // opens the source path itself (the app holds the folder's consent, the worker does not).
        let handoff = request.source.retainer is LiveSourceDescriptorHolding ? try LiveSourceHandoff() : nil
        let stream = LiveRelayStream(
            id: id,
            request: request,
            workspaceURL: workspaceURL,
            runner: try makeRunner(configuration),
            handoff: handoff,
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
        // Mark it as this instance's before the worker creates it, so a sweep by another copy of
        // the app never removes it. The stream's cleanup removes the marker with the workspace.
        guard LiveLeftoverOwner.write(LiveLeftoverOwner.marker(forWorkspace: workspaceURL)) else {
            throw LiveRelayStreamStartError.workspaceUnavailable
        }
        return workspaceURL
    }
}

/// The pid marker that ties a live workspace or handoff directory to the app instance that made
/// it, so the launch sweep removes only what no running instance owns.
enum LiveLeftoverOwner {
    static let markerExtension = "owner"
    static let handoffMarkerName = "owner"

    static func marker(forWorkspace workspace: URL) -> URL {
        workspace.deletingLastPathComponent()
            .appendingPathComponent(workspace.lastPathComponent + "." + markerExtension)
    }

    @discardableResult
    static func write(_ url: URL, pid: pid_t = getpid()) -> Bool {
        (try? Data("\(pid)\n".utf8).write(to: url, options: .atomic)) != nil
    }

    static func read(_ url: URL) -> pid_t? {
        guard let data = try? Data(contentsOf: url), data.count <= 32,
              let text = String(data: data, encoding: .utf8),
              let pid = pid_t(text.trimmingCharacters(in: .whitespacesAndNewlines)), pid > 0
        else { return nil }
        return pid
    }

    /// Whether a process with this pid exists. An unrelated process that reused the pid keeps a
    /// leftover until the next launch, which is the safe direction.
    static func isRunning(_ pid: pid_t) -> Bool {
        kill(pid, 0) == 0 || errno == EPERM
    }
}

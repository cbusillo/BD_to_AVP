import AppKit
import Foundation

struct WorkerCancellationSmokeConfiguration: Equatable {
    let workerURL: URL
    let destinationURL: URL
    let resultURL: URL

    static func parse(arguments: [String]) -> WorkerCancellationSmokeConfiguration? {
        guard let argumentIndex = arguments.firstIndex(of: AppDelegate.workerCancellationSmokeArgument),
              arguments.indices.contains(argumentIndex + 3) else {
            return nil
        }
        let workerPath = arguments[argumentIndex + 1]
        let destinationPath = arguments[argumentIndex + 2]
        let resultPath = arguments[argumentIndex + 3]
        guard !workerPath.isEmpty, !destinationPath.isEmpty, !resultPath.isEmpty else {
            return nil
        }
        return WorkerCancellationSmokeConfiguration(
            workerURL: URL(fileURLWithPath: workerPath).standardizedFileURL,
            destinationURL: URL(fileURLWithPath: destinationPath).standardizedFileURL,
            resultURL: URL(fileURLWithPath: resultPath).standardizedFileURL
        )
    }
}

private actor WorkerCancellationSmokeState {
    private(set) var workerReady = false

    func markWorkerReady() -> Bool {
        guard !workerReady else {
            return false
        }
        workerReady = true
        return true
    }
}

private struct WorkerCancellationSmokeReceipt: Codable {
    let schemaVersion: Int
    let workerReady: Bool
    let terminalCancelled: Bool
    let childReaped: Bool
    let cleanupComplete: Bool
    let message: String?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case workerReady = "worker_ready"
        case terminalCancelled = "terminal_cancelled"
        case childReaped = "child_reaped"
        case cleanupComplete = "cleanup_complete"
        case message
    }
}

@MainActor
enum WorkerCancellationSmoke {
    static func run(
        configuration: WorkerCancellationSmokeConfiguration,
        fileManager: FileManager = .default
    ) async {
        var workerReady = false
        var terminalCancelled = false
        var childReaped = false
        var cleanupComplete = false
        var message: String?
        var cleanupContext: (cache: PreviewCache, directoryURL: URL)?

        do {
            try fileManager.createDirectory(at: configuration.destinationURL, withIntermediateDirectories: true)
            let cache = PreviewCache.destinationScoped(
                destinationURL: configuration.destinationURL,
                fileManager: fileManager
            )
            let jobID = UUID()
            let directoryURL = try cache.prepareDirectory(jobID: jobID)
            cleanupContext = (cache, directoryURL)
            let sourceURL = directoryURL.appendingPathComponent("source.m2ts")
            let markerURL = directoryURL.appendingPathComponent("child-reaped.txt")
            guard fileManager.createFile(atPath: sourceURL.path, contents: Data("smoke".utf8)) else {
                throw CocoaError(.fileWriteUnknown)
            }

            var environment = ProcessInfo.processInfo.environment
            environment["BD_TO_AVP_CANCELLATION_SMOKE_WORKSPACE"] = directoryURL.path
            environment["BD_TO_AVP_CANCELLATION_SMOKE_MARKER"] = markerURL.path
            environment["BD_TO_AVP_CANCELLATION_SMOKE_RESULT"] = configuration.resultURL.path
            let client = WorkerProcessClient(
                configuration: WorkerLaunchConfiguration(
                    executableURL: configuration.workerURL,
                    arguments: [],
                    currentDirectoryURL: directoryURL,
                    environment: environment
                )
            )
            let state = WorkerCancellationSmokeState()
            let result = try await client.run(
                job: WorkerJobSpec(sourceURL: sourceURL, jobID: jobID)
            ) { event in
                if event.type == .workerReady, await state.markWorkerReady() {
                    client.cancel()
                }
            }
            workerReady = await state.workerReady
            terminalCancelled = result.terminalEvent.type == .jobCancelled
            childReaped = fileManager.fileExists(atPath: markerURL.path)
        } catch {
            message = error.localizedDescription
        }

        if let cleanupContext {
            cleanupComplete = await PreviewCleanup.remove(
                cache: cleanupContext.cache,
                directoryURL: cleanupContext.directoryURL
            )
        }
        let receipt = WorkerCancellationSmokeReceipt(
            schemaVersion: 1,
            workerReady: workerReady,
            terminalCancelled: terminalCancelled,
            childReaped: childReaped,
            cleanupComplete: cleanupComplete,
            message: message
        )
        if let data = try? JSONEncoder().encode(receipt) {
            try? fileManager.createDirectory(
                at: configuration.resultURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try? data.write(to: configuration.resultURL, options: .atomic)
        }
    }
}

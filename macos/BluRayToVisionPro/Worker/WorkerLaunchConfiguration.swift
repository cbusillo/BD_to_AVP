import Foundation

enum WorkerLaunchConfigurationError: Error, LocalizedError, Equatable {
    case missingBundledWorker
    case missingRepositoryRoot
    case missingUV

    var errorDescription: String? {
        switch self {
        case .missingBundledWorker:
            return "The conversion engine is missing. Reinstall the application and try again."
        case .missingRepositoryRoot:
            return "A required app component could not be found."
        case .missingUV:
            return "A required app component could not start."
        }
    }
}

struct WorkerLaunchConfiguration: Equatable {
    static let packagedExecutableName = "BluRayToVisionProEngine"

    let executableURL: URL
    let arguments: [String]
    let currentDirectoryURL: URL?
    let environment: [String: String]

    static func automatic(
        bundle: Bundle = .main,
        fileManager: FileManager = .default,
        processEnvironment: [String: String] = ProcessInfo.processInfo.environment
    ) throws -> WorkerLaunchConfiguration {
        let packagedWorker = bundle.bundleURL
            .appendingPathComponent("Contents/MacOS", isDirectory: true)
            .appendingPathComponent(packagedExecutableName)
        if fileManager.isExecutableFile(atPath: packagedWorker.path) {
            return WorkerLaunchConfiguration(
                executableURL: packagedWorker,
                arguments: [],
                currentDirectoryURL: nil,
                environment: sanitizedEnvironment(from: processEnvironment)
            )
        }

        #if !DEBUG
        throw WorkerLaunchConfigurationError.missingBundledWorker
        #else
        if bundle.object(forInfoDictionaryKey: "BluRayToVisionProEngineBundled") as? Bool == true {
            throw WorkerLaunchConfigurationError.missingBundledWorker
        }

        let configuredRoot = processEnvironment["BD_TO_AVP_REPO_ROOT"]
            ?? developmentRepositoryRoot
        guard let configuredRoot, !configuredRoot.isEmpty else {
            throw WorkerLaunchConfigurationError.missingRepositoryRoot
        }

        let repositoryURL = URL(fileURLWithPath: configuredRoot, isDirectory: true).standardizedFileURL
        guard fileManager.fileExists(atPath: repositoryURL.appendingPathComponent("pyproject.toml").path) else {
            throw WorkerLaunchConfigurationError.missingRepositoryRoot
        }
        let developmentWorker = repositoryURL
            .appendingPathComponent(".venv/bin", isDirectory: true)
            .appendingPathComponent("bd-to-avp-worker")
        if fileManager.isExecutableFile(atPath: developmentWorker.path) {
            return WorkerLaunchConfiguration(
                executableURL: developmentWorker,
                arguments: [],
                currentDirectoryURL: repositoryURL,
                environment: sanitizedEnvironment(from: processEnvironment)
            )
        }
        guard let uvURL = findUV(fileManager: fileManager, environment: processEnvironment) else {
            throw WorkerLaunchConfigurationError.missingUV
        }

        return WorkerLaunchConfiguration(
            executableURL: uvURL,
            arguments: [
                "run",
                "--project",
                repositoryURL.path,
                "--no-sync",
                "bd-to-avp-worker",
            ],
            currentDirectoryURL: repositoryURL,
            environment: sanitizedEnvironment(from: processEnvironment)
        )
        #endif
    }

    static func sanitizedEnvironment(from environment: [String: String]) -> [String: String] {
        var workerEnvironment = environment
        for key in workerEnvironment.keys where key.hasPrefix("PYTHON")
            || key.hasPrefix("DYLD_")
            || key.hasPrefix("BD_TO_AVP_")
        {
            workerEnvironment.removeValue(forKey: key)
        }
        workerEnvironment["PYTHONUNBUFFERED"] = "1"
        return workerEnvironment
    }

    private static var developmentRepositoryRoot: String? {
        #if DEBUG
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .path
        #else
        nil
        #endif
    }

    private static func findUV(fileManager: FileManager, environment: [String: String]) -> URL? {
        var candidates: [String] = []
        if let configuredPath = environment["BD_TO_AVP_UV_EXECUTABLE"] {
            candidates.append(configuredPath)
        }
        candidates.append(contentsOf: [
            "/opt/homebrew/bin/uv",
            "/usr/local/bin/uv",
            FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin/uv").path,
        ])
        if let path = environment["PATH"] {
            candidates.append(contentsOf: path.split(separator: ":").map { "\($0)/uv" })
        }

        return candidates
            .map { URL(fileURLWithPath: $0) }
            .first { fileManager.isExecutableFile(atPath: $0.path) }
    }
}

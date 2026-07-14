import Darwin
import Foundation

enum WorkerClientError: Error, LocalizedError {
    case alreadyRunning
    case requestEncoding(String)
    case launch(String)
    case protocolFailure(message: String, diagnostics: String)
    case missingTerminalEvent(exitStatus: Int32, diagnostics: String)
    case unexpectedExit(exitStatus: Int32, diagnostics: String)

    var errorDescription: String? {
        switch self {
        case .alreadyRunning:
            return "Another activity is already in progress."
        case .requestEncoding:
            return "The app couldn’t prepare the source analysis."
        case .launch:
            return "A required app component could not start."
        case .protocolFailure:
            return "A required app component returned an invalid response."
        case .missingTerminalEvent:
            return "The source analysis ended before results were available."
        case .unexpectedExit:
            return "The source analysis ended unexpectedly."
        }
    }

    var technicalDetails: String? {
        switch self {
        case let .requestEncoding(message), let .launch(message):
            return message
        case let .protocolFailure(message, diagnostics):
            return [message, diagnostics].filter { !$0.isEmpty }.joined(separator: "\n\n")
        case let .missingTerminalEvent(exitStatus, diagnostics),
             let .unexpectedExit(exitStatus, diagnostics):
            return ["Exit status: \(exitStatus)", diagnostics].filter { !$0.isEmpty }.joined(separator: "\n\n")
        case .alreadyRunning:
            return nil
        }
    }

    var diagnostics: String? { technicalDetails }
}

private enum WorkerStreamError: Error, LocalizedError {
    case expectedWorkerReady(received: WorkerEventType)
    case invalidProcessGroup(received: Int32?)
    case duplicateWorkerReady

    var errorDescription: String? {
        switch self {
        case let .expectedWorkerReady(received):
            return "The first worker event was \(received.rawValue), not worker.ready."
        case let .invalidProcessGroup(received):
            return "The worker reported an invalid process group: \(received.map(String.init) ?? "missing")."
        case .duplicateWorkerReady:
            return "The worker sent worker.ready more than once."
        }
    }
}

struct WorkerRunResult {
    let terminalEvent: WorkerEvent
    let exitStatus: Int32
    let diagnostics: String
}

protocol WorkerProcessRunning: AnyObject {
    func run(
        job: WorkerJobSpec,
        onEvent: @escaping (WorkerEvent) async throws -> Void
    ) async throws -> WorkerRunResult
    func cancel()
}

final class WorkerProcessClient: WorkerProcessRunning, @unchecked Sendable {
    typealias EventHandler = (WorkerEvent) async throws -> Void

    private static let ioQueue = DispatchQueue(
        label: "com.shinycomputers.bd-to-avp.worker-io",
        qos: .utility,
        attributes: .concurrent
    )

    private let configuration: WorkerLaunchConfiguration
    private let stateLock = NSLock()
    private var activeProcess: Process?
    private var activeProcessGroupID: pid_t?
    private var cancellationRequested = false

    init(configuration: WorkerLaunchConfiguration) {
        self.configuration = configuration
    }

    func run(job: WorkerJobSpec, onEvent: @escaping EventHandler) async throws -> WorkerRunResult {
        let process = Process()
        let standardInput = Pipe()
        let standardOutput = Pipe()
        let standardError = Pipe()
        _ = fcntl(standardInput.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1)

        process.executableURL = configuration.executableURL
        process.arguments = configuration.arguments
        process.currentDirectoryURL = configuration.currentDirectoryURL
        process.environment = configuration.environment
        process.standardInput = standardInput
        process.standardOutput = standardOutput
        process.standardError = standardError
        let exitWaiter = ProcessExitWaiter()
        process.terminationHandler = { terminatedProcess in
            exitWaiter.complete(with: terminatedProcess.terminationStatus)
        }

        guard register(process) else {
            throw WorkerClientError.alreadyRunning
        }
        defer {
            clear(process)
        }

        do {
            try process.run()
        } catch {
            throw WorkerClientError.launch(error.localizedDescription)
        }

        let cancelAfterLaunch = stateLock.withLock {
            activeProcess === process && cancellationRequested
        }
        if cancelAfterLaunch {
            cancel()
        }

        async let diagnosticsData = Self.readAll(from: standardError.fileHandleForReading)
        async let processExitStatus = exitWaiter.wait()

        do {
            var requestData = try JSONEncoder().encode(job)
            requestData.append(0x0A)
            try standardInput.fileHandleForWriting.write(contentsOf: requestData)
            try standardInput.fileHandleForWriting.close()
        } catch {
            cancel()
            _ = await processExitStatus
            _ = await diagnosticsData
            throw WorkerClientError.requestEncoding(error.localizedDescription)
        }

        let terminalEvent: WorkerEvent
        do {
            guard let event = try await readEvents(
                from: standardOutput.fileHandleForReading,
                expectedJobID: job.jobID,
                process: process,
                onEvent: onEvent
            ) else {
                let exitStatus = await processExitStatus
                let diagnostics = Self.decodeDiagnostics(await diagnosticsData)
                throw WorkerClientError.missingTerminalEvent(exitStatus: exitStatus, diagnostics: diagnostics)
            }
            terminalEvent = event
        } catch let error as WorkerClientError {
            throw error
        } catch {
            cancel()
            let exitStatus = await processExitStatus
            let diagnostics = Self.decodeDiagnostics(await diagnosticsData)
            let detail = diagnostics.isEmpty ? "worker exit status \(exitStatus)" : diagnostics
            throw WorkerClientError.protocolFailure(message: error.localizedDescription, diagnostics: detail)
        }

        let exitStatus = await processExitStatus
        let diagnostics = Self.decodeDiagnostics(await diagnosticsData)
        let wasCancelled = stateLock.withLock {
            activeProcess === process && cancellationRequested
        }
        if terminalEvent.type == .jobCompleted, exitStatus != 0, !wasCancelled {
            throw WorkerClientError.unexpectedExit(exitStatus: exitStatus, diagnostics: diagnostics)
        }
        return WorkerRunResult(terminalEvent: terminalEvent, exitStatus: exitStatus, diagnostics: diagnostics)
    }

    func cancel() {
        let target = stateLock.withLock {
            cancellationRequested = true
            return (activeProcess, activeProcessGroupID)
        }
        guard let process = target.0, process.isRunning else {
            return
        }

        if let processGroupID = verifiedProcessGroup(target.1, for: process) {
            if kill(-processGroupID, SIGTERM) != 0 {
                process.terminate()
            }
        } else {
            process.terminate()
        }

        DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + 2) { [weak self, weak process] in
            guard let self, let process, process.isRunning else {
                return
            }
            let processGroupID = self.stateLock.withLock {
                self.activeProcess === process ? self.activeProcessGroupID : nil
            }
            if let processGroupID = self.verifiedProcessGroup(processGroupID, for: process) {
                if kill(-processGroupID, SIGKILL) != 0 {
                    kill(process.processIdentifier, SIGKILL)
                }
            } else {
                kill(process.processIdentifier, SIGKILL)
            }
        }
    }

    private func register(_ process: Process) -> Bool {
        stateLock.withLock {
            guard activeProcess == nil else {
                return false
            }
            activeProcess = process
            activeProcessGroupID = nil
            return true
        }
    }

    private func clear(_ process: Process) {
        stateLock.withLock {
            guard activeProcess === process else {
                return
            }
            activeProcess = nil
            activeProcessGroupID = nil
            cancellationRequested = false
        }
    }

    private func recordProcessGroup(_ processGroupID: pid_t, process: Process) {
        stateLock.withLock {
            guard activeProcess === process else {
                return
            }
            activeProcessGroupID = processGroupID
        }
    }

    private func verifiedProcessGroup(_ candidate: pid_t?, for process: Process) -> pid_t? {
        guard let candidate,
              candidate == process.processIdentifier,
              getpgid(process.processIdentifier) == candidate else {
            return nil
        }
        return candidate
    }

    private func readEvents(
        from fileHandle: FileHandle,
        expectedJobID: UUID,
        process: Process,
        onEvent: @escaping EventHandler
    ) async throws -> WorkerEvent? {
        var framer = JSONLFramer()
        var terminalEvent: WorkerEvent?
        var expectedSequence = 0
        let decoder = JSONDecoder()

        for try await chunk in Self.chunks(from: fileHandle) {
            for line in try framer.append(chunk) {
                guard terminalEvent == nil else {
                    throw WorkerLifecycleError.eventAfterTerminal
                }
                let event = try decoder.decode(WorkerEvent.self, from: line)
                guard event.protocolVersion == WorkerJobSpec.protocolVersion else {
                    throw WorkerLifecycleError.protocolMismatch(received: event.protocolVersion)
                }
                guard event.jobID == expectedJobID else {
                    throw WorkerLifecycleError.unexpectedJob(received: event.jobID)
                }
                guard event.sequence == expectedSequence else {
                    throw WorkerLifecycleError.unexpectedSequence(
                        expected: expectedSequence,
                        received: event.sequence
                    )
                }
                if expectedSequence == 0 {
                    guard event.type == .workerReady else {
                        throw WorkerStreamError.expectedWorkerReady(received: event.type)
                    }
                    guard let processGroupID = event.payload.processGroupID,
                          processGroupID == process.processIdentifier else {
                        throw WorkerStreamError.invalidProcessGroup(
                            received: event.payload.processGroupID
                        )
                    }
                    recordProcessGroup(processGroupID, process: process)
                } else if event.type == .workerReady {
                    throw WorkerStreamError.duplicateWorkerReady
                }
                expectedSequence += 1
                if event.type.isTerminal {
                    terminalEvent = event
                }
                try await onEvent(event)
            }
        }
        try framer.finish()
        return terminalEvent
    }

    private static func chunks(from fileHandle: FileHandle) -> AsyncThrowingStream<Data, Error> {
        AsyncThrowingStream { continuation in
            let fileDescriptor = dup(fileHandle.fileDescriptor)
            guard fileDescriptor >= 0 else {
                continuation.finish(
                    throwing: NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
                )
                return
            }
            let channel = DispatchIO(
                type: .stream,
                fileDescriptor: fileDescriptor,
                queue: ioQueue
            ) { _ in
                close(fileDescriptor)
            }
            channel.setLimit(lowWater: 1)
            channel.read(offset: 0, length: Int.max, queue: ioQueue) { done, dispatchData, errorCode in
                if let dispatchData, !dispatchData.isEmpty {
                    continuation.yield(Data(dispatchData))
                }
                if errorCode != 0 {
                    continuation.finish(
                        throwing: NSError(domain: NSPOSIXErrorDomain, code: Int(errorCode))
                    )
                    channel.close(flags: .stop)
                } else if done {
                    continuation.finish()
                    channel.close()
                }
            }
            continuation.onTermination = { _ in
                channel.close(flags: .stop)
            }
        }
    }

    private static func readAll(from fileHandle: FileHandle) async -> Data {
        var output = Data()
        do {
            for try await chunk in chunks(from: fileHandle) {
                output.append(chunk)
            }
        } catch {
            return output
        }
        return output
    }

    private static func decodeDiagnostics(_ data: Data) -> String {
        String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }
}

private final class ProcessExitWaiter: @unchecked Sendable {
    private let lock = NSLock()
    private var status: Int32?
    private var continuation: CheckedContinuation<Int32, Never>?

    func complete(with status: Int32) {
        let waitingContinuation = lock.withLock { () -> CheckedContinuation<Int32, Never>? in
            guard self.status == nil else {
                return nil
            }
            self.status = status
            defer { continuation = nil }
            return continuation
        }
        waitingContinuation?.resume(returning: status)
    }

    func wait() async -> Int32 {
        await withCheckedContinuation { waitingContinuation in
            let completedStatus = lock.withLock { () -> Int32? in
                if let status {
                    return status
                }
                continuation = waitingContinuation
                return nil
            }
            if let completedStatus {
                waitingContinuation.resume(returning: completedStatus)
            }
        }
    }
}

private extension NSLock {
    func withLock<Result>(_ operation: () throws -> Result) rethrows -> Result {
        lock()
        defer { unlock() }
        return try operation()
    }
}

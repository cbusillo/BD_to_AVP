import Darwin
import Foundation

enum WorkerClientError: Error, LocalizedError {
    case alreadyRunning
    case requestEncoding(message: String, exitStatus: Int32?)
    case launch(String)
    case protocolFailure(message: String, diagnostics: String, exitStatus: Int32)
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
        case let .requestEncoding(message, exitStatus):
            return [
                message,
                exitStatus.map { "Exit status: \($0)" },
            ]
            .compactMap { $0 }
            .filter { !$0.isEmpty }
            .joined(separator: "\n\n")
        case let .launch(message):
            return message
        case let .protocolFailure(message, diagnostics, exitStatus):
            return [message, "Exit status: \(exitStatus)", diagnostics]
                .filter { !$0.isEmpty }
                .joined(separator: "\n\n")
        case let .missingTerminalEvent(exitStatus, diagnostics),
             let .unexpectedExit(exitStatus, diagnostics):
            return ["Exit status: \(exitStatus)", diagnostics].filter { !$0.isEmpty }.joined(separator: "\n\n")
        case .alreadyRunning:
            return nil
        }
    }

    var diagnostics: String? { technicalDetails }

    var processExitStatus: Int32? {
        switch self {
        case let .requestEncoding(_, exitStatus):
            return exitStatus
        case let .protocolFailure(_, _, exitStatus),
             let .missingTerminalEvent(exitStatus, _),
             let .unexpectedExit(exitStatus, _):
            return exitStatus
        case .alreadyRunning, .launch:
            return nil
        }
    }
}

private enum WorkerStreamError: Error, LocalizedError {
    case expectedWorkerReady(received: WorkerEventType)
    case invalidProcessGroup(received: Int32?)
    case duplicateWorkerReady

    var errorDescription: String? {
        switch self {
        case let .expectedWorkerReady(received):
            return "The first worker event was \(received.rawValue), not worker.ready."
        case .invalidProcessGroup:
            return "The worker reported an invalid process group identifier."
        case .duplicateWorkerReady:
            return "The worker sent worker.ready more than once."
        }
    }
}

struct WorkerRunResult {
    let terminalEvent: WorkerEvent
    let exitStatus: Int32
    let diagnostics: String
    let diagnosticSnapshot: WorkerProcessDiagnosticSnapshot

    init(
        terminalEvent: WorkerEvent,
        exitStatus: Int32,
        diagnostics: String,
        diagnosticSnapshot: WorkerProcessDiagnosticSnapshot = .empty
    ) {
        self.terminalEvent = terminalEvent
        self.exitStatus = exitStatus
        self.diagnostics = diagnostics
        self.diagnosticSnapshot = diagnosticSnapshot
    }
}

protocol WorkerProcessRunning: AnyObject {
    func run(
        job: WorkerJobSpec,
        onEvent: @escaping (WorkerEvent) async throws -> Void
    ) async throws -> WorkerRunResult
    func cancel()
    func diagnosticSnapshot() -> WorkerProcessDiagnosticSnapshot
}

extension WorkerProcessRunning {
    func diagnosticSnapshot() -> WorkerProcessDiagnosticSnapshot { .empty }
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
    private var activeDiagnosticBuffer: BoundedDiagnosticTextBuffer?
    private var lastDiagnosticSnapshot = WorkerProcessDiagnosticSnapshot.empty
    private var cancellationRequested = false

    init(configuration: WorkerLaunchConfiguration) {
        self.configuration = configuration
    }

    func run(job: WorkerJobSpec, onEvent: @escaping EventHandler) async throws -> WorkerRunResult {
        do {
            let result = try await withTaskCancellationHandler(
                operation: {
                    try await runProcess(job: job, onEvent: onEvent)
                },
                onCancel: {
                    self.cancel()
                }
            )
            try Task.checkCancellation()
            return result
        } catch {
            if Task.isCancelled {
                throw CancellationError()
            }
            throw error
        }
    }

    private func runProcess(job: WorkerJobSpec, onEvent: @escaping EventHandler) async throws -> WorkerRunResult {
        let process = Process()
        let standardInput = Pipe()
        let standardOutput = Pipe()
        let standardError = Pipe()
        let diagnosticBuffer = BoundedDiagnosticTextBuffer(maximumBytes: 512 * 1_024)
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

        guard register(process, diagnosticBuffer: diagnosticBuffer) else {
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

        let diagnosticsDrainer = DiagnosticPipeDrainer(
            fileHandle: standardError.fileHandleForReading,
            buffer: diagnosticBuffer,
            queue: Self.ioQueue
        )
        async let processExitStatus = exitWaiter.wait()

        do {
            var requestData = try JSONEncoder().encode(job)
            requestData.append(0x0A)
            try standardInput.fileHandleForWriting.write(contentsOf: requestData)
            try standardInput.fileHandleForWriting.close()
        } catch {
            cancel()
            let exitStatus = await processExitStatus
            await diagnosticsDrainer.wait()
            throw WorkerClientError.requestEncoding(
                message: error.localizedDescription,
                exitStatus: exitStatus
            )
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
                await diagnosticsDrainer.wait()
                let diagnostics = diagnosticBuffer.snapshot().text
                throw WorkerClientError.missingTerminalEvent(exitStatus: exitStatus, diagnostics: diagnostics)
            }
            terminalEvent = event
        } catch let error as WorkerClientError {
            throw error
        } catch {
            cancel()
            let exitStatus = await processExitStatus
            await diagnosticsDrainer.wait()
            let diagnostics = diagnosticBuffer.snapshot().text
            throw WorkerClientError.protocolFailure(
                message: error.localizedDescription,
                diagnostics: diagnostics,
                exitStatus: exitStatus
            )
        }

        let exitStatus = await processExitStatus
        await diagnosticsDrainer.wait()
        let finalSnapshot = processDiagnosticSnapshot(process: process, buffer: diagnosticBuffer)
        let diagnostics = finalSnapshot.toolOutput.text
        let wasCancelled = stateLock.withLock {
            activeProcess === process && cancellationRequested
        }
        if terminalEvent.type == .jobCompleted, exitStatus != 0, !wasCancelled {
            throw WorkerClientError.unexpectedExit(exitStatus: exitStatus, diagnostics: diagnostics)
        }
        return WorkerRunResult(
            terminalEvent: terminalEvent,
            exitStatus: exitStatus,
            diagnostics: diagnostics,
            diagnosticSnapshot: finalSnapshot
        )
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

    func diagnosticSnapshot() -> WorkerProcessDiagnosticSnapshot {
        stateLock.withLock {
            guard let activeProcess, let activeDiagnosticBuffer else {
                return lastDiagnosticSnapshot
            }
            return WorkerProcessDiagnosticSnapshot(
                isRunning: activeProcess.isRunning,
                processIdentifier: activeProcess.processIdentifier > 0 ? activeProcess.processIdentifier : nil,
                processGroupIdentifier: activeProcessGroupID,
                cancellationRequested: cancellationRequested,
                toolOutput: activeDiagnosticBuffer.snapshot()
            )
        }
    }

    private func register(
        _ process: Process,
        diagnosticBuffer: BoundedDiagnosticTextBuffer
    ) -> Bool {
        stateLock.withLock {
            guard activeProcess == nil else {
                return false
            }
            activeProcess = process
            activeProcessGroupID = nil
            activeDiagnosticBuffer = diagnosticBuffer
            lastDiagnosticSnapshot = .empty
            return true
        }
    }

    private func clear(_ process: Process) {
        stateLock.withLock {
            guard activeProcess === process else {
                return
            }
            lastDiagnosticSnapshot = WorkerProcessDiagnosticSnapshot(
                isRunning: false,
                processIdentifier: process.processIdentifier > 0 ? process.processIdentifier : nil,
                processGroupIdentifier: activeProcessGroupID,
                cancellationRequested: cancellationRequested,
                toolOutput: activeDiagnosticBuffer?.snapshot() ?? .empty
            )
            activeProcess = nil
            activeProcessGroupID = nil
            activeDiagnosticBuffer = nil
            cancellationRequested = false
        }
    }

    private func processDiagnosticSnapshot(
        process: Process,
        buffer: BoundedDiagnosticTextBuffer
    ) -> WorkerProcessDiagnosticSnapshot {
        stateLock.withLock {
            WorkerProcessDiagnosticSnapshot(
                isRunning: process.isRunning,
                processIdentifier: process.processIdentifier > 0 ? process.processIdentifier : nil,
                processGroupIdentifier: activeProcess === process ? activeProcessGroupID : nil,
                cancellationRequested: activeProcess === process && cancellationRequested,
                toolOutput: buffer.snapshot()
            )
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
        let reader = try PullDrivenFileReader(
            fileHandle: fileHandle,
            queue: Self.ioQueue
        )
        defer {
            reader.close()
        }
        var framer = JSONLFramer()
        var terminalEvent: WorkerEvent?
        var expectedSequence = 0
        let decoder = JSONDecoder()

        while let chunk = try await reader.read(maximumBytes: Self.stdoutChunkBytes) {
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

    private static let stdoutChunkBytes = 64 * 1_024

}

private final class PullDrivenFileReader: @unchecked Sendable {
    private let fileDescriptor: Int32
    private let queue: DispatchQueue
    private let lock = NSLock()
    private var closed = false

    init(fileHandle: FileHandle, queue: DispatchQueue) throws {
        let fileDescriptor = dup(fileHandle.fileDescriptor)
        guard fileDescriptor >= 0 else {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
        self.fileDescriptor = fileDescriptor
        self.queue = queue
    }

    deinit {
        close()
    }

    func read(maximumBytes: Int) async throws -> Data? {
        try await withCheckedThrowingContinuation { continuation in
            queue.async { [self] in
                continuation.resume(
                    with: Result {
                        try readSynchronously(maximumBytes: maximumBytes)
                    }
                )
            }
        }
    }

    func close() {
        let shouldClose = lock.withLock {
            guard !closed else {
                return false
            }
            closed = true
            return true
        }
        if shouldClose {
            Darwin.close(fileDescriptor)
        }
    }

    private func readSynchronously(maximumBytes: Int) throws -> Data? {
        guard maximumBytes > 0 else {
            return nil
        }
        var buffer = [UInt8](repeating: 0, count: maximumBytes)
        while true {
            let byteCount = buffer.withUnsafeMutableBytes { bytes in
                Darwin.read(fileDescriptor, bytes.baseAddress, bytes.count)
            }
            if byteCount > 0 {
                return Data(buffer.prefix(Int(byteCount)))
            }
            if byteCount == 0 {
                return nil
            }
            let errorCode = errno
            if errorCode == EINTR {
                continue
            }
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errorCode))
        }
    }
}

private final class DiagnosticPipeDrainer: @unchecked Sendable {
    private static let maximumChunkBytes = 64 * 1_024

    private let lock = NSLock()
    private let completion = AsyncCompletion()
    private var channel: DispatchIO?
    private var finished = false

    init(
        fileHandle: FileHandle,
        buffer: BoundedDiagnosticTextBuffer,
        queue: DispatchQueue
    ) {
        let fileDescriptor = dup(fileHandle.fileDescriptor)
        guard fileDescriptor >= 0 else {
            completion.complete()
            return
        }
        let channel = DispatchIO(
            type: .stream,
            fileDescriptor: fileDescriptor,
            queue: queue
        ) { _ in
            close(fileDescriptor)
        }
        self.channel = channel
        channel.setLimit(lowWater: 1)
        channel.setLimit(highWater: Self.maximumChunkBytes)
        channel.read(offset: 0, length: Int.max, queue: queue) { [weak self] done, dispatchData, errorCode in
            guard let self else {
                return
            }
            if let dispatchData, !dispatchData.isEmpty {
                buffer.append(Data(dispatchData))
            }
            if errorCode != 0 {
                finish(stop: true)
            } else if done {
                finish(stop: false)
            }
        }
    }

    deinit {
        finish(stop: true)
    }

    func wait() async {
        await completion.wait()
    }

    private func finish(stop: Bool) {
        let channel = lock.withLock { () -> DispatchIO? in
            guard !finished else {
                return nil
            }
            finished = true
            defer { self.channel = nil }
            return self.channel
        }
        guard let channel else {
            return
        }
        channel.close(flags: stop ? .stop : [])
        completion.complete()
    }
}

private final class AsyncCompletion: @unchecked Sendable {
    private let lock = NSLock()
    private var completed = false
    private var continuation: CheckedContinuation<Void, Never>?

    func complete() {
        let waitingContinuation = lock.withLock { () -> CheckedContinuation<Void, Never>? in
            guard !completed else {
                return nil
            }
            completed = true
            defer { continuation = nil }
            return continuation
        }
        waitingContinuation?.resume()
    }

    func wait() async {
        await withCheckedContinuation { waitingContinuation in
            let isCompleted = lock.withLock { () -> Bool in
                if completed {
                    return true
                }
                continuation = waitingContinuation
                return false
            }
            if isCompleted {
                waitingContinuation.resume()
            }
        }
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

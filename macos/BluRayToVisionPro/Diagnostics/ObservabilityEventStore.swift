import Darwin
import Dispatch
import Foundation

struct ObservabilityEventPersistenceSnapshot: Equatable, Sendable {
    let enabled: Bool
    let maximumFileBytes: Int
    let maximumTotalBytes: Int
    let maximumPendingBytes: Int
    let pendingBytes: Int
    let writtenEvents: Int
    let writtenBytes: Int
    let droppedEvents: Int
    let droppedBytes: Int
    let failureCount: Int

    static let disabled = ObservabilityEventPersistenceSnapshot(
        enabled: false,
        maximumFileBytes: 0,
        maximumTotalBytes: 0,
        maximumPendingBytes: 0,
        pendingBytes: 0,
        writtenEvents: 0,
        writtenBytes: 0,
        droppedEvents: 0,
        droppedBytes: 0,
        failureCount: 0
    )
}

protocol ObservabilityEventPersisting: Sendable {
    func append(_ event: ObservabilityEvent)
    func snapshot() -> ObservabilityEventPersistenceSnapshot
    func flush() async
}

extension ObservabilityEventPersisting {
    func flush() async {}
}

final class NullObservabilityEventStore: ObservabilityEventPersisting, @unchecked Sendable {
    static let shared = NullObservabilityEventStore()

    private init() {}

    func append(_: ObservabilityEvent) {}

    func snapshot() -> ObservabilityEventPersistenceSnapshot {
        .disabled
    }
}

protocol ObservabilityEventWriting: AnyObject {
    func append(_ line: Data) throws
}

final class ObservabilityEventStore: ObservabilityEventPersisting, @unchecked Sendable {
    struct Configuration: Sendable {
        let directoryURL: URL
        let maximumFileBytes: Int
        let maximumTotalBytes: Int
        let maximumPendingBytes: Int

        init(
            directoryURL: URL,
            maximumFileBytes: Int = 4 * 1_024 * 1_024,
            maximumTotalBytes: Int = 12 * 1_024 * 1_024,
            maximumPendingBytes: Int = 4 * 1_024 * 1_024
        ) {
            precondition(maximumFileBytes > 0)
            precondition(maximumTotalBytes >= maximumFileBytes)
            precondition(maximumPendingBytes > 0)
            self.directoryURL = directoryURL
            self.maximumFileBytes = maximumFileBytes
            self.maximumTotalBytes = maximumTotalBytes
            self.maximumPendingBytes = maximumPendingBytes
        }
    }

    typealias WriterFactory = () throws -> any ObservabilityEventWriting

    private struct State {
        var pendingLines: [Data] = []
        var pendingIndex = 0
        var pendingBytes = 0
        var drainScheduled = false
        var writtenEvents = 0
        var writtenBytes = 0
        var droppedEvents = 0
        var droppedBytes = 0
        var failureCount = 0
        var lastFailureDescription: String?
    }

    private let configuration: Configuration
    private let writerFactory: WriterFactory
    private let lock = NSLock()
    private let queue = DispatchQueue(label: "com.shinycomputers.bd-to-avp.observability-store", qos: .utility)
    private var state = State()
    private var writer: (any ObservabilityEventWriting)?

    init(
        configuration: Configuration,
        writerFactory: WriterFactory? = nil
    ) {
        self.configuration = configuration
        self.writerFactory = writerFactory ?? {
            try SecureRotatingJSONLWriter(configuration: configuration)
        }
    }

    static func automatic(
        fileManager: FileManager = .default,
        bundle: Bundle = .main
    ) -> any ObservabilityEventPersisting {
        guard let applicationSupport = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first else {
            return NullObservabilityEventStore.shared
        }
        let identifier = bundle.bundleIdentifier ?? "com.shinycomputers.bd-to-avp"
        let directoryURL = applicationSupport
            .appendingPathComponent(identifier, isDirectory: true)
            .appendingPathComponent("Observability", isDirectory: true)
        return ObservabilityEventStore(configuration: Configuration(directoryURL: directoryURL))
    }

    func append(_ event: ObservabilityEvent) {
        let line: Data
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
            var encoded = try encoder.encode(event)
            encoded.append(0x0A)
            line = encoded
        } catch {
            lock.withObservabilityLock {
                state.droppedEvents += 1
                state.failureCount += 1
                state.lastFailureDescription = String(describing: error)
            }
            return
        }

        var shouldScheduleDrain = false
        lock.withObservabilityLock {
            guard line.count <= configuration.maximumFileBytes,
                  line.count <= configuration.maximumPendingBytes
            else {
                state.droppedEvents += 1
                state.droppedBytes += line.count
                return
            }
            while state.pendingBytes + line.count > configuration.maximumPendingBytes {
                guard let dropped = dequeuePendingLine() else {
                    break
                }
                state.droppedEvents += 1
                state.droppedBytes += dropped.count
            }
            state.pendingLines.append(line)
            state.pendingBytes += line.count
            if !state.drainScheduled {
                state.drainScheduled = true
                shouldScheduleDrain = true
            }
        }
        if shouldScheduleDrain {
            queue.async { [self] in
                drain()
            }
        }
    }

    func snapshot() -> ObservabilityEventPersistenceSnapshot {
        lock.withObservabilityLock {
            ObservabilityEventPersistenceSnapshot(
                enabled: true,
                maximumFileBytes: configuration.maximumFileBytes,
                maximumTotalBytes: configuration.maximumTotalBytes,
                maximumPendingBytes: configuration.maximumPendingBytes,
                pendingBytes: state.pendingBytes,
                writtenEvents: state.writtenEvents,
                writtenBytes: state.writtenBytes,
                droppedEvents: state.droppedEvents,
                droppedBytes: state.droppedBytes,
                failureCount: state.failureCount
            )
        }
    }

    func flushForTesting() {
        queue.sync {}
    }

    func failureDescriptionForTesting() -> String? {
        lock.withObservabilityLock { state.lastFailureDescription }
    }

    func flush() async {
        await withCheckedContinuation { continuation in
            queue.async { [self] in
                drain()
                continuation.resume()
            }
        }
    }

    private func drain() {
        while true {
            let line = lock.withObservabilityLock { () -> Data? in
                guard let line = dequeuePendingLine() else {
                    state.drainScheduled = false
                    return nil
                }
                return line
            }
            guard let line else {
                return
            }
            do {
                if writer == nil {
                    writer = try writerFactory()
                }
                try writer?.append(line)
                lock.withObservabilityLock {
                    state.writtenEvents += 1
                    state.writtenBytes += line.count
                }
            } catch {
                writer = nil
                lock.withObservabilityLock {
                    state.droppedEvents += 1
                    state.droppedBytes += line.count
                    state.failureCount += 1
                    state.lastFailureDescription = String(describing: error)
                }
            }
        }
    }

    private func dequeuePendingLine() -> Data? {
        guard state.pendingIndex < state.pendingLines.count else {
            state.pendingLines.removeAll(keepingCapacity: true)
            state.pendingIndex = 0
            state.pendingBytes = 0
            return nil
        }
        let line = state.pendingLines[state.pendingIndex]
        state.pendingLines[state.pendingIndex] = Data()
        state.pendingIndex += 1
        state.pendingBytes -= line.count
        if state.pendingIndex == state.pendingLines.count {
            state.pendingLines.removeAll(keepingCapacity: true)
            state.pendingIndex = 0
        } else if state.pendingIndex >= 128,
                  state.pendingIndex * 2 >= state.pendingLines.count
        {
            state.pendingLines.removeFirst(state.pendingIndex)
            state.pendingIndex = 0
        }
        return line
    }
}

private enum SecureJSONLWriterError: Error {
    case invalidDirectory
    case invalidEntry(String)
    case systemCall(String, Int32)
}

private final class SecureRotatingJSONLWriter: ObservabilityEventWriting {
    private static let currentFilename = "events.jsonl"
    private static let lockFilename = ".events.jsonl.lock"

    private let directoryFileDescriptor: Int32
    private let maximumFileBytes: Int
    private let segmentCount: Int

    init(configuration: ObservabilityEventStore.Configuration) throws {
        maximumFileBytes = configuration.maximumFileBytes
        segmentCount = max(1, configuration.maximumTotalBytes / configuration.maximumFileBytes)
        directoryFileDescriptor = try Self.openPrivateDirectory(at: configuration.directoryURL)
    }

    deinit {
        close(directoryFileDescriptor)
    }

    func append(_ line: Data) throws {
        guard !line.isEmpty, line.count <= maximumFileBytes else {
            throw SecureJSONLWriterError.invalidEntry(Self.currentFilename)
        }
        let lockFileDescriptor = try openValidatedFile(
            named: Self.lockFilename,
            flags: O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW
        )
        defer { close(lockFileDescriptor) }
        guard flock(lockFileDescriptor, LOCK_EX | LOCK_NB) == 0 else {
            throw SecureJSONLWriterError.systemCall("flock", errno)
        }
        defer { _ = flock(lockFileDescriptor, LOCK_UN) }

        try removeOutOfRangeSegments()
        try normalizeAndRemoveOversizedSegments()
        var eventFileDescriptor = try openValidatedFile(
            named: Self.currentFilename,
            flags: O_RDWR | O_APPEND | O_CREAT | O_CLOEXEC | O_NOFOLLOW
        )
        defer {
            if eventFileDescriptor >= 0 {
                close(eventFileDescriptor)
            }
        }
        var metadata = try repairPartialTail(
            fileDescriptor: eventFileDescriptor,
            metadata: fileMetadata(eventFileDescriptor, named: Self.currentFilename)
        )
        if metadata.st_size + off_t(line.count) > off_t(maximumFileBytes) {
            close(eventFileDescriptor)
            eventFileDescriptor = -1
            try rotate()
            eventFileDescriptor = try openValidatedFile(
                named: Self.currentFilename,
                flags: O_RDWR | O_APPEND | O_CREAT | O_CLOEXEC | O_NOFOLLOW
            )
            metadata = try fileMetadata(eventFileDescriptor, named: Self.currentFilename)
        }
        let originalSize = metadata.st_size
        do {
            try writeAll(line, to: eventFileDescriptor)
        } catch {
            guard ftruncate(eventFileDescriptor, originalSize) == 0 else {
                throw SecureJSONLWriterError.systemCall("ftruncate(rollback)", errno)
            }
            throw error
        }
    }

    private func rotate() throws {
        try compactRotatedSegments()
        for index in stride(from: segmentCount - 1, through: 1, by: -1) {
            let destination = filename(for: index)
            let source = filename(for: index - 1)
            if try validatedEntryExists(named: source) {
                _ = try validatedEntryMetadata(named: destination)
                try rename(source, to: destination)
            }
        }
        if segmentCount == 1,
           try validatedEntryExists(named: Self.currentFilename)
        {
            try unlink(named: Self.currentFilename)
        }
    }

    private func compactRotatedSegments() throws {
        var destinationIndex = 1
        for sourceIndex in 1 ..< segmentCount {
            let source = filename(for: sourceIndex)
            guard try recoverableRotatedEntryExists(named: source) else {
                continue
            }
            if sourceIndex != destinationIndex {
                let destination = filename(for: destinationIndex)
                guard try validatedEntryMetadata(named: destination) == nil else {
                    throw SecureJSONLWriterError.invalidEntry(destination)
                }
                try rename(source, to: destination)
            }
            destinationIndex += 1
        }
    }

    private func normalizeAndRemoveOversizedSegments() throws {
        for index in 0 ..< segmentCount {
            let name = filename(for: index)
            let exists = if index == 0 {
                try validatedEntryExists(named: name)
            } else {
                try recoverableRotatedEntryExists(named: name)
            }
            guard exists else {
                continue
            }
            let descriptor = try openValidatedFile(
                named: name,
                flags: O_RDWR | O_CLOEXEC | O_NOFOLLOW
            )
            defer { close(descriptor) }
            let metadata = try fileMetadata(descriptor, named: name)
            if metadata.st_size > off_t(maximumFileBytes) {
                try unlink(named: name)
            }
        }
    }

    private func removeOutOfRangeSegments() throws {
        let scanDescriptor = ".".withCString {
            openat(
                directoryFileDescriptor,
                $0,
                O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW
            )
        }
        guard scanDescriptor >= 0 else {
            throw SecureJSONLWriterError.systemCall("openat(directory)", errno)
        }
        guard let directory = fdopendir(scanDescriptor) else {
            close(scanDescriptor)
            throw SecureJSONLWriterError.systemCall("fdopendir", errno)
        }
        defer { closedir(directory) }
        while let entry = readdir(directory) {
            let name = withUnsafePointer(to: &entry.pointee.d_name) { pointer in
                pointer.withMemoryRebound(
                    to: CChar.self,
                    capacity: Int(MAXNAMLEN) + 1
                ) {
                    String(cString: $0)
                }
            }
            guard let index = segmentIndex(from: name), index >= segmentCount else {
                continue
            }
            if try recoverableRotatedEntryExists(named: name) {
                try unlink(named: name)
            }
        }
    }

    private func segmentIndex(from name: String) -> Int? {
        guard name.hasPrefix("events."), name.hasSuffix(".jsonl") else {
            return nil
        }
        let start = name.index(name.startIndex, offsetBy: "events.".count)
        let end = name.index(name.endIndex, offsetBy: -".jsonl".count)
        guard start < end,
              let index = Int(name[start ..< end]),
              index > 0
        else {
            return nil
        }
        return index
    }

    private func filename(for index: Int) -> String {
        index == 0 ? Self.currentFilename : "events.\(index).jsonl"
    }

    private func openValidatedFile(named name: String, flags: Int32) throws -> Int32 {
        let descriptor = name.withCString {
            openat(directoryFileDescriptor, $0, flags | O_NONBLOCK, mode_t(0o600))
        }
        guard descriptor >= 0 else {
            throw SecureJSONLWriterError.systemCall("openat(\(name))", errno)
        }
        do {
            _ = try fileMetadata(descriptor, named: name)
            guard fchmod(descriptor, mode_t(0o600)) == 0 else {
                throw SecureJSONLWriterError.systemCall("fchmod(\(name))", errno)
            }
            try Self.removeExtendedACL(from: descriptor, name: name)
            return descriptor
        } catch {
            close(descriptor)
            throw error
        }
    }

    private func fileMetadata(_ descriptor: Int32, named name: String) throws -> stat {
        var metadata = stat()
        guard fstat(descriptor, &metadata) == 0 else {
            throw SecureJSONLWriterError.systemCall("fstat(\(name))", errno)
        }
        guard metadata.st_mode & S_IFMT == S_IFREG,
              metadata.st_uid == geteuid(),
              metadata.st_nlink == 1
        else {
            throw SecureJSONLWriterError.invalidEntry(name)
        }
        return metadata
    }

    private func validatedEntryExists(named name: String) throws -> Bool {
        try validatedEntryMetadata(named: name) != nil
    }

    private func recoverableRotatedEntryExists(named name: String) throws -> Bool {
        do {
            return try validatedEntryExists(named: name)
        } catch SecureJSONLWriterError.invalidEntry {
            try unlink(named: name)
            return false
        }
    }

    private func validatedEntryMetadata(named name: String) throws -> stat? {
        var metadata = stat()
        let result = name.withCString {
            fstatat(directoryFileDescriptor, $0, &metadata, AT_SYMLINK_NOFOLLOW)
        }
        if result != 0 {
            if errno == ENOENT {
                return nil
            }
            throw SecureJSONLWriterError.systemCall("fstatat(\(name))", errno)
        }
        guard metadata.st_mode & S_IFMT == S_IFREG,
              metadata.st_uid == geteuid(),
              metadata.st_nlink == 1
        else {
            throw SecureJSONLWriterError.invalidEntry(name)
        }
        return metadata
    }

    private func unlink(named name: String) throws {
        let result = name.withCString {
            unlinkat(directoryFileDescriptor, $0, 0)
        }
        guard result == 0 else {
            throw SecureJSONLWriterError.systemCall("unlinkat(\(name))", errno)
        }
    }

    private func rename(_ source: String, to destination: String) throws {
        let result = source.withCString { sourcePointer in
            destination.withCString { destinationPointer in
                renameat(
                    directoryFileDescriptor,
                    sourcePointer,
                    directoryFileDescriptor,
                    destinationPointer
                )
            }
        }
        guard result == 0 else {
            throw SecureJSONLWriterError.systemCall("renameat(\(source))", errno)
        }
    }

    private func writeAll(_ data: Data, to descriptor: Int32) throws {
        try data.withUnsafeBytes { bytes in
            guard let baseAddress = bytes.baseAddress else {
                return
            }
            var offset = 0
            while offset < bytes.count {
                let written = Darwin.write(
                    descriptor,
                    baseAddress.advanced(by: offset),
                    bytes.count - offset
                )
                if written < 0 {
                    if errno == EINTR {
                        continue
                    }
                    throw SecureJSONLWriterError.systemCall("write", errno)
                }
                guard written > 0 else {
                    throw SecureJSONLWriterError.systemCall("write", EIO)
                }
                offset += written
            }
        }
    }

    private func repairPartialTail(fileDescriptor: Int32, metadata: stat) throws -> stat {
        guard metadata.st_size > 0 else {
            return metadata
        }
        var lastByte: UInt8 = 0
        try readExactly(
            fileDescriptor: fileDescriptor,
            into: &lastByte,
            count: 1,
            offset: metadata.st_size - 1
        )
        guard lastByte != 0x0A else {
            return metadata
        }

        let chunkSize = 64 * 1_024
        var searchEnd = metadata.st_size
        var repairedSize: off_t = 0
        while searchEnd > 0 {
            let readSize = min(off_t(chunkSize), searchEnd)
            let readStart = searchEnd - readSize
            var bytes = [UInt8](repeating: 0, count: Int(readSize))
            try bytes.withUnsafeMutableBytes { buffer in
                guard let baseAddress = buffer.baseAddress else {
                    return
                }
                try readExactly(
                    fileDescriptor: fileDescriptor,
                    into: baseAddress,
                    count: buffer.count,
                    offset: readStart
                )
            }
            if let newlineIndex = bytes.lastIndex(of: 0x0A) {
                repairedSize = readStart + off_t(newlineIndex + 1)
                break
            }
            searchEnd = readStart
        }
        guard ftruncate(fileDescriptor, repairedSize) == 0 else {
            throw SecureJSONLWriterError.systemCall("ftruncate(repair)", errno)
        }
        return try fileMetadata(fileDescriptor, named: Self.currentFilename)
    }

    private func readExactly(
        fileDescriptor: Int32,
        into destination: UnsafeMutableRawPointer,
        count: Int,
        offset: off_t
    ) throws {
        var totalRead = 0
        while totalRead < count {
            let result = pread(
                fileDescriptor,
                destination.advanced(by: totalRead),
                count - totalRead,
                offset + off_t(totalRead)
            )
            if result < 0 {
                if errno == EINTR {
                    continue
                }
                throw SecureJSONLWriterError.systemCall("pread", errno)
            }
            guard result > 0 else {
                throw SecureJSONLWriterError.systemCall("pread", EIO)
            }
            totalRead += result
        }
    }

    private func readExactly(
        fileDescriptor: Int32,
        into byte: inout UInt8,
        count: Int,
        offset: off_t
    ) throws {
        try withUnsafeMutablePointer(to: &byte) {
            try readExactly(
                fileDescriptor: fileDescriptor,
                into: UnsafeMutableRawPointer($0),
                count: count,
                offset: offset
            )
        }
    }

    private static func openPrivateDirectory(at url: URL) throws -> Int32 {
        guard url.isFileURL, url.standardizedFileURL.path.hasPrefix("/") else {
            throw SecureJSONLWriterError.invalidDirectory
        }
        let standardizedPath = canonicalSystemPath(url.standardizedFileURL.path)
        var currentDescriptor = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC)
        guard currentDescriptor >= 0 else {
            throw SecureJSONLWriterError.systemCall("open(/)", errno)
        }
        let components = URL(fileURLWithPath: standardizedPath, isDirectory: true)
            .pathComponents
            .dropFirst()
        guard !components.isEmpty else {
            close(currentDescriptor)
            throw SecureJSONLWriterError.invalidDirectory
        }
        do {
            for (offset, component) in components.enumerated() {
                let isFinal = offset == components.count - 1
                var nextDescriptor = component.withCString {
                    openat(
                        currentDescriptor,
                        $0,
                        O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW
                    )
                }
                if nextDescriptor < 0, errno == ENOENT {
                    let createResult = component.withCString {
                        mkdirat(currentDescriptor, $0, mode_t(0o700))
                    }
                    if createResult != 0, errno != EEXIST {
                        throw SecureJSONLWriterError.systemCall("mkdirat(\(component))", errno)
                    }
                    nextDescriptor = component.withCString {
                        openat(
                            currentDescriptor,
                            $0,
                            O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW
                        )
                    }
                }
                guard nextDescriptor >= 0 else {
                    throw SecureJSONLWriterError.systemCall("openat(\(component))", errno)
                }
                var metadata = stat()
                guard fstat(nextDescriptor, &metadata) == 0,
                      metadata.st_mode & S_IFMT == S_IFDIR
                else {
                    close(nextDescriptor)
                    throw SecureJSONLWriterError.invalidEntry(component)
                }
                if isFinal {
                    guard metadata.st_uid == geteuid() else {
                        close(nextDescriptor)
                        throw SecureJSONLWriterError.invalidEntry(component)
                    }
                    guard fchmod(nextDescriptor, mode_t(0o700)) == 0 else {
                        close(nextDescriptor)
                        throw SecureJSONLWriterError.systemCall("fchmod(directory)", errno)
                    }
                    do {
                        try removeExtendedACL(from: nextDescriptor, name: component)
                    } catch {
                        close(nextDescriptor)
                        throw error
                    }
                }
                close(currentDescriptor)
                currentDescriptor = nextDescriptor
            }
            return currentDescriptor
        } catch {
            close(currentDescriptor)
            throw error
        }
    }

    private static func canonicalSystemPath(_ path: String) -> String {
        if path == "/var" || path.hasPrefix("/var/") || path == "/tmp" || path.hasPrefix("/tmp/") {
            return "/private\(path)"
        }
        return path
    }

    private static func removeExtendedACL(from descriptor: Int32, name: String) throws {
        guard let accessControlList = acl_get_fd_np(descriptor, ACL_TYPE_EXTENDED) else {
            if errno == ENOENT {
                return
            }
            throw SecureJSONLWriterError.systemCall("acl_get_fd_np(\(name))", errno)
        }
        defer { acl_free(UnsafeMutableRawPointer(accessControlList)) }
        var entry: acl_entry_t?
        while acl_get_entry(
            accessControlList,
            ACL_FIRST_ENTRY.rawValue,
            &entry
        ) == 0 {
            guard let entry,
                  acl_delete_entry(accessControlList, entry) == 0
            else {
                throw SecureJSONLWriterError.systemCall("acl_delete_entry(\(name))", errno)
            }
        }
        guard acl_set_fd_np(descriptor, accessControlList, ACL_TYPE_EXTENDED) == 0 else {
            throw SecureJSONLWriterError.systemCall("acl_set_fd_np(\(name))", errno)
        }
    }
}

private extension NSLock {
    func withObservabilityLock<Result>(_ operation: () throws -> Result) rethrows -> Result {
        lock()
        defer { unlock() }
        return try operation()
    }
}

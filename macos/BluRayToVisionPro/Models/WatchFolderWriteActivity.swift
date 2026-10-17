import Darwin
import Foundation

/// A file that a process belonging to this user has open for writing.
struct OpenWritableFile: Equatable, Sendable {
    /// The path the kernel reports, with symbolic links such as `/var` already resolved.
    let path: String
    let device: UInt32
    let inode: UInt64
    let processID: pid_t
    let processName: String
}

/// Every file this user's processes have open for writing, or why that could not be read.
enum OpenWritableFilesSnapshot: Equatable, Sendable {
    case available([OpenWritableFile])
    case unavailable(reason: String)
}

/// Lists open-for-writing files; tests substitute a fixed list.
protocol OpenWritableFilesProviding: Sendable {
    func openWritableFiles() -> OpenWritableFilesSnapshot
}

/// Reads open files from the kernel with libproc, for every process running as `userID`.
///
/// A process whose open files cannot be read is skipped. The snapshot is unavailable only when the
/// process list itself cannot be read or no process at all could be inspected.
struct LibprocOpenWritableFilesProvider: OpenWritableFilesProviding {
    /// The kernel's open-for-writing file flag (`FWRITE` in `sys/fcntl.h`).
    private static let writeFlag = UInt32(FWRITE)

    let userID: uid_t

    init(userID: uid_t = getuid()) {
        self.userID = userID
    }

    func openWritableFiles() -> OpenWritableFilesSnapshot {
        guard let processIDs = Self.processIDs(userID: userID) else {
            return .unavailable(reason: "The list of running apps could not be read (error \(errno)).")
        }
        var inspectedCount = 0
        var files: [OpenWritableFile] = []
        for processID in processIDs {
            guard let descriptors = Self.fileDescriptors(of: processID) else {
                continue
            }
            inspectedCount += 1
            var processName: String?
            for descriptor in descriptors where descriptor.proc_fdtype == UInt32(PROX_FDTYPE_VNODE) {
                var info = vnode_fdinfowithpath()
                let size = Int32(MemoryLayout<vnode_fdinfowithpath>.size)
                guard proc_pidfdinfo(processID, descriptor.proc_fd, PROC_PIDFDVNODEPATHINFO, &info, size) == size,
                      info.pfi.fi_openflags & Self.writeFlag != 0
                else {
                    continue
                }
                let name = processName ?? Self.name(of: processID)
                processName = name
                files.append(OpenWritableFile(
                    path: Self.string(fromPath: info.pvip.vip_path),
                    device: info.pvip.vip_vi.vi_stat.vst_dev,
                    inode: info.pvip.vip_vi.vi_stat.vst_ino,
                    processID: processID,
                    processName: name
                ))
            }
        }
        guard inspectedCount > 0 else {
            return .unavailable(reason: "No running app's open files could be read.")
        }
        return .available(files)
    }

    private static func processIDs(userID: uid_t) -> [pid_t]? {
        let byteCount = proc_listpids(UInt32(PROC_UID_ONLY), userID, nil, 0)
        guard byteCount > 0 else {
            return nil
        }
        // Leave room for processes that start between the two calls.
        var processIDs = [pid_t](repeating: 0, count: Int(byteCount) / MemoryLayout<pid_t>.stride + 64)
        let filled = processIDs.withUnsafeMutableBytes { buffer in
            proc_listpids(UInt32(PROC_UID_ONLY), userID, buffer.baseAddress, Int32(buffer.count))
        }
        guard filled > 0 else {
            return nil
        }
        return processIDs.prefix(Int(filled) / MemoryLayout<pid_t>.stride).filter { $0 > 0 }
    }

    private static func fileDescriptors(of processID: pid_t) -> [proc_fdinfo]? {
        let byteCount = proc_pidinfo(processID, PROC_PIDLISTFDS, 0, nil, 0)
        guard byteCount > 0 else {
            return nil
        }
        var descriptors = [proc_fdinfo](
            repeating: proc_fdinfo(),
            count: Int(byteCount) / MemoryLayout<proc_fdinfo>.stride + 16
        )
        let filled = descriptors.withUnsafeMutableBytes { buffer in
            proc_pidinfo(processID, PROC_PIDLISTFDS, 0, buffer.baseAddress, Int32(buffer.count))
        }
        guard filled > 0 else {
            return nil
        }
        return Array(descriptors.prefix(Int(filled) / MemoryLayout<proc_fdinfo>.stride))
    }

    private static func name(of processID: pid_t) -> String {
        var buffer = [CChar](repeating: 0, count: Int(MAXCOMLEN) * 2 + 1)
        guard proc_name(processID, &buffer, UInt32(buffer.count)) > 0 else {
            return "process \(processID)"
        }
        return String(decoding: buffer.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
    }

    private static func string<Path>(fromPath path: Path) -> String {
        withUnsafeBytes(of: path) { bytes in
            String(decoding: bytes.prefix { $0 != 0 }, as: UTF8.self)
        }
    }
}

/// Why a candidate that stopped changing is still not admitted.
enum WatchFolderWriteHold: Equatable, Sendable {
    /// A process still has the file, or a file inside the Blu-ray folder, open for writing.
    case openForWriting(processName: String)
    /// The file still starts with the placeholder MakeMKV writes until it finishes a title.
    case unfinishedMakeMKVRip

    func waitingMessage(fileName: String) -> String {
        switch self {
        case let .openForWriting(processName):
            "Still being written: “\(fileName)” is open in \(processName)."
        case .unfinishedMakeMKVRip:
            "Still being written: MakeMKV has not finished “\(fileName)”."
        }
    }
}

struct WatchFolderWriteCheckResult: Equatable, Sendable {
    /// Holds keyed by candidate fingerprint path; candidates without an entry may be admitted.
    var holds: [String: WatchFolderWriteHold] = [:]
    /// Set when open files could not be read, so only the settle time and MakeMKV check applied.
    var fallbackReason: String?
}

/// Confirms a settled candidate is not still being written before it is admitted.
///
/// A MakeMKV title that has not been finalized is held by its placeholder header. Anything else is
/// held while a process of this user has it (or, for a Blu-ray folder, anything inside it) open for
/// writing. When open files cannot be read, candidates fall back to the settle rule alone.
struct WatchFolderWriteCompletionCheck: Sendable {
    /// MakeMKV starts each title with this text and replaces it with the real header when the title
    /// finishes, so a stopped or failed rip still begins with it.
    static let makeMKVUnfinalizedMarker = Data("This file was not properly finalized".utf8)

    static let system = WatchFolderWriteCompletionCheck(openFiles: LibprocOpenWritableFilesProvider())

    let openFiles: any OpenWritableFilesProviding

    func check(_ candidates: [WatchFolderCandidate]) -> WatchFolderWriteCheckResult {
        var result = WatchFolderWriteCheckResult()
        var remaining: [WatchFolderCandidate] = []
        for candidate in candidates {
            if candidate.kind == .matroska, Self.hasUnfinalizedMakeMKVHeader(candidate.url) {
                result.holds[candidate.fingerprint.path] = .unfinishedMakeMKVRip
            } else {
                remaining.append(candidate)
            }
        }
        guard !remaining.isEmpty else {
            return result
        }
        let openFiles: [OpenWritableFile]
        switch self.openFiles.openWritableFiles() {
        case let .available(files):
            openFiles = files
        case let .unavailable(reason):
            result.fallbackReason = reason
            return result
        }
        for candidate in remaining {
            if let writer = Self.writer(of: candidate, among: openFiles) {
                result.holds[candidate.fingerprint.path] = .openForWriting(processName: writer.processName)
            }
        }
        return result
    }

    static func hasUnfinalizedMakeMKVHeader(_ url: URL) -> Bool {
        guard let handle = try? FileHandle(forReadingFrom: url) else {
            return false
        }
        defer { try? handle.close() }
        let header = (try? handle.read(upToCount: makeMKVUnfinalizedMarker.count)) ?? Data()
        return header == makeMKVUnfinalizedMarker
    }

    /// The first open-for-writing file that is this candidate, or inside it for a Blu-ray folder.
    static func writer(of candidate: WatchFolderCandidate, among openFiles: [OpenWritableFile]) -> OpenWritableFile? {
        guard !openFiles.isEmpty else {
            return nil
        }
        if candidate.kind == .bluRayFolder {
            // The kernel reports resolved paths; compare without case, since a false match only delays.
            let folderPath = resolvedPath(candidate.url.path).lowercased()
            let prefix = folderPath.hasSuffix("/") ? folderPath : folderPath + "/"
            return openFiles.first { $0.path.lowercased().hasPrefix(prefix) }
        }
        // Device and inode identify the file whatever path or link it was opened through.
        var status = stat()
        guard stat(candidate.url.path, &status) == 0 else {
            return nil
        }
        let device = UInt32(bitPattern: status.st_dev)
        let inode = UInt64(status.st_ino)
        return openFiles.first { $0.device == device && $0.inode == inode }
    }

    private static func resolvedPath(_ path: String) -> String {
        guard let resolved = realpath(path, nil) else {
            return path
        }
        defer { free(resolved) }
        return String(cString: resolved)
    }
}

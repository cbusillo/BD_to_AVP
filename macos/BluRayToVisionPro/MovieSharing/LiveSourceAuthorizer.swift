import Darwin
import Foundation

/// Why a live source could not be authorized. `message` is shown on the Vision Pro.
enum LiveSourceAuthorizationError: Error, Equatable {
    /// The Mac app's own open was refused (`EPERM` or `EACCES`).
    case permissionDenied
    /// The open did not finish within the deadline, or failed for another reason.
    case unavailable
    /// The file, or the approved folder, is no longer what the listing saw.
    case changed

    static let permissionMessage =
        "The Mac app needs permission to read this folder. Open the folder once in the Mac app, or allow access in System Settings, then refresh."

    var code: String {
        switch self {
        case .permissionDenied: "source_permission_required"
        case .unavailable: "source_unavailable"
        case .changed: "live_source_changed"
        }
    }

    var message: String {
        switch self {
        case .permissionDenied:
            Self.permissionMessage
        case .unavailable:
            "The Mac could not open this disc in time. Check that its drive or network share is connected, then refresh."
        case .changed:
            "The disc changed or is no longer available on the Mac. Refresh the library and choose it again."
        }
    }
}

/// Keeps a live job's source open: the descriptor the Mac authorized and the approved root with
/// its security-scoped access, for as long as the job runs.
final class LiveSourceRetainer: LiveSourceDescriptorHolding, @unchecked Sendable {
    let descriptor: Int32
    private let root: MovieRootDirectory

    init(descriptor: Int32, root: MovieRootDirectory) {
        self.descriptor = descriptor
        self.root = root
    }

    deinit {
        Darwin.close(descriptor)
    }
}

/// Turns a listed ISO into a source lease. A cached listing or index entry never authorizes
/// anything: every inspection and every start reopens the file here.
///
/// 1. Reopen the file under the approved root with the `O_NOFOLLOW` walk.
/// 2. Confirm the root is still the approved one.
/// 3. Compare the descriptor's revision with the requested one.
/// 4. Take the canonical path with `F_GETPATH`.
/// 5. Keep the descriptor and the root in the lease's retainer for the whole job.
///
/// The open runs off the caller with a deadline, because a network share can block it.
struct LiveSourceAuthorizer: Sendable {
    typealias Opener = @Sendable (_ root: MovieRootDirectory, _ components: [String]) -> Result<Int32, MovieFileAccessError>

    var deadline: TimeInterval = 10
    var open: Opener = { root, components in root.openFileReportingErrno(components) }

    func authorize(_ candidate: LiveCatalogCandidate, revision: String) async throws -> LiveSourceLease {
        let open = open
        let result = await BoundedFileAccess.run(deadline: deadline) {
            Self.lease(candidate, revision: revision, open: open)
        }
        return try (result ?? .failure(.unavailable)).get()
    }

    private static func lease(
        _ candidate: LiveCatalogCandidate,
        revision: String,
        open: Opener
    ) -> Result<LiveSourceLease, LiveSourceAuthorizationError> {
        let descriptor: Int32
        switch open(candidate.root, candidate.components) {
        case let .success(value):
            descriptor = value
        case let .failure(error):
            switch error.code {
            case EPERM, EACCES: return .failure(.permissionDenied)
            case ENOENT, ELOOP, ENOTDIR: return .failure(.changed)
            default: return .failure(.unavailable)
            }
        }
        let retainer = LiveSourceRetainer(descriptor: descriptor, root: candidate.root)
        let flags = fcntl(descriptor, F_GETFL)
        if flags >= 0 { _ = fcntl(descriptor, F_SETFL, flags & ~O_NONBLOCK) }
        var info = stat()
        guard candidate.root.isAtApprovedPath,
              Darwin.fstat(descriptor, &info) == 0,
              (info.st_mode & S_IFMT) == S_IFREG,
              MovieCatalog.revision(info) == revision
        else { return .failure(.changed) }
        var path = [CChar](repeating: 0, count: Int(MAXPATHLEN))
        guard fcntl(descriptor, F_GETPATH, &path) == 0 else { return .failure(.changed) }
        let canonicalPath = String(decoding: path.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
        return .success(LiveSourceLease(canonicalPath: canonicalPath, identity: LiveSourceIdentity(info), retainer: retainer))
    }
}

import AVFoundation
import Foundation
import UniformTypeIdentifiers

/// Feeds a movie file outside the app to `AVPlayer` through its own reads, so a dropped or slow
/// share behaves like a slow stream: the player buffers ahead, waits with its clock stopped when
/// the buffer runs out, and continues from the same frame when reads come back. Opened as a plain
/// file URL, `AVPlayer` keeps its clock running through a stalled read and then jumps forward to
/// catch up (#1131).
///
/// Reads go through `ShareConnectionMonitor`, which retries curable failures until they work,
/// the player cancels their request, or the monitor stops. Each loading request reads on its own work item, since a long sequential request
/// and a short one elsewhere in the file can be open at once.
final class ShareFileResourceLoader: NSObject, AVAssetResourceLoaderDelegate, @unchecked Sendable {
    typealias Read = @Sendable (_ offset: Int64, _ count: Int) throws -> Data

    static let chunkBytes = 1 << 20
    private static let scheme = "bdavpshare"

    let connection: ShareConnectionMonitor
    private let fileName: String
    private let byteCount: Int64
    private let read: Read
    private let delegateQueue = DispatchQueue(label: "com.shinycomputers.bd-to-avp.share-loader")
    private let readQueue = DispatchQueue(label: "com.shinycomputers.bd-to-avp.share-reads", attributes: .concurrent)
    // Guarded by `delegateQueue`.
    private var openRequests: Set<ObjectIdentifier> = []
    private var cancelled = false

    /// Reads the file at `url` with `pread`, so concurrent requests never share a file position.
    convenience init(url: URL, connection: ShareConnectionMonitor) throws {
        let file = try ShareFile(url: url)
        self.init(fileName: url.lastPathComponent, byteCount: file.byteCount, connection: connection) { offset, count in
            try file.read(at: offset, count: count)
        }
    }

    init(fileName: String, byteCount: Int64, connection: ShareConnectionMonitor, read: @escaping Read) {
        self.fileName = fileName
        self.byteCount = byteCount
        self.connection = connection
        self.read = read
    }

    func makeAsset() -> AVURLAsset {
        var components = URLComponents()
        components.scheme = Self.scheme
        components.host = "movie"
        components.path = "/" + fileName
        let asset = AVURLAsset(url: components.url ?? URL(string: "\(Self.scheme)://movie/movie.mov")!)
        asset.resourceLoader.setDelegate(self, queue: delegateQueue)
        return asset
    }

    /// Ends every open request and the monitor's retries.
    func cancel() {
        connection.cancel()
        delegateQueue.async {
            self.cancelled = true
            self.openRequests.removeAll()
        }
    }

    func resourceLoader(
        _ resourceLoader: AVAssetResourceLoader,
        shouldWaitForLoadingOfRequestedResource request: AVAssetResourceLoadingRequest
    ) -> Bool {
        guard !cancelled else {
            request.finishLoading(with: CancellationError())
            return true
        }
        if let information = request.contentInformationRequest {
            let fileExtension = (fileName as NSString).pathExtension
            information.contentType = UTType(filenameExtension: fileExtension)?.identifier ?? UTType.quickTimeMovie.identifier
            information.contentLength = byteCount
            information.isByteRangeAccessSupported = true
        }
        guard let dataRequest = request.dataRequest else {
            request.finishLoading()
            return true
        }
        let start = max(dataRequest.requestedOffset, dataRequest.currentOffset)
        let requestedEnd = dataRequest.requestedOffset.addingReportingOverflow(Int64(dataRequest.requestedLength))
        let end = dataRequest.requestsAllDataToEndOfResource || requestedEnd.overflow
            ? byteCount
            : min(requestedEnd.partialValue, byteCount)
        guard start >= 0, start <= end else {
            request.finishLoading(with: CocoaError(.fileReadUnknown))
            return true
        }
        let id = ObjectIdentifier(request)
        openRequests.insert(id)
        let loading = LoadingRequest(request)
        readQueue.async { [weak self] in
            self?.load(loading, id: id, from: start, to: end)
        }
        return true
    }

    func resourceLoader(_ resourceLoader: AVAssetResourceLoader, didCancel request: AVAssetResourceLoadingRequest) {
        openRequests.remove(ObjectIdentifier(request))
    }

    private func load(_ loading: LoadingRequest, id: ObjectIdentifier, from start: Int64, to end: Int64) {
        var offset = start
        do {
            while offset < end {
                guard isOpen(id) else { return }
                let count = Int(min(Int64(Self.chunkBytes), end - offset))
                // A request the player cancels, say after a seek during an outage, stops retrying,
                // so it neither holds a worker nor keeps the connection reported lost.
                let data = try connection.read(while: { isOpen(id) }) { try read(offset, count) }
                // The file ended before its recorded length: it changed under the player.
                guard !data.isEmpty else { throw CocoaError(.fileReadUnknown) }
                guard deliver(data, to: loading, id: id) else { return }
                offset += Int64(data.count)
            }
            finish(loading, id: id, error: nil)
        } catch {
            finish(loading, id: id, error: error)
        }
    }

    private func isOpen(_ id: ObjectIdentifier) -> Bool {
        delegateQueue.sync { !cancelled && openRequests.contains(id) }
    }

    private func deliver(_ data: Data, to loading: LoadingRequest, id: ObjectIdentifier) -> Bool {
        delegateQueue.sync {
            guard !cancelled, openRequests.contains(id), !loading.request.isCancelled else { return false }
            loading.request.dataRequest?.respond(with: data)
            return true
        }
    }

    private func finish(_ loading: LoadingRequest, id: ObjectIdentifier, error: Error?) {
        delegateQueue.async {
            guard self.openRequests.remove(id) != nil, !loading.request.isCancelled else { return }
            if let error {
                loading.request.finishLoading(with: error)
            } else {
                loading.request.finishLoading()
            }
        }
    }
}

private final class LoadingRequest: @unchecked Sendable {
    let request: AVAssetResourceLoadingRequest
    init(_ request: AVAssetResourceLoadingRequest) { self.request = request }
}

/// A read-only file descriptor read with `pread`.
private final class ShareFile: @unchecked Sendable {
    let byteCount: Int64
    private let descriptor: Int32

    init(url: URL) throws {
        let descriptor = open(url.path, O_RDONLY)
        guard descriptor >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        var info = stat()
        guard fstat(descriptor, &info) == 0 else {
            let code = errno
            close(descriptor)
            throw POSIXError(POSIXErrorCode(rawValue: code) ?? .EIO)
        }
        self.descriptor = descriptor
        byteCount = Int64(info.st_size)
    }

    deinit {
        close(descriptor)
    }

    func read(at offset: Int64, count: Int) throws -> Data {
        var data = Data(count: count)
        let got = data.withUnsafeMutableBytes { pread(descriptor, $0.baseAddress, count, off_t(offset)) }
        guard got >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        data.count = got
        return data
    }
}

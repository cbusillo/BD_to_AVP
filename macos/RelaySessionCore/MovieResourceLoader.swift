import AVFoundation
import Foundation
import UniformTypeIdentifiers

/// Delivers only verified ranges to AVFoundation. All loading-request access is
/// confined to queue; network tasks never retain an unbounded movie buffer.
final class MovieResourceLoader: NSObject, AVAssetResourceLoaderDelegate, @unchecked Sendable {
    typealias Read = @Sendable (Int64, Int) async throws -> Data
    let queue = DispatchQueue(label: "com.shinycomputers.bd-to-avp.movie-loader")
    private let movie: SharedMovie
    private let read: Read
    private struct Loading {
        let task: Task<Void, Never>
        let request: MovieLoadingRequest
    }
    private var tasks: [ObjectIdentifier: Loading] = [:]
    private var cancelled = false

    init(movie: SharedMovie, read: @escaping Read) throws {
        try movie.validate()
        self.movie = movie
        self.read = read
    }

    func makeAsset() -> AVURLAsset {
        let url = URL(string: "bdavpmovie://library/\(movie.id).\((movie.fileName as NSString).pathExtension.lowercased())")!
        let asset = AVURLAsset(url: url)
        asset.resourceLoader.setDelegate(self, queue: queue)
        return asset
    }

    func cancel() {
        queue.async {
            self.cancelled = true
            for loading in self.tasks.values {
                loading.task.cancel()
                if !loading.request.request.isCancelled {
                    loading.request.request.finishLoading(with: CancellationError())
                }
            }
            self.tasks.removeAll()
        }
    }

    func resourceLoader(_ resourceLoader: AVAssetResourceLoader, shouldWaitForLoadingOfRequestedResource request: AVAssetResourceLoadingRequest) -> Bool {
        guard !cancelled, tasks.count < 8 else {
            request.finishLoading(with: MovieLibraryError.unavailable)
            return true
        }
        if let information = request.contentInformationRequest {
            information.contentType = UTType(filenameExtension: (movie.fileName as NSString).pathExtension)?.identifier ?? UTType.mpeg4Movie.identifier
            information.contentLength = movie.byteCount
            information.isByteRangeAccessSupported = true
        }
        guard let dataRequest = request.dataRequest else { request.finishLoading(); return true }
        let offset = max(dataRequest.requestedOffset, dataRequest.currentOffset)
        let sum = dataRequest.requestedOffset.addingReportingOverflow(Int64(dataRequest.requestedLength))
        guard offset >= 0, offset <= movie.byteCount, dataRequest.requestedLength >= 0,
              dataRequest.requestsAllDataToEndOfResource || !sum.overflow
        else { request.finishLoading(with: MovieLibraryError.invalidRange); return true }
        let end = dataRequest.requestsAllDataToEndOfResource ? movie.byteCount : min(sum.partialValue, movie.byteCount)
        guard end >= offset else { request.finishLoading(with: MovieLibraryError.invalidRange); return true }
        let id = ObjectIdentifier(request)
        let loading = MovieLoadingRequest(request)
        let task = Task { [weak self, read] in
            do {
                var next = offset
                while next < end {
                    try Task.checkCancellation()
                    let count = Int(min(Int64(MovieLibraryContract.maximumChunkBytes), end - next))
                    let data = try await read(next, count)
                    try Task.checkCancellation()
                    guard data.count == count else { throw MovieLibraryError.invalidResponse }
                    guard let self else { throw CancellationError() }
                    let delivered = await self.deliver(data, to: loading, id: id)
                    guard delivered else { throw CancellationError() }
                    next += Int64(count)
                }
                self?.finish(loading, id: id, error: nil)
            } catch {
                self?.finish(loading, id: id, error: error)
            }
        }
        tasks[id] = Loading(task: task, request: loading)
        return true
    }

    func resourceLoader(_ resourceLoader: AVAssetResourceLoader, didCancel request: AVAssetResourceLoadingRequest) {
        tasks.removeValue(forKey: ObjectIdentifier(request))?.task.cancel()
    }

    private func deliver(_ data: Data, to loading: MovieLoadingRequest, id: ObjectIdentifier) async -> Bool {
        await withCheckedContinuation { continuation in
            queue.async {
                guard !self.cancelled, self.tasks[id] != nil, !loading.request.isCancelled else {
                    continuation.resume(returning: false); return
                }
                loading.request.dataRequest?.respond(with: data)
                continuation.resume(returning: true)
            }
        }
    }

    private func finish(_ loading: MovieLoadingRequest, id: ObjectIdentifier, error: Error?) {
        queue.async {
            guard self.tasks.removeValue(forKey: id) != nil, !loading.request.isCancelled else { return }
            if let error { loading.request.finishLoading(with: error) }
            else { loading.request.finishLoading() }
        }
    }
}

private final class MovieLoadingRequest: @unchecked Sendable {
    let request: AVAssetResourceLoadingRequest
    init(_ request: AVAssetResourceLoadingRequest) { self.request = request }
}

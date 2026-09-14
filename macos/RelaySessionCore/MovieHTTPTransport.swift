import Foundation

protocol MovieHTTPTransport: Sendable {
    func data(for request: URLRequest, maximumBytes: Int) async throws -> (Data, HTTPURLResponse)
}

struct BoundedMovieHTTPTransport: MovieHTTPTransport {
    private let configuration: @Sendable () -> URLSessionConfiguration

    init(configuration: @escaping @Sendable () -> URLSessionConfiguration = { .ephemeral }) {
        self.configuration = configuration
    }

    func data(for request: URLRequest, maximumBytes: Int) async throws -> (Data, HTTPURLResponse) {
        let transfer = MovieHTTPTransfer(maximumBytes: maximumBytes, configuration: configuration)
        return try await withTaskCancellationHandler {
            try await transfer.run(request)
        } onCancel: { transfer.cancel() }
    }
}

/// One bounded response, including cancellation before URLSession has started.
private final class MovieHTTPTransfer: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private let maximumBytes: Int
    private let configuration: @Sendable () -> URLSessionConfiguration
    private var continuation: CheckedContinuation<(Data, HTTPURLResponse), Error>?
    private var session: URLSession?
    private var response: HTTPURLResponse?
    private var data = Data()
    private var cancelled = false
    private var finished = false

    init(maximumBytes: Int, configuration: @escaping @Sendable () -> URLSessionConfiguration) {
        self.maximumBytes = maximumBytes
        self.configuration = configuration
    }

    func run(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            guard !cancelled else { lock.unlock(); continuation.resume(throwing: CancellationError()); return }
            self.continuation = continuation
            let config = configuration()
            config.timeoutIntervalForRequest = 15
            config.timeoutIntervalForResource = 30
            config.urlCache = nil
            config.httpCookieStorage = nil
            config.urlCredentialStorage = nil
            let session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
            self.session = session
            let task = session.dataTask(with: request)
            lock.unlock()
            task.resume()
        }
    }

    func cancel() {
        lock.withLock { cancelled = true }
        finish(.failure(CancellationError()))
    }

    private func finish(_ result: Result<(Data, HTTPURLResponse), Error>) {
        lock.lock()
        guard !finished else { lock.unlock(); return }
        finished = true
        let continuation = self.continuation
        let session = self.session
        self.continuation = nil
        self.session = nil
        data.removeAll()
        lock.unlock()
        session?.invalidateAndCancel()
        continuation?.resume(with: result)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
        finish(.failure(MovieLibraryError.invalidResponse))
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse, completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard let http = response as? HTTPURLResponse, response.url == dataTask.originalRequest?.url,
              response.expectedContentLength >= 0, response.expectedContentLength <= Int64(maximumBytes),
              (200...599).contains(http.statusCode)
        else { completionHandler(.cancel); finish(.failure(MovieLibraryError.invalidResponse)); return }
        lock.withLock { self.response = http }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        lock.lock()
        guard !finished, data.count <= maximumBytes - self.data.count else {
            lock.unlock(); finish(.failure(MovieLibraryError.invalidResponse)); return
        }
        self.data.append(data)
        lock.unlock()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error { finish(.failure(error)); return }
        let result: Result<(Data, HTTPURLResponse), Error> = lock.withLock {
            guard let response, response.expectedContentLength == Int64(data.count) else {
                return .failure(MovieLibraryError.invalidResponse)
            }
            return .success((data, response))
        }
        finish(result)
    }
}

import Foundation
import XCTest
@testable import BluRayToVisionPro

final class MovieHTTPTransportTests: XCTestCase {
    private var transport: BoundedMovieHTTPTransport {
        BoundedMovieHTTPTransport {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.protocolClasses = [MovieHTTPStub.self]
            return configuration
        }
    }

    func testAcceptsAnExactBoundedResponse() async throws {
        let (data, response) = try await transport.data(for: request("valid"), maximumBytes: 5)
        XCTAssertEqual(data, Data("movie".utf8))
        XCTAssertEqual(response.statusCode, 200)
    }

    func testRejectsOversizedHeaderOversizedBodyAndTruncation() async {
        for path in ["large-header", "large-body", "truncated"] {
            do {
                _ = try await transport.data(for: request(path), maximumBytes: 5)
                XCTFail("Accepted invalid response: \(path)")
            } catch { }
        }
    }

    func testCancellationBeforeTransportStartReturnsWithoutHanging() async {
        let transport = transport
        let request = request("valid")
        let task = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try await transport.data(for: request, maximumBytes: 5)
        }
        do { _ = try await task.value; XCTFail("Accepted a cancelled request") }
        catch is CancellationError { }
        catch { XCTFail("Unexpected cancellation error: \(error)") }
    }

    private func request(_ path: String) -> URLRequest {
        URLRequest(url: URL(string: "http://movie.local/\(path)")!)
    }
}

private final class MovieHTTPStub: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "movie.local" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() { }
    override func startLoading() {
        guard let url = request.url else { return }
        let path = url.lastPathComponent
        let length = path == "large-header" ? "1048576" : "5"
        let body = path == "large-body" ? Data(repeating: 0, count: 6)
            : path == "truncated" ? Data([0]) : Data("movie".utf8)
        let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: "HTTP/1.1", headerFields: ["Content-Length": length])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
}

import CryptoKit
import Foundation
import XCTest
@testable import BluRayToVisionPro

final class DiagnosticReportClientTests: XCTestCase {
    private let now = ISO8601DateFormatter().date(from: "2026-07-18T12:00:00Z")!

    override func tearDown() {
        DiagnosticURLProtocolStub.reset()
        super.tearDown()
    }

    func testInfoPlistConfigurationIsStrictHTTPSOriginOnly() throws {
        XCTAssertNil(DiagnosticServiceConfiguration.configured(infoDictionary: [:]))
        XCTAssertNil(
            DiagnosticServiceConfiguration.configured(
                infoDictionary: [DiagnosticServiceConfiguration.infoPlistKey: ""]
            )
        )
        XCTAssertNil(
            DiagnosticServiceConfiguration.configured(
                infoDictionary: [DiagnosticServiceConfiguration.infoPlistKey: "http://support.example"]
            )
        )

        for endpoint in [
            "http://support.example",
            "https://user:secret@support.example",
            "https://support.example/custom/path",
            "https://support.example?token=secret",
            "https://support.example#fragment",
        ] {
            XCTAssertThrowsError(try DiagnosticServiceConfiguration(endpointString: endpoint)) { error in
                XCTAssertEqual(error as? DiagnosticServiceConfigurationError, .invalidEndpoint)
            }
        }

        let configuration = try DiagnosticServiceConfiguration(endpointString: "https://SUPPORT.example:8443/")
        XCTAssertEqual(configuration.baseURL.absoluteString, "https://support.example:8443/")
        XCTAssertTrue(configuration.hasSameOrigin(as: URL(string: "https://support.example:8443/v1/reports/1")!))
        XCTAssertFalse(configuration.hasSameOrigin(as: URL(string: "https://support.example/v1/reports/1")!))
        XCTAssertFalse(configuration.hasSameOrigin(as: URL(string: "https://other.example:8443/v1/reports/1")!))
    }

    @MainActor
    func testUploadPostsMetadataThenPutsExactImmutableBytesAndDecodesReceipt() async throws {
        let artifactFixture = try makeArtifact(data: Data("immutable diagnostic zip bytes".utf8))
        defer { try? FileManager.default.removeItem(at: artifactFixture.directory) }
        let checksum = Self.sha256Hex(artifactFixture.data)
        let reportID = "8CDB329F-88DB-4E6B-AD68-B32BF25C06F5"
        let requestRecorder = LockedRequestRecorder()
        DiagnosticURLProtocolStub.requestHandler = { stub, request in
            let index = requestRecorder.append(request)
            if index == 0 {
                XCTAssertEqual(request.httpMethod, "POST")
                XCTAssertEqual(request.url?.absoluteString, "https://support.example/v1/reports")
                XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
                let body = try XCTUnwrap(Self.bodyData(from: request))
                let metadata = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: Any])
                XCTAssertEqual(metadata["bundle_schema_version"] as? Int, 1)
                XCTAssertEqual(metadata["content_type"] as? String, "application/zip")
                XCTAssertEqual(metadata["privacy_rules_version"] as? Int, 4)
                XCTAssertEqual(metadata["size_bytes"] as? Int, artifactFixture.data.count)
                XCTAssertEqual(metadata["sha256"] as? String, checksum)
                stub.respond(
                    statusCode: 201,
                    json: Self.createResponse(
                        reportID: reportID,
                        checksum: checksum,
                        size: artifactFixture.data.count
                    )
                )
            } else {
                XCTAssertEqual(index, 1)
                XCTAssertEqual(request.httpMethod, "PUT")
                XCTAssertEqual(
                    request.url?.absoluteString,
                    "https://support.example/v1/reports/\(reportID)/upload"
                )
                XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer upload-secret")
                XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/zip")
                XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Length"), String(artifactFixture.data.count))
                XCTAssertEqual(request.value(forHTTPHeaderField: "X-Content-SHA256"), checksum)
                XCTAssertEqual(try Self.bodyData(from: request), artifactFixture.data)
                stub.respond(
                    statusCode: 201,
                    json: [
                        "expires_at": "2026-08-17T12:00:00.000Z",
                        "report_id": reportID,
                        "status": "uploaded",
                    ]
                )
            }
        }
        let client = try makeClient()
        var progressValues: [Double] = []

        let receipt = try await client.upload(artifact: artifactFixture.artifact) { progress in
            progressValues.append(progress)
        }

        XCTAssertEqual(receipt.supportCode, "BDAVP-0123456789ABCDEF")
        XCTAssertEqual(receipt.expiresAt, ISO8601DateFormatter().date(from: "2026-08-17T12:00:00Z"))
        XCTAssertEqual(requestRecorder.count, 2)
        XCTAssertEqual(progressValues.first, 0)
        XCTAssertEqual(progressValues.last, 1)
    }

    @MainActor
    func testCrossOriginUploadURLIsRejectedBeforeBundleIsSent() async throws {
        let fixture = try makeArtifact(data: Data("private zip".utf8))
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let requestRecorder = LockedRequestRecorder()
        DiagnosticURLProtocolStub.requestHandler = { stub, request in
            _ = requestRecorder.append(request)
            let checksum = Self.sha256Hex(fixture.data)
            var response = Self.createResponse(
                reportID: "8CDB329F-88DB-4E6B-AD68-B32BF25C06F5",
                checksum: checksum,
                size: fixture.data.count
            )
            var upload = response["upload"] as! [String: Any]
            upload["url"] = "https://collector.example/v1/reports/8CDB329F-88DB-4E6B-AD68-B32BF25C06F5/upload"
            response["upload"] = upload
            stub.respond(statusCode: 201, json: response)
        }

        do {
            _ = try await makeClient().upload(artifact: fixture.artifact) { _ in }
            XCTFail("Expected the cross-origin upload URL to be rejected")
        } catch {
            XCTAssertEqual(error as? DiagnosticReportClientError, .unsafeServerResponse)
        }
        XCTAssertEqual(requestRecorder.count, 1)
    }

    @MainActor
    func testCrossOriginStatusURLIsRejectedBeforeBundleIsSent() async throws {
        let fixture = try makeArtifact(data: Data("private zip".utf8))
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let requestRecorder = LockedRequestRecorder()
        DiagnosticURLProtocolStub.requestHandler = { stub, request in
            _ = requestRecorder.append(request)
            let checksum = Self.sha256Hex(fixture.data)
            var response = Self.createResponse(
                reportID: "8CDB329F-88DB-4E6B-AD68-B32BF25C06F5",
                checksum: checksum,
                size: fixture.data.count
            )
            var status = response["status"] as! [String: Any]
            status["url"] = "https://collector.example/v1/reports/8CDB329F-88DB-4E6B-AD68-B32BF25C06F5/status"
            response["status"] = status
            stub.respond(statusCode: 201, json: response)
        }

        do {
            _ = try await makeClient().upload(artifact: fixture.artifact) { _ in }
            XCTFail("Expected the cross-origin status URL to be rejected")
        } catch {
            XCTAssertEqual(error as? DiagnosticReportClientError, .unsafeServerResponse)
        }
        XCTAssertEqual(requestRecorder.count, 1)
    }

    @MainActor
    func testUploadRedirectIsRejectedWithoutForwardingPrivateBundle() async throws {
        let fixture = try makeArtifact(data: Data("redirect protected zip".utf8))
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let checksum = Self.sha256Hex(fixture.data)
        let reportID = "8CDB329F-88DB-4E6B-AD68-B32BF25C06F5"
        let requestRecorder = LockedRequestRecorder()
        DiagnosticURLProtocolStub.requestHandler = { stub, request in
            let index = requestRecorder.append(request)
            XCTAssertNotEqual(request.url?.host, "collector.example")
            if index == 0 {
                stub.respond(
                    statusCode: 201,
                    json: Self.createResponse(
                        reportID: reportID,
                        checksum: checksum,
                        size: fixture.data.count
                    )
                )
            } else {
                stub.respond(
                    statusCode: 307,
                    headers: ["Location": "https://collector.example/forward"]
                )
            }
        }

        do {
            _ = try await makeClient().upload(artifact: fixture.artifact) { _ in }
            XCTFail("Expected redirect rejection")
        } catch {
            XCTAssertEqual(error as? DiagnosticReportClientError, .unsafeServerResponse)
        }
        XCTAssertEqual(requestRecorder.count, 2)
    }

    @MainActor
    func testCancellingInFlightUploadCancelsOnlyNetworkTask() async throws {
        let fixture = try makeArtifact(data: Data(repeating: 0x5A, count: 64 * 1_024))
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let checksum = Self.sha256Hex(fixture.data)
        let reportID = "8CDB329F-88DB-4E6B-AD68-B32BF25C06F5"
        let uploadStarted = expectation(description: "upload started")
        let uploadStopped = expectation(description: "upload stopped")
        let requestRecorder = LockedRequestRecorder()
        DiagnosticURLProtocolStub.requestHandler = { stub, request in
            let index = requestRecorder.append(request)
            if index == 0 {
                stub.respond(
                    statusCode: 201,
                    json: Self.createResponse(
                        reportID: reportID,
                        checksum: checksum,
                        size: fixture.data.count
                    )
                )
            } else {
                uploadStarted.fulfill()
            }
        }
        DiagnosticURLProtocolStub.stopHandler = { request in
            if request.httpMethod == "PUT" {
                uploadStopped.fulfill()
            }
        }
        let task = Task {
            try await makeClient().upload(artifact: fixture.artifact) { _ in }
        }

        await fulfillment(of: [uploadStarted], timeout: 2)
        task.cancel()

        do {
            _ = try await task.value
            XCTFail("Expected cancellation")
        } catch {
            XCTAssertEqual(error as? DiagnosticReportClientError, .cancelled)
        }
        await fulfillment(of: [uploadStopped], timeout: 2)
        XCTAssertEqual(requestRecorder.count, 2)
        XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.artifact.archiveURL.path))
    }

    @MainActor
    func testNetworkAndServerFailuresMapToBoundedSafeErrors() async throws {
        let fixture = try makeArtifact(data: Data("safe errors".utf8))
        defer { try? FileManager.default.removeItem(at: fixture.directory) }

        DiagnosticURLProtocolStub.requestHandler = { stub, _ in
            stub.fail(URLError(.notConnectedToInternet))
        }
        do {
            _ = try await makeClient().upload(artifact: fixture.artifact) { _ in }
            XCTFail("Expected offline failure")
        } catch {
            XCTAssertEqual(error as? DiagnosticReportClientError, .offline)
            XCTAssertFalse(error.localizedDescription.contains("support.example"))
        }

        DiagnosticURLProtocolStub.requestHandler = { stub, _ in
            stub.respond(statusCode: 429, json: ["error": "private-server-detail"])
        }
        do {
            _ = try await makeClient().upload(artifact: fixture.artifact) { _ in }
            XCTFail("Expected rate-limit failure")
        } catch {
            XCTAssertEqual(error as? DiagnosticReportClientError, .rateLimited)
            XCTAssertFalse(error.localizedDescription.contains("private-server-detail"))
        }

        DiagnosticURLProtocolStub.requestHandler = { stub, _ in
            stub.respond(statusCode: 422, json: ["error": "unsupported_privacy_rules_version"])
        }
        do {
            _ = try await makeClient().upload(artifact: fixture.artifact) { _ in }
            XCTFail("Expected rejected-bundle failure")
        } catch {
            XCTAssertEqual(error as? DiagnosticReportClientError, .bundleRejected)
            XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.artifact.archiveURL.path))
        }
    }

    private func makeClient() throws -> DiagnosticReportClient {
        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.protocolClasses = [DiagnosticURLProtocolStub.self]
        return DiagnosticReportClient(
            configuration: try DiagnosticServiceConfiguration(endpointString: "https://support.example"),
            sessionConfiguration: sessionConfiguration,
            clock: { self.now }
        )
    }

    private func makeArtifact(data: Data) throws -> (artifact: DiagnosticBundleArtifact, data: Data, directory: URL) {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let archiveURL = directory.appendingPathComponent("diagnostics.zip", isDirectory: false)
        try data.write(to: archiveURL)
        let preview = DiagnosticBundlePreview(
            includedCategories: ["worker state"],
            excludedCategories: ["source media"],
            files: [],
            truncationNotices: [],
            archiveBytes: data.count,
            maximumArchiveBytes: 2 * 1_024 * 1_024
        )
        return (
            DiagnosticBundleArtifact(
                bundleID: UUID(),
                createdAt: now,
                archiveURL: archiveURL,
                suggestedFilename: "diagnostics.zip",
                preview: preview
            ),
            data,
            directory
        )
    }

    private static func createResponse(
        reportID: String,
        checksum: String,
        size: Int
    ) -> [String: Any] {
        [
            "expires_at": "2026-08-17T12:00:00.000Z",
            "report_id": reportID,
            "schema_version": 1,
            "status": [
                "expires_at": "2026-07-18T12:10:00.000Z",
                "headers": ["Authorization": "Bearer status-secret"],
                "method": "GET",
                "url": "https://support.example/v1/reports/\(reportID)/status",
            ],
            "support_code": "BDAVP-0123456789ABCDEF",
            "upload": [
                "expires_at": "2026-07-18T12:10:00.000Z",
                "headers": [
                    "Authorization": "Bearer upload-secret",
                    "Content-Length": String(size),
                    "Content-Type": "application/zip",
                    "X-Content-SHA256": checksum,
                ],
                "method": "PUT",
                "url": "https://support.example/v1/reports/\(reportID)/upload",
            ],
        ]
    }

    private static func bodyData(from request: URLRequest) throws -> Data? {
        if let body = request.httpBody {
            return body
        }
        guard let stream = request.httpBodyStream else {
            return nil
        }
        stream.open()
        defer { stream.close() }
        var data = Data()
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: 16 * 1_024)
        defer { buffer.deallocate() }
        while stream.hasBytesAvailable {
            let count = stream.read(buffer, maxLength: 16 * 1_024)
            if count < 0 {
                throw stream.streamError ?? URLError(.cannotDecodeContentData)
            }
            if count == 0 {
                break
            }
            data.append(buffer, count: count)
        }
        return data
    }

    private static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}

private final class LockedRequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var requests: [URLRequest] = []

    var count: Int {
        lock.withLock { requests.count }
    }

    @discardableResult
    func append(_ request: URLRequest) -> Int {
        lock.withLock {
            requests.append(request)
            return requests.count - 1
        }
    }
}

private final class DiagnosticURLProtocolStub: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var storedRequestHandler: ((DiagnosticURLProtocolStub, URLRequest) throws -> Void)?
    nonisolated(unsafe) private static var storedStopHandler: ((URLRequest) -> Void)?

    static var requestHandler: ((DiagnosticURLProtocolStub, URLRequest) throws -> Void)? {
        get { lock.withLock { storedRequestHandler } }
        set { lock.withLock { storedRequestHandler = newValue } }
    }

    static var stopHandler: ((URLRequest) -> Void)? {
        get { lock.withLock { storedStopHandler } }
        set { lock.withLock { storedStopHandler = newValue } }
    }

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        guard let handler = Self.requestHandler else {
            fail(URLError(.badServerResponse))
            return
        }
        do {
            try handler(self, request)
        } catch {
            fail(error)
        }
    }

    override func stopLoading() {
        Self.stopHandler?(request)
    }

    func respond(
        statusCode: Int,
        headers: [String: String] = [:],
        data: Data = Data()
    ) {
        var responseHeaders = headers
        if responseHeaders["Content-Type"] == nil, !data.isEmpty {
            responseHeaders["Content-Type"] = "application/json"
        }
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: responseHeaders
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if !data.isEmpty {
            client?.urlProtocol(self, didLoad: data)
        }
        client?.urlProtocolDidFinishLoading(self)
    }

    func respond(
        statusCode: Int,
        headers: [String: String] = [:],
        json: [String: Any]
    ) {
        let data = try! JSONSerialization.data(withJSONObject: json, options: [.sortedKeys])
        respond(statusCode: statusCode, headers: headers, data: data)
    }

    func fail(_ error: Error) {
        client?.urlProtocol(self, didFailWithError: error)
    }

    static func reset() {
        lock.withLock {
            storedRequestHandler = nil
            storedStopHandler = nil
        }
    }
}

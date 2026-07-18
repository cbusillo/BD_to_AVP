import CryptoKit
import Foundation

enum DiagnosticServiceConfigurationError: Error, Equatable, Sendable {
    case invalidEndpoint
}

struct DiagnosticServiceConfiguration: Equatable, Sendable {
    static let infoPlistKey = "BDToAVPSupportDiagnosticsEndpoint"

    let baseURL: URL

    init(endpointString: String) throws {
        let trimmedEndpoint = endpointString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedEndpoint.isEmpty, let endpointURL = URL(string: trimmedEndpoint) else {
            throw DiagnosticServiceConfigurationError.invalidEndpoint
        }
        try self.init(endpointURL: endpointURL)
    }

    init(endpointURL: URL) throws {
        guard var components = URLComponents(url: endpointURL, resolvingAgainstBaseURL: false),
              components.scheme?.lowercased() == "https",
              let host = components.host,
              !host.isEmpty,
              components.user == nil,
              components.password == nil,
              components.query == nil,
              components.fragment == nil,
              components.path.isEmpty || components.path == "/"
        else {
            throw DiagnosticServiceConfigurationError.invalidEndpoint
        }

        components.scheme = "https"
        components.host = host.lowercased()
        components.path = "/"
        guard let normalizedURL = components.url else {
            throw DiagnosticServiceConfigurationError.invalidEndpoint
        }
        baseURL = normalizedURL
    }

    static func configured(
        infoDictionary: [String: Any] = Bundle.main.infoDictionary ?? [:]
    ) -> DiagnosticServiceConfiguration? {
        guard let endpoint = infoDictionary[infoPlistKey] as? String,
              !endpoint.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else {
            return nil
        }
        return try? DiagnosticServiceConfiguration(endpointString: endpoint)
    }

    var createReportURL: URL {
        baseURL.appendingPathComponent("v1/reports", isDirectory: false)
    }

    func hasSameOrigin(as candidateURL: URL) -> Bool {
        guard let baseComponents = URLComponents(url: baseURL, resolvingAgainstBaseURL: false),
              let candidateComponents = URLComponents(url: candidateURL, resolvingAgainstBaseURL: false),
              candidateComponents.scheme?.lowercased() == "https",
              candidateComponents.user == nil,
              candidateComponents.password == nil,
              candidateComponents.fragment == nil,
              let baseHost = baseComponents.host?.lowercased(),
              let candidateHost = candidateComponents.host?.lowercased()
        else {
            return false
        }
        return baseHost == candidateHost
            && (baseComponents.port ?? 443) == (candidateComponents.port ?? 443)
    }
}

struct DiagnosticReportReceipt: Equatable, Sendable {
    let supportCode: String
    let expiresAt: Date
}

enum DiagnosticReportClientError: Error, Equatable, LocalizedError, Sendable {
    case cancelled
    case offline
    case timedOut
    case rateLimited
    case serviceUnavailable
    case bundleRejected
    case bundleTooLarge
    case authorizationExpired
    case unsafeServerResponse
    case cannotReadBundle

    var errorDescription: String? {
        switch self {
        case .cancelled:
            return "The diagnostic upload was cancelled. The local copy is still available."
        case .offline:
            return "The Mac appears to be offline. Save or share the local diagnostic copy instead."
        case .timedOut:
            return "The diagnostic service did not respond in time. The local copy is still available."
        case .rateLimited:
            return "The diagnostic service is temporarily busy. Wait a moment, then retry with a new report."
        case .serviceUnavailable:
            return "The diagnostic service is temporarily unavailable. Save or share the local copy instead."
        case .bundleRejected:
            return "The diagnostic service could not accept this bundle. Save the local copy for support."
        case .bundleTooLarge:
            return "The diagnostic bundle exceeds the service size limit. Save the local copy for support."
        case .authorizationExpired:
            return "The upload authorization expired. Retry to create a new report."
        case .unsafeServerResponse:
            return "The diagnostic service returned an invalid response. The local copy was not forwarded."
        case .cannotReadBundle:
            return "The local diagnostic bundle could not be read. Capture a new copy and try again."
        }
    }
}

protocol DiagnosticReportUploading: Sendable {
    func upload(
        artifact: DiagnosticBundleArtifact,
        progress: @escaping @MainActor @Sendable (Double) -> Void
    ) async throws -> DiagnosticReportReceipt
}

final class DiagnosticReportClient: DiagnosticReportUploading, @unchecked Sendable {
    private static let contentType = "application/zip"
    private static let maximumBundleBytes = 2 * 1_024 * 1_024
    private static let maximumResponseBytes = 64 * 1_024

    private let configuration: DiagnosticServiceConfiguration
    private let session: URLSession
    private let clock: @Sendable () -> Date

    init(
        configuration: DiagnosticServiceConfiguration,
        sessionConfiguration: URLSessionConfiguration? = nil,
        clock: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.configuration = configuration
        self.clock = clock
        session = URLSession(configuration: Self.hardenedSessionConfiguration(sessionConfiguration))
    }

    deinit {
        session.invalidateAndCancel()
    }

    func upload(
        artifact: DiagnosticBundleArtifact,
        progress: @escaping @MainActor @Sendable (Double) -> Void
    ) async throws -> DiagnosticReportReceipt {
        try Self.requireNotCancelled()

        let archiveData: Data
        do {
            archiveData = try Data(contentsOf: artifact.archiveURL)
        } catch {
            throw DiagnosticReportClientError.cannotReadBundle
        }
        guard !archiveData.isEmpty,
              archiveData.count <= Self.maximumBundleBytes,
              archiveData.count == artifact.preview.archiveBytes
        else {
            throw archiveData.count > Self.maximumBundleBytes
                ? DiagnosticReportClientError.bundleTooLarge
                : DiagnosticReportClientError.cannotReadBundle
        }

        let checksum = Self.sha256Hex(archiveData)
        let pendingUpload = try await createReport(
            sizeBytes: archiveData.count,
            checksum: checksum
        )

        try Self.requireNotCancelled()
        await progress(0)
        let receipt = try await uploadBundle(
            archiveData,
            pendingUpload: pendingUpload,
            progress: progress
        )
        await progress(1)
        return receipt
    }

    private func createReport(sizeBytes: Int, checksum: String) async throws -> PendingUpload {
        let payload = CreateReportRequest(
            bundleSchemaVersion: 1,
            contentType: Self.contentType,
            sha256: checksum,
            sizeBytes: sizeBytes
        )
        let requestData: Data
        do {
            requestData = try JSONEncoder().encode(payload)
        } catch {
            throw DiagnosticReportClientError.unsafeServerResponse
        }

        var request = URLRequest(
            url: configuration.createReportURL,
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: 30
        )
        request.httpMethod = "POST"
        request.httpBody = requestData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(String(requestData.count), forHTTPHeaderField: "Content-Length")

        let responseData: Data
        let response: URLResponse
        do {
            let taskDelegate = DiagnosticRequestTaskDelegate()
            (responseData, response) = try await session.data(for: request, delegate: taskDelegate)
        } catch {
            throw Self.mapTransportError(error)
        }

        _ = try Self.validateJSONResponse(
            response,
            data: responseData,
            expectedStatus: 201
        )
        let decoded: CreateReportResponse = try Self.decodeResponse(responseData)
        return try validateCreateResponse(decoded, expectedSize: sizeBytes, expectedChecksum: checksum)
    }

    private func uploadBundle(
        _ archiveData: Data,
        pendingUpload: PendingUpload,
        progress: @escaping @MainActor @Sendable (Double) -> Void
    ) async throws -> DiagnosticReportReceipt {
        var request = URLRequest(
            url: pendingUpload.uploadURL,
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: 120
        )
        request.httpMethod = "PUT"
        for (header, value) in pendingUpload.headers {
            request.setValue(value, forHTTPHeaderField: header)
        }

        let responseData: Data
        let response: URLResponse
        do {
            let taskDelegate = DiagnosticRequestTaskDelegate(expectedUploadBytes: archiveData.count) { fraction in
                Task { @MainActor in
                    progress(fraction)
                }
            }
            (responseData, response) = try await session.upload(
                for: request,
                from: archiveData,
                delegate: taskDelegate
            )
        } catch {
            throw Self.mapTransportError(error)
        }

        _ = try Self.validateJSONResponse(response, data: responseData, expectedStatus: 201)
        let decoded: UploadReportResponse = try Self.decodeResponse(responseData)
        guard decoded.reportID == pendingUpload.reportID,
              decoded.status == "uploaded",
              decoded.expiresAt == pendingUpload.expiresAt,
              decoded.expiresAt > clock()
        else {
            throw DiagnosticReportClientError.unsafeServerResponse
        }
        return DiagnosticReportReceipt(
            supportCode: pendingUpload.supportCode,
            expiresAt: decoded.expiresAt
        )
    }

    private func validateCreateResponse(
        _ response: CreateReportResponse,
        expectedSize: Int,
        expectedChecksum: String
    ) throws -> PendingUpload {
        guard response.schemaVersion == 1,
              UUID(uuidString: response.reportID) != nil,
              Self.isValidSupportCode(response.supportCode),
              response.expiresAt > clock()
        else {
            throw DiagnosticReportClientError.unsafeServerResponse
        }

        let expectedBasePath = "/v1/reports/\(response.reportID)"
        _ = try validateAuthorizedRequest(
            response.status,
            method: "GET",
            expectedPath: "\(expectedBasePath)/status",
            requiredHeaders: ["Authorization": nil]
        )
        let upload = try validateAuthorizedRequest(
            response.upload,
            method: "PUT",
            expectedPath: "\(expectedBasePath)/upload",
            requiredHeaders: [
                "Authorization": nil,
                "Content-Length": String(expectedSize),
                "Content-Type": Self.contentType,
                "X-Content-SHA256": expectedChecksum,
            ]
        )
        return PendingUpload(
            reportID: response.reportID,
            supportCode: response.supportCode,
            expiresAt: response.expiresAt,
            uploadURL: upload.url,
            headers: upload.headers
        )
    }

    private func validateAuthorizedRequest(
        _ authorizedRequest: AuthorizedRequest,
        method: String,
        expectedPath: String,
        requiredHeaders: [String: String?]
    ) throws -> ValidatedAuthorizedRequest {
        guard authorizedRequest.method == method,
              authorizedRequest.expiresAt > clock(),
              authorizedRequest.url.count <= 2_048,
              let url = URL(string: authorizedRequest.url),
              configuration.hasSameOrigin(as: url),
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.path == expectedPath,
              components.query == nil,
              components.fragment == nil
        else {
            throw DiagnosticReportClientError.unsafeServerResponse
        }

        let headers = try Self.validateHeaders(authorizedRequest.headers)
        let requiredHeaderNames = Set(requiredHeaders.keys.map { $0.lowercased() })
        let actualHeaderNames = Set(headers.keys.map { $0.lowercased() })
        guard requiredHeaderNames == actualHeaderNames else {
            throw DiagnosticReportClientError.unsafeServerResponse
        }
        for (requiredName, requiredValue) in requiredHeaders {
            guard let actualValue = Self.headerValue(named: requiredName, in: headers) else {
                throw DiagnosticReportClientError.unsafeServerResponse
            }
            if let requiredValue, actualValue != requiredValue {
                throw DiagnosticReportClientError.unsafeServerResponse
            }
        }
        guard let authorization = Self.headerValue(named: "Authorization", in: headers),
              authorization.hasPrefix("Bearer "),
              authorization.dropFirst("Bearer ".count).isEmpty == false
        else {
            throw DiagnosticReportClientError.unsafeServerResponse
        }
        return ValidatedAuthorizedRequest(url: url, headers: headers)
    }

    private static func hardenedSessionConfiguration(
        _ suppliedConfiguration: URLSessionConfiguration?
    ) -> URLSessionConfiguration {
        let configuration = (suppliedConfiguration?.copy() as? URLSessionConfiguration) ?? .ephemeral
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.urlCredentialStorage = nil
        configuration.waitsForConnectivity = false
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 120
        configuration.tlsMinimumSupportedProtocolVersion = .TLSv12
        return configuration
    }

    private static func validateJSONResponse(
        _ response: URLResponse,
        data: Data,
        expectedStatus: Int
    ) throws -> HTTPURLResponse {
        guard data.count <= maximumResponseBytes,
              let httpResponse = response as? HTTPURLResponse
        else {
            throw DiagnosticReportClientError.unsafeServerResponse
        }
        guard httpResponse.statusCode == expectedStatus else {
            throw mapHTTPStatus(httpResponse.statusCode)
        }
        let contentType = httpResponse.value(forHTTPHeaderField: "Content-Type")?
            .split(separator: ";", maxSplits: 1)
            .first?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        guard contentType == "application/json" else {
            throw DiagnosticReportClientError.unsafeServerResponse
        }
        return httpResponse
    }

    private static func decodeResponse<T: Decodable>(_ data: Data) throws -> T {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let value = try container.decode(String.self)
            guard let date = parseISO8601(value) else {
                throw DecodingError.dataCorruptedError(
                    in: container,
                    debugDescription: "Invalid ISO-8601 date"
                )
            }
            return date
        }
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw DiagnosticReportClientError.unsafeServerResponse
        }
    }

    private static func validateHeaders(_ headers: [String: String]) throws -> [String: String] {
        guard !headers.isEmpty, headers.count <= 16 else {
            throw DiagnosticReportClientError.unsafeServerResponse
        }
        var normalizedNames = Set<String>()
        var totalBytes = 0
        for (name, value) in headers {
            let normalizedName = name.lowercased()
            guard !name.isEmpty,
                  name.count <= 64,
                  value.count <= 4_096,
                  name.unicodeScalars.allSatisfy({ scalar in
                      scalar.value > 32 && scalar.value < 127 && scalar.value != 58
                  }),
                  !value.contains("\r"),
                  !value.contains("\n"),
                  normalizedNames.insert(normalizedName).inserted
            else {
                throw DiagnosticReportClientError.unsafeServerResponse
            }
            totalBytes += name.utf8.count + value.utf8.count
        }
        guard totalBytes <= 8_192 else {
            throw DiagnosticReportClientError.unsafeServerResponse
        }
        return headers
    }

    private static func headerValue(named name: String, in headers: [String: String]) -> String? {
        headers.first { $0.key.caseInsensitiveCompare(name) == .orderedSame }?.value
    }

    private static func mapHTTPStatus(_ statusCode: Int) -> DiagnosticReportClientError {
        switch statusCode {
        case 408:
            return .timedOut
        case 409, 410:
            return .authorizationExpired
        case 413:
            return .bundleTooLarge
        case 429:
            return .rateLimited
        case 400, 404, 415, 422:
            return .bundleRejected
        case 500 ... 599, 401, 403:
            return .serviceUnavailable
        default:
            return .unsafeServerResponse
        }
    }

    private static func mapTransportError(_ error: Error) -> DiagnosticReportClientError {
        if Task.isCancelled || error is CancellationError {
            return .cancelled
        }
        guard let urlError = error as? URLError else {
            return .serviceUnavailable
        }
        switch urlError.code {
        case .cancelled:
            return .cancelled
        case .notConnectedToInternet, .networkConnectionLost, .dataNotAllowed, .internationalRoamingOff:
            return .offline
        case .timedOut:
            return .timedOut
        default:
            return .serviceUnavailable
        }
    }

    private static func requireNotCancelled() throws {
        if Task.isCancelled {
            throw DiagnosticReportClientError.cancelled
        }
    }

    private static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private static func parseISO8601(_ value: String) -> Date? {
        let fractionalFormatter = ISO8601DateFormatter()
        fractionalFormatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractionalFormatter.date(from: value) {
            return date
        }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }

    private static func isValidSupportCode(_ supportCode: String) -> Bool {
        guard supportCode.hasPrefix("BDAVP-") else {
            return false
        }
        let identifier = supportCode.dropFirst("BDAVP-".count)
        guard identifier.count == 16 else {
            return false
        }
        return identifier.allSatisfy { character in
            character.isASCII && (character.isUppercase || character.isNumber)
        }
    }
}

private final class DiagnosticRequestTaskDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private let expectedUploadBytes: Int64?
    private let progress: (@Sendable (Double) -> Void)?

    init(
        expectedUploadBytes: Int? = nil,
        progress: (@Sendable (Double) -> Void)? = nil
    ) {
        self.expectedUploadBytes = expectedUploadBytes.map(Int64.init)
        self.progress = progress
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didSendBodyData bytesSent: Int64,
        totalBytesSent: Int64,
        totalBytesExpectedToSend: Int64
    ) {
        guard let expectedUploadBytes, expectedUploadBytes > 0 else {
            return
        }
        progress?(min(max(Double(totalBytesSent) / Double(expectedUploadBytes), 0), 1))
    }
}

private struct CreateReportRequest: Encodable {
    let bundleSchemaVersion: Int
    let contentType: String
    let sha256: String
    let sizeBytes: Int

    enum CodingKeys: String, CodingKey {
        case bundleSchemaVersion = "bundle_schema_version"
        case contentType = "content_type"
        case sha256
        case sizeBytes = "size_bytes"
    }
}

private struct CreateReportResponse: Decodable {
    let expiresAt: Date
    let reportID: String
    let schemaVersion: Int
    let status: AuthorizedRequest
    let supportCode: String
    let upload: AuthorizedRequest

    enum CodingKeys: String, CodingKey {
        case expiresAt = "expires_at"
        case reportID = "report_id"
        case schemaVersion = "schema_version"
        case status
        case supportCode = "support_code"
        case upload
    }
}

private struct AuthorizedRequest: Decodable {
    let expiresAt: Date
    let headers: [String: String]
    let method: String
    let url: String

    enum CodingKeys: String, CodingKey {
        case expiresAt = "expires_at"
        case headers
        case method
        case url
    }
}

private struct UploadReportResponse: Decodable {
    let expiresAt: Date
    let reportID: String
    let status: String

    enum CodingKeys: String, CodingKey {
        case expiresAt = "expires_at"
        case reportID = "report_id"
        case status
    }
}

private struct ValidatedAuthorizedRequest {
    let url: URL
    let headers: [String: String]
}

private struct PendingUpload {
    let reportID: String
    let supportCode: String
    let expiresAt: Date
    let uploadURL: URL
    let headers: [String: String]
}

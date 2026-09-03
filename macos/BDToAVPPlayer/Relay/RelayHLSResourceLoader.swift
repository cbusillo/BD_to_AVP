import AVFoundation
import Foundation

protocol RelayRequestSigning: Sendable {
    var sessionID: RelaySessionIdentifier { get }
    var expirationDate: Date { get }
    var mediaCapability: RelayMediaCapability { get }
    func signRelayRequest(
        method: String,
        requestTarget: String,
        body: Data,
        timestamp: Date,
        nonce: String
    ) throws -> RelayAuthenticatedRequest
    func verifyRelayResponse(
        _ response: RelayAuthenticatedResponse,
        requestNonce: String,
        statusCode: Int,
        body: Data,
        now: Date
    ) throws
}

extension RelayEstablishedSession: RelayRequestSigning {
    func signRelayRequest(
        method: String,
        requestTarget: String,
        body: Data,
        timestamp: Date,
        nonce: String
    ) throws -> RelayAuthenticatedRequest {
        try signRequest(
            method: method,
            requestTarget: requestTarget,
            timestamp: timestamp,
            nonce: nonce,
            body: body
        )
    }

    func verifyRelayResponse(
        _ response: RelayAuthenticatedResponse,
        requestNonce: String,
        statusCode: Int,
        body: Data,
        now: Date
    ) throws {
        try verifyResponse(
            response,
            requestNonce: requestNonce,
            actualStatusCode: statusCode,
            body: body,
            now: now
        )
    }
}

struct RelayPreparedRequest: Sendable {
    let request: URLRequest
    let authentication: RelayAuthenticatedRequest
}

enum RelayAuthenticatedRequestFactory {
    static func makeRequest(
        baseURL: URL,
        path: String,
        signer: any RelayRequestSigning,
        clock: @escaping @Sendable () -> Date,
        nonce: @escaping @Sendable () -> String
    ) throws -> RelayPreparedRequest {
        guard signer.expirationDate > clock() else {
            throw RelayTransportError.sessionExpired
        }
        let url = baseURL.appendingPathComponent(path.dropFirst().description)
        let target = url.path + (url.query.map { "?\($0)" } ?? "")
        let signed = try signer.signRelayRequest(
            method: "GET",
            requestTarget: target,
            body: Data(),
            timestamp: clock(),
            nonce: nonce()
        )
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue(
            try JSONEncoder().encode(signed).base64EncodedString(),
            forHTTPHeaderField: RelayWireContract.authenticationHeader
        )
        request.setValue(signer.mediaCapability.value, forHTTPHeaderField: RelayWireContract.mediaCapabilityHeader)
        return RelayPreparedRequest(request: request, authentication: signed)
    }

    static func makeResourceRequest(
        customURL: URL,
        baseURL: URL,
        signer: any RelayRequestSigning,
        clock: @escaping @Sendable () -> Date,
        nonce: @escaping @Sendable () -> String
    ) throws -> RelayPreparedRequest {
        guard let resolved = RelayHLSResourceLoader.resolveURL(customURL, serverBaseURL: baseURL) else {
            throw RelayTransportError.invalidRelayURL
        }
        return try makeRequest(
            baseURL: baseURL,
            path: resolved.path,
            signer: signer,
            clock: clock,
            nonce: nonce
        )
    }
}

enum RelayAuthenticatedResponseVerifier {
    static func verify(
        data: Data,
        response: HTTPURLResponse,
        request: RelayPreparedRequest,
        signer: any RelayRequestSigning,
        now: Date
    ) throws {
        guard let encodedAuthentication = response.value(
            forHTTPHeaderField: RelayWireContract.responseAuthenticationHeader
        ), encodedAuthentication.utf8.count <= RelayWireContract.maximumAuthenticationHeaderBytes,
        let authenticationData = Data(base64Encoded: encodedAuthentication),
        authenticationData.count <= RelayWireContract.maximumAuthenticationHeaderBytes
        else {
            throw RelaySessionError.invalidResponse
        }
        let authentication: RelayAuthenticatedResponse
        do {
            authentication = try JSONDecoder().decode(RelayAuthenticatedResponse.self, from: authenticationData)
        } catch {
            throw RelaySessionError.invalidResponse
        }
        try signer.verifyRelayResponse(
            authentication,
            requestNonce: request.authentication.nonce,
            statusCode: response.statusCode,
            body: data,
            now: now
        )
    }
}

final class RelayAuthenticatedResourceClient: @unchecked Sendable {
    private let signer: any RelayRequestSigning
    private let transport: any RelayTransport
    private let serverBaseURL: URL
    private let clock: @Sendable () -> Date
    private let nonce: @Sendable () -> String
    private let maximumTransientRetries: Int

    init(
        signer: any RelayRequestSigning,
        transport: any RelayTransport,
        serverBaseURL: URL,
        clock: @escaping @Sendable () -> Date = { Date() },
        nonce: @escaping @Sendable () -> String = { UUID().uuidString },
        maximumTransientRetries: Int = 1
    ) {
        self.signer = signer
        self.transport = transport
        self.serverBaseURL = serverBaseURL
        self.clock = clock
        self.nonce = nonce
        self.maximumTransientRetries = min(max(maximumTransientRetries, 0), 2)
    }

    func load(_ customURL: URL) async throws -> (Data, HTTPURLResponse) {
        var retryCount = 0
        while true {
            do {
                let request = try RelayAuthenticatedRequestFactory.makeResourceRequest(
                    customURL: customURL,
                    baseURL: serverBaseURL,
                    signer: signer,
                    clock: clock,
                    nonce: nonce
                )
                let result = try await transport.data(for: request.request)
                try RelayAuthenticatedResponseVerifier.verify(
                    data: result.0,
                    response: result.1,
                    request: request,
                    signer: signer,
                    now: clock()
                )
                switch result.1.statusCode {
                case 200:
                    return result
                case 410:
                    throw RelayTransportError.sessionExpired
                case 503:
                    throw RelayTransportError.unpaired
                default:
                    throw RelayTransportError.unexpectedStatusCode(result.1.statusCode)
                }
            } catch {
                guard retryCount < maximumTransientRetries, isTransient(error) else {
                    throw error
                }
                retryCount += 1
            }
        }
    }

    private func isTransient(_ error: Error) -> Bool {
        guard let error = error as? URLError else { return false }
        switch error.code {
        case .timedOut, .networkConnectionLost, .notConnectedToInternet:
            return true
        default:
            return false
        }
    }
}

final class RelayHLSResourceLoader: NSObject, AVAssetResourceLoaderDelegate, @unchecked Sendable {
    static let customScheme = "bdtoavprelay"

    private let resourceClient: RelayAuthenticatedResourceClient
    private let lock = NSLock()
    private var activeTasks: [ObjectIdentifier: Task<Void, Never>] = [:]

    init(
        signer: any RelayRequestSigning,
        transport: any RelayTransport,
        serverBaseURL: URL,
        clock: @escaping @Sendable () -> Date = { Date() },
        nonce: @escaping @Sendable () -> String = { UUID().uuidString }
    ) {
        resourceClient = RelayAuthenticatedResourceClient(
            signer: signer,
            transport: transport,
            serverBaseURL: serverBaseURL,
            clock: clock,
            nonce: nonce
        )
    }

    func resourceLoader(
        _ resourceLoader: AVAssetResourceLoader,
        shouldWaitForLoadingOfRequestedResource loadingRequest: AVAssetResourceLoadingRequest
    ) -> Bool {
        guard let url = loadingRequest.request.url, url.scheme == Self.customScheme else { return false }
        let key = ObjectIdentifier(loadingRequest)
        let task = Task { [weak self, key] in
            guard let self else { return }
            await self.fulfil(loadingRequest)
            _ = self.lock.withLock { self.activeTasks.removeValue(forKey: key) }
        }
        lock.withLock { self.activeTasks[key] = task }
        return true
    }

    func resourceLoader(
        _ resourceLoader: AVAssetResourceLoader,
        didCancel loadingRequest: AVAssetResourceLoadingRequest
    ) {
        lock.withLock { activeTasks.removeValue(forKey: ObjectIdentifier(loadingRequest)) }?.cancel()
    }

    func cancelAllRequests() {
        let tasks = lock.withLock { () -> [Task<Void, Never>] in
            defer { activeTasks.removeAll() }
            return Array(activeTasks.values)
        }
        tasks.forEach { $0.cancel() }
    }

    static func resolveURL(_ customURL: URL, serverBaseURL: URL) -> URL? {
        let percentEncodedPath = URLComponents(url: customURL, resolvingAgainstBaseURL: false)?.percentEncodedPath ?? ""
        guard customURL.scheme?.lowercased() == customScheme,
              customURL.query == nil,
              customURL.fragment == nil,
              !percentEncodedPath.contains("%"),
              sameOrigin(customURL, serverBaseURL),
              isAllowedPath(customURL.path)
        else { return nil }
        var components = URLComponents(url: customURL, resolvingAgainstBaseURL: false)
        components?.scheme = serverBaseURL.scheme
        components?.host = serverBaseURL.host
        components?.port = serverBaseURL.port
        return components?.url
    }

    static func customPlaylistURL(for serverBaseURL: URL) -> URL? {
        var components = URLComponents(url: serverBaseURL, resolvingAgainstBaseURL: false)
        components?.scheme = customScheme
        components?.path = RelayWireContract.playlistPath
        return components?.url
    }

    private static func sameOrigin(_ customURL: URL, _ serverBaseURL: URL) -> Bool {
        let customHost = customURL.host?.lowercased()
        let serverHost = serverBaseURL.host?.lowercased()
        guard customHost == serverHost else { return false }
        return effectivePort(customURL) == effectivePort(serverBaseURL)
    }

    private static func effectivePort(_ url: URL) -> Int? {
        if let port = url.port { return port }
        switch url.scheme?.lowercased() {
        case "http", customScheme: return 80
        case "https": return 443
        default: return nil
        }
    }

    private static func isAllowedPath(_ path: String) -> Bool {
        if path == RelayWireContract.playlistPath || path == RelayWireContract.playlistSnapshotPath {
            return true
        }
        guard path.hasPrefix(RelayWireContract.mediaPathPrefix) else { return false }
        let identifier = String(path.dropFirst(RelayWireContract.mediaPathPrefix.count))
        let bytes = identifier.utf8
        guard (1 ... 256).contains(bytes.count),
              bytes.allSatisfy({ byte in
                  (byte >= 65 && byte <= 90)
                      || (byte >= 97 && byte <= 122)
                      || (byte >= 48 && byte <= 57)
                      || "-._/".utf8.contains(byte)
              })
        else { return false }
        return identifier.split(separator: "/", omittingEmptySubsequences: false).allSatisfy {
            !$0.isEmpty && $0 != "." && $0 != ".."
        }
    }

    private func fulfil(_ loadingRequest: AVAssetResourceLoadingRequest) async {
        guard let url = loadingRequest.request.url else {
            loadingRequest.finishLoading(with: URLError(.badURL))
            return
        }
        do {
            let (data, response) = try await resourceClient.load(url)
            guard !Task.isCancelled else { return }
            loadingRequest.dataRequest?.respond(with: data)
            loadingRequest.contentInformationRequest?.contentType = response.value(forHTTPHeaderField: "content-type")
            loadingRequest.contentInformationRequest?.contentLength = Int64(data.count)
            loadingRequest.contentInformationRequest?.isByteRangeAccessSupported = false
            loadingRequest.finishLoading()
        } catch {
            guard !Task.isCancelled else { return }
            loadingRequest.finishLoading(with: error)
        }
    }
}

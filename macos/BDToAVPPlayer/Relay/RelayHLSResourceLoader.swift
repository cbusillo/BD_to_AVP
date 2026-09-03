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

enum RelayResourceLoadingError: Error, Equatable, Sendable {
    case invalidDataRequest
    case requestedRangeNotSatisfiable
}

final class RelayActiveTaskRegistration: @unchecked Sendable {
    private let lock = NSLock()
    private var task: Task<Void, Never>?
    private var isCancelled = false

    func install(_ task: Task<Void, Never>) {
        let shouldCancel = lock.withLock { () -> Bool in
            self.task = task
            return isCancelled
        }
        if shouldCancel {
            task.cancel()
        }
    }

    func cancel() {
        let installedTask = lock.withLock { () -> Task<Void, Never>? in
            isCancelled = true
            return self.task
        }
        installedTask?.cancel()
    }
}

final class RelayActiveTaskRegistry: @unchecked Sendable {
    private let lock = NSLock()
    private var activeTasks: [ObjectIdentifier: RelayActiveTaskRegistration] = [:]

    func register(_ key: ObjectIdentifier) -> RelayActiveTaskRegistration {
        let registration = RelayActiveTaskRegistration()
        let replacedRegistration = lock.withLock {
            activeTasks.updateValue(registration, forKey: key)
        }
        replacedRegistration?.cancel()
        return registration
    }

    func install(
        _ task: Task<Void, Never>,
        for key: ObjectIdentifier,
        registration: RelayActiveTaskRegistration
    ) {
        let remainsActive = lock.withLock {
            activeTasks[key] === registration
        }
        if remainsActive {
            registration.install(task)
        } else {
            task.cancel()
        }
    }

    func complete(_ key: ObjectIdentifier, registration: RelayActiveTaskRegistration) {
        lock.withLock {
            guard activeTasks[key] === registration else { return }
            activeTasks.removeValue(forKey: key)
        }
    }

    func cancel(_ key: ObjectIdentifier) {
        let registration = lock.withLock {
            activeTasks.removeValue(forKey: key)
        }
        registration?.cancel()
    }

    func cancelAll() {
        let registrations = lock.withLock { () -> [RelayActiveTaskRegistration] in
            defer { activeTasks.removeAll() }
            return Array(activeTasks.values)
        }
        registrations.forEach { $0.cancel() }
    }

    var activeTaskCount: Int {
        lock.withLock { activeTasks.count }
    }
}

final class RelayHLSResourceLoader: NSObject, AVAssetResourceLoaderDelegate, @unchecked Sendable {
    static let customScheme = "bdtoavprelay"

    private let resourceClient: RelayAuthenticatedResourceClient
    private let activeTasks = RelayActiveTaskRegistry()

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
        let registration = activeTasks.register(key)
        let task = Task { [weak self, key, registration] in
            guard let self else { return }
            defer { self.activeTasks.complete(key, registration: registration) }
            await self.fulfil(loadingRequest)
        }
        activeTasks.install(task, for: key, registration: registration)
        return true
    }

    func resourceLoader(
        _ resourceLoader: AVAssetResourceLoader,
        didCancel loadingRequest: AVAssetResourceLoadingRequest
    ) {
        activeTasks.cancel(ObjectIdentifier(loadingRequest))
    }

    func cancelAllRequests() {
        activeTasks.cancelAll()
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

    static func requestedData(
        from data: Data,
        requestedOffset: Int64,
        currentOffset: Int64,
        requestedLength: Int,
        requestsAllDataToEndOfResource: Bool = false
    ) throws -> Data {
        guard let totalLength = Int64(exactly: data.count),
              requestedOffset >= 0,
              currentOffset >= requestedOffset,
              currentOffset <= totalLength,
              requestedLength >= 0,
              let requestedLength64 = Int64(exactly: requestedLength)
        else {
            throw RelayResourceLoadingError.invalidDataRequest
        }

        let consumedLength = currentOffset - requestedOffset
        guard requestsAllDataToEndOfResource || consumedLength <= requestedLength64 else {
            throw RelayResourceLoadingError.requestedRangeNotSatisfiable
        }
        let availableLength = totalLength - currentOffset
        let responseLength = requestsAllDataToEndOfResource
            ? availableLength
            : min(requestedLength64 - consumedLength, availableLength)
        guard responseLength >= 0,
              let lowerBound = Int(exactly: currentOffset),
              let upperBound = Int(exactly: currentOffset + responseLength)
        else {
            throw RelayResourceLoadingError.requestedRangeNotSatisfiable
        }
        return data.subdata(in: lowerBound ..< upperBound)
    }

    private func fulfil(_ loadingRequest: AVAssetResourceLoadingRequest) async {
        guard let url = loadingRequest.request.url else {
            loadingRequest.finishLoading(with: URLError(.badURL))
            return
        }
        do {
            let (data, response) = try await resourceClient.load(url)
            guard !Task.isCancelled else { return }
            guard let contentLength = Int64(exactly: data.count) else {
                throw RelayResourceLoadingError.invalidDataRequest
            }
            if let contentInformationRequest = loadingRequest.contentInformationRequest {
                contentInformationRequest.contentType = response.value(forHTTPHeaderField: "content-type")
                contentInformationRequest.contentLength = contentLength
                contentInformationRequest.isByteRangeAccessSupported = false
            }
            if let dataRequest = loadingRequest.dataRequest {
                let requestedData = try Self.requestedData(
                    from: data,
                    requestedOffset: dataRequest.requestedOffset,
                    currentOffset: dataRequest.currentOffset,
                    requestedLength: dataRequest.requestedLength,
                    requestsAllDataToEndOfResource: dataRequest.requestsAllDataToEndOfResource
                )
                guard !Task.isCancelled else { return }
                dataRequest.respond(with: requestedData)
            }
            loadingRequest.finishLoading()
        } catch {
            guard !Task.isCancelled else { return }
            loadingRequest.finishLoading(with: error)
        }
    }
}

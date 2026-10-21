import Foundation

protocol RelayTransport: Sendable {
    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

enum RelayTransportError: Error, Equatable {
    case unexpectedResponseType
    case invalidRelayURL
    case invalidResourceIdentifier
    case sessionExpired
    case unpaired
    case unexpectedStatusCode(Int)
    /// An unsigned, empty 401: the Mac no longer knows the parent session. Pair again.
    case sessionRevoked
    /// A signed 410 or 503 carrying `RelayLiveStreamEnded`: the live stream stopped or failed
    /// on the Mac while the session itself is still valid. Never a reason to pair again.
    case streamEnded(code: String, message: String?)
}

struct URLSessionRelayTransport: RelayTransport {
    private let session: URLSession

    init(configuration: URLSessionConfiguration = .ephemeral) {
        session = URLSession(configuration: configuration)
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await session.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw RelayTransportError.unexpectedResponseType
        }
        return (data, httpResponse)
    }
}

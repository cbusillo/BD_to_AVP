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

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

enum RelayWireContract {
    static let challengePath = "/relay/v1/challenge"
    static let pairingPath = "/relay/v1/pairing"
    static let playlistPath = "/relay/v1/playlist.m3u8"
    static let playlistSnapshotPath = "/relay/v1/playlist.json"
    static let mediaPathPrefix = "/relay/v1/media/"
    static let authenticationHeader = "x-bdtoavp-relay-auth"
    static let mediaCapabilityHeader = "x-bdtoavp-relay-media-capability"
    static let jsonContentType = "application/json"
}

struct RelayChallengeEnvelope: Decodable, Sendable {
    let challenge: RelaySessionChallenge
}

struct RelayPairingEnvelope: Decodable, Sendable {
    let acceptance: RelayPairingAcceptance
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

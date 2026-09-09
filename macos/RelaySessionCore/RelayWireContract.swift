import Foundation

enum RelayWireContract {
    static let protocolVersion = 3
    static let bonjourServiceType = "_bdtoavp-relay._tcp"

    static let challengePath = "/relay/v1/challenge"
    static let pairingPath = "/relay/v1/pairing"
    static let pairingConfirmPath = "/relay/v1/pairing/confirm"
    static let playlistPath = "/relay/v1/playlist.m3u8"
    static let playlistSnapshotPath = "/relay/v1/playlist.json"
    static let finishPath = "/relay/v1/control/finish"
    static let cancelPath = "/relay/v1/control/cancel"
    static let mediaPathPrefix = "/relay/v1/media/"

    static let authenticationHeader = "x-bdtoavp-relay-auth"
    static let responseAuthenticationHeader = "x-bdtoavp-relay-response-auth"
    static let mediaCapabilityHeader = "x-bdtoavp-relay-media-capability"
    static let jsonContentType = "application/json"
    static let maximumAuthenticationHeaderBytes = 1_024
}

struct RelayChallengeEnvelope: Codable, Sendable {
    let challenge: RelaySessionChallenge
}

struct RelayPairingCandidateEnvelope: Codable, Sendable {
    let candidate: RelayPairingCandidate
}

struct RelayPairingConfirmationEnvelope: Codable, Sendable {
    let confirmation: RelayPairingConfirmationResponse
}

struct RelayPlaylistSnapshot: Codable, Equatable, Sendable {
    let earliestPlayableTimeMilliseconds: Int64
    let totalDurationMilliseconds: Int64
    let isFinalized: Bool
    let segments: [RelayPlaylistSegment]

    init(
        earliestPlayableTimeMilliseconds: Int64,
        totalDurationMilliseconds: Int64,
        isFinalized: Bool,
        segments: [RelayPlaylistSegment]
    ) throws {
        guard (0 ... RelayPlaylistLimits.maximumTimelineDurationMilliseconds).contains(
            earliestPlayableTimeMilliseconds
        ), (earliestPlayableTimeMilliseconds ... RelayPlaylistLimits.maximumTimelineDurationMilliseconds).contains(
            totalDurationMilliseconds
        ), segments.count <= RelayPlaylistLimits.maximumRetainedSegmentLimit
        else {
            throw RelayPlaylistError.invalidSegment
        }

        if let first = segments.first, let last = segments.last {
            guard first.startTimeMilliseconds == earliestPlayableTimeMilliseconds,
                  last.endTimeMilliseconds == totalDurationMilliseconds,
                  zip(segments, segments.dropFirst()).allSatisfy({ current, next in
                      let expectedSequence = current.sequenceNumber.addingReportingOverflow(1)
                      return !expectedSequence.overflow
                          && expectedSequence.partialValue == next.sequenceNumber
                          && current.endTimeMilliseconds == next.startTimeMilliseconds
                  })
            else {
                throw RelayPlaylistError.invalidSegment
            }
        } else if earliestPlayableTimeMilliseconds != totalDurationMilliseconds {
            throw RelayPlaylistError.invalidSegment
        }

        self.earliestPlayableTimeMilliseconds = earliestPlayableTimeMilliseconds
        self.totalDurationMilliseconds = totalDurationMilliseconds
        self.isFinalized = isFinalized
        self.segments = segments
    }

    private enum CodingKeys: String, CodingKey {
        case earliestPlayableTimeMilliseconds
        case totalDurationMilliseconds
        case isFinalized
        case segments
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        var decodedSegments: [RelayPlaylistSegment] = []
        var segmentsContainer = try container.nestedUnkeyedContainer(forKey: .segments)
        while !segmentsContainer.isAtEnd {
            guard decodedSegments.count < RelayPlaylistLimits.maximumRetainedSegmentLimit else {
                throw RelayPlaylistError.invalidRetentionLimit
            }
            decodedSegments.append(try segmentsContainer.decode(RelayPlaylistSegment.self))
        }
        try self.init(
            earliestPlayableTimeMilliseconds: container.decode(
                Int64.self,
                forKey: .earliestPlayableTimeMilliseconds
            ),
            totalDurationMilliseconds: container.decode(Int64.self, forKey: .totalDurationMilliseconds),
            isFinalized: container.decode(Bool.self, forKey: .isFinalized),
            segments: decodedSegments
        )
    }
}

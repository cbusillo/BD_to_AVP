import Foundation

public enum RelayPlaylistError: Error, Equatable, Sendable {
    case invalidTargetDuration
    case invalidRetentionLimit
    case invalidSegment
    case segmentExceedsTargetDuration
    case finalized
}

public enum RelayPlaylistLimits {
    public static let maximumTargetDurationMilliseconds: Int64 = 24 * 60 * 60 * 1_000
    public static let maximumTimelineDurationMilliseconds: Int64 = 365 * 24 * 60 * 60 * 1_000
    public static let maximumRetainedSegmentLimit = 100_000
    public static let maximumResourceIdentifierLength = 512
}

public struct RelayPlaylistSegment: Codable, Equatable, Sendable {
    public let sequenceNumber: UInt64
    public let startTimeMilliseconds: Int64
    public let durationMilliseconds: Int64
    public let resourceIdentifier: String

    public init(
        sequenceNumber: UInt64,
        startTimeMilliseconds: Int64,
        durationMilliseconds: Int64,
        resourceIdentifier: String
    ) throws {
        guard (0 ... RelayPlaylistLimits.maximumTimelineDurationMilliseconds).contains(startTimeMilliseconds),
              (1 ... RelayPlaylistLimits.maximumTargetDurationMilliseconds).contains(durationMilliseconds),
              RelayEventPlaylist.isValidResourceIdentifier(resourceIdentifier)
        else {
            throw RelayPlaylistError.invalidSegment
        }
        let end = startTimeMilliseconds.addingReportingOverflow(durationMilliseconds)
        guard !end.overflow, end.partialValue <= RelayPlaylistLimits.maximumTimelineDurationMilliseconds else {
            throw RelayPlaylistError.invalidSegment
        }

        self.sequenceNumber = sequenceNumber
        self.startTimeMilliseconds = startTimeMilliseconds
        self.durationMilliseconds = durationMilliseconds
        self.resourceIdentifier = resourceIdentifier
    }

    public var startTime: TimeInterval {
        TimeInterval(startTimeMilliseconds) / 1_000
    }

    public var duration: TimeInterval {
        TimeInterval(durationMilliseconds) / 1_000
    }

    public var endTimeMilliseconds: Int64 {
        startTimeMilliseconds.addingReportingOverflow(durationMilliseconds).partialValue
    }

    private enum CodingKeys: String, CodingKey {
        case sequenceNumber
        case startTimeMilliseconds
        case durationMilliseconds
        case resourceIdentifier
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(
            sequenceNumber: container.decode(UInt64.self, forKey: .sequenceNumber),
            startTimeMilliseconds: container.decode(Int64.self, forKey: .startTimeMilliseconds),
            durationMilliseconds: container.decode(Int64.self, forKey: .durationMilliseconds),
            resourceIdentifier: container.decode(String.self, forKey: .resourceIdentifier)
        )
    }
}

public enum RelayPlaylistSeekValidation: Equatable, Sendable {
    case playable(segment: RelayPlaylistSegment, offsetMilliseconds: Int64)
    case beforeRetainedHistory(earliestPlayableMilliseconds: Int64)
    case notYetAvailable(latestAvailableMilliseconds: Int64)
    case ended(finalDurationMilliseconds: Int64)
    case invalidTime
}

public struct RelayEventPlaylist: Codable, Equatable, Sendable {
    public let targetDurationMilliseconds: Int64
    public let retainedSegmentLimit: Int
    public private(set) var segments: [RelayPlaylistSegment]
    public private(set) var nextSequenceNumber: UInt64
    public private(set) var nextStartTimeMilliseconds: Int64
    public private(set) var isFinalized: Bool

    public init(targetDuration: TimeInterval, retainedSegmentLimit: Int) throws {
        guard let targetDurationMilliseconds = RelayTime.milliseconds(
            from: targetDuration,
            maximum: RelayPlaylistLimits.maximumTargetDurationMilliseconds
        ), targetDurationMilliseconds > 0
        else {
            throw RelayPlaylistError.invalidTargetDuration
        }
        guard (1 ... RelayPlaylistLimits.maximumRetainedSegmentLimit).contains(retainedSegmentLimit) else {
            throw RelayPlaylistError.invalidRetentionLimit
        }

        self.targetDurationMilliseconds = targetDurationMilliseconds
        self.retainedSegmentLimit = retainedSegmentLimit
        segments = []
        nextSequenceNumber = 0
        nextStartTimeMilliseconds = 0
        isFinalized = false
    }

    public var targetDuration: TimeInterval {
        TimeInterval(targetDurationMilliseconds) / 1_000
    }

    public var earliestPlayableTimeMilliseconds: Int64 {
        segments.first?.startTimeMilliseconds ?? nextStartTimeMilliseconds
    }

    public var earliestPlayableTime: TimeInterval {
        TimeInterval(earliestPlayableTimeMilliseconds) / 1_000
    }

    public var totalDuration: TimeInterval {
        TimeInterval(nextStartTimeMilliseconds) / 1_000
    }

    public var hasEndList: Bool {
        isFinalized
    }

    @discardableResult
    public mutating func append(resourceIdentifier: String, duration: TimeInterval) throws -> RelayPlaylistSegment {
        guard !isFinalized else {
            throw RelayPlaylistError.finalized
        }
        guard let durationMilliseconds = RelayTime.milliseconds(
            from: duration,
            maximum: RelayPlaylistLimits.maximumTargetDurationMilliseconds
        ), durationMilliseconds > 0,
            RelayEventPlaylist.isValidResourceIdentifier(resourceIdentifier)
        else {
            throw RelayPlaylistError.invalidSegment
        }
        guard durationMilliseconds <= targetDurationMilliseconds else {
            throw RelayPlaylistError.segmentExceedsTargetDuration
        }

        let nextSequence = nextSequenceNumber.addingReportingOverflow(1)
        let nextStart = nextStartTimeMilliseconds.addingReportingOverflow(durationMilliseconds)
        guard !nextSequence.overflow,
              !nextStart.overflow,
              nextStart.partialValue <= RelayPlaylistLimits.maximumTimelineDurationMilliseconds
        else {
            throw RelayPlaylistError.invalidSegment
        }

        let segment = try RelayPlaylistSegment(
            sequenceNumber: nextSequenceNumber,
            startTimeMilliseconds: nextStartTimeMilliseconds,
            durationMilliseconds: durationMilliseconds,
            resourceIdentifier: resourceIdentifier
        )
        segments.append(segment)
        nextSequenceNumber = nextSequence.partialValue
        nextStartTimeMilliseconds = nextStart.partialValue
        if segments.count > retainedSegmentLimit {
            segments.removeFirst(segments.count - retainedSegmentLimit)
        }
        return segment
    }

    public mutating func finalize() {
        isFinalized = true
    }

    public func validateSeek(to time: TimeInterval) -> RelayPlaylistSeekValidation {
        guard time.isFinite, time >= 0 else {
            return .invalidTime
        }
        guard let requestedMilliseconds = RelayTime.milliseconds(
            from: time,
            maximum: RelayPlaylistLimits.maximumTimelineDurationMilliseconds
        ) else {
            return .notYetAvailable(latestAvailableMilliseconds: nextStartTimeMilliseconds)
        }
        if requestedMilliseconds < earliestPlayableTimeMilliseconds {
            return .beforeRetainedHistory(earliestPlayableMilliseconds: earliestPlayableTimeMilliseconds)
        }
        if requestedMilliseconds >= nextStartTimeMilliseconds {
            if isFinalized {
                return .ended(finalDurationMilliseconds: nextStartTimeMilliseconds)
            }
            return .notYetAvailable(latestAvailableMilliseconds: nextStartTimeMilliseconds)
        }
        guard let segment = segments.first(where: {
            requestedMilliseconds >= $0.startTimeMilliseconds && requestedMilliseconds < $0.endTimeMilliseconds
        }) else {
            return .beforeRetainedHistory(earliestPlayableMilliseconds: earliestPlayableTimeMilliseconds)
        }
        let offset = requestedMilliseconds.subtractingReportingOverflow(segment.startTimeMilliseconds)
        guard !offset.overflow else {
            return .beforeRetainedHistory(earliestPlayableMilliseconds: earliestPlayableTimeMilliseconds)
        }
        return .playable(segment: segment, offsetMilliseconds: offset.partialValue)
    }

    private enum CodingKeys: String, CodingKey {
        case targetDurationMilliseconds
        case retainedSegmentLimit
        case segments
        case nextSequenceNumber
        case nextStartTimeMilliseconds
        case isFinalized
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let targetDurationMilliseconds = try container.decode(Int64.self, forKey: .targetDurationMilliseconds)
        let retainedSegmentLimit = try container.decode(Int.self, forKey: .retainedSegmentLimit)

        guard (1 ... RelayPlaylistLimits.maximumTargetDurationMilliseconds).contains(targetDurationMilliseconds) else {
            throw RelayPlaylistError.invalidTargetDuration
        }
        guard (1 ... RelayPlaylistLimits.maximumRetainedSegmentLimit).contains(retainedSegmentLimit) else {
            throw RelayPlaylistError.invalidRetentionLimit
        }

        var decodedSegments: [RelayPlaylistSegment] = []
        decodedSegments.reserveCapacity(min(retainedSegmentLimit, 1_024))
        var segmentsContainer = try container.nestedUnkeyedContainer(forKey: .segments)
        while !segmentsContainer.isAtEnd {
            guard decodedSegments.count < retainedSegmentLimit else {
                throw RelayPlaylistError.invalidRetentionLimit
            }
            decodedSegments.append(try segmentsContainer.decode(RelayPlaylistSegment.self))
        }

        let nextSequenceNumber = try container.decode(UInt64.self, forKey: .nextSequenceNumber)
        let nextStartTimeMilliseconds = try container.decode(Int64.self, forKey: .nextStartTimeMilliseconds)
        let isFinalized = try container.decode(Bool.self, forKey: .isFinalized)

        guard (0 ... RelayPlaylistLimits.maximumTimelineDurationMilliseconds).contains(nextStartTimeMilliseconds),
              decodedSegments.allSatisfy({ $0.durationMilliseconds <= targetDurationMilliseconds }),
              RelayEventPlaylist.areDurableSegments(
                  decodedSegments,
                  endingAt: nextStartTimeMilliseconds,
                  nextSequenceNumber: nextSequenceNumber
              )
        else {
            throw RelayPlaylistError.invalidSegment
        }

        self.targetDurationMilliseconds = targetDurationMilliseconds
        self.retainedSegmentLimit = retainedSegmentLimit
        segments = decodedSegments
        self.nextSequenceNumber = nextSequenceNumber
        self.nextStartTimeMilliseconds = nextStartTimeMilliseconds
        self.isFinalized = isFinalized
    }

    fileprivate static func isValidResourceIdentifier(_ value: String) -> Bool {
        let bytes = value.utf8
        guard (1 ... RelayPlaylistLimits.maximumResourceIdentifierLength).contains(bytes.count),
              bytes.first != Character("/").asciiValue,
              bytes.allSatisfy({ byte in
                  (byte >= Character("A").asciiValue! && byte <= Character("Z").asciiValue!)
                      || (byte >= Character("a").asciiValue! && byte <= Character("z").asciiValue!)
                      || (byte >= Character("0").asciiValue! && byte <= Character("9").asciiValue!)
                      || "-._~/".utf8.contains(byte)
              })
        else {
            return false
        }
        return value.split(separator: "/", omittingEmptySubsequences: false).allSatisfy { component in
            !component.isEmpty && component != "." && component != ".."
        }
    }

    private static func areDurableSegments(
        _ segments: [RelayPlaylistSegment],
        endingAt endTimeMilliseconds: Int64,
        nextSequenceNumber: UInt64
    ) -> Bool {
        guard let lastSegment = segments.last else {
            return endTimeMilliseconds == 0 && nextSequenceNumber == 0
        }

        let expectedNextSequence = lastSegment.sequenceNumber.addingReportingOverflow(1)
        guard !expectedNextSequence.overflow,
              lastSegment.endTimeMilliseconds == endTimeMilliseconds,
              nextSequenceNumber == expectedNextSequence.partialValue
        else {
            return false
        }

        return zip(segments, segments.dropFirst()).allSatisfy { current, next in
            let sequence = current.sequenceNumber.addingReportingOverflow(1)
            return !sequence.overflow
                && sequence.partialValue == next.sequenceNumber
                && current.endTimeMilliseconds == next.startTimeMilliseconds
        }
    }
}

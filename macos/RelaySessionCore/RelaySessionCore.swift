import CryptoKit
import Foundation

public enum RelaySessionError: Error, Equatable, Sendable {
    case invalidSessionIdentifier
    case invalidPublicKey
    case invalidNonce
    case invalidNonceCommitment
    case invalidCandidateIdentifier
    case invalidProof
    case invalidTimestamp
    case invalidRequest
    case expiredChallenge
    case confirmationExpired
    case nonceCommitmentMismatch
    case candidateProofMismatch
    case clientConfirmationMismatch
    case serverConfirmationMismatch
    case pairingAlreadyCompleted
    case pairingCandidateInProgress
    case pairingCandidateNotFound
    case pairingAttemptsExhausted
    case requestBodyMismatch
    case requestSignatureMismatch
    case requestExpired
    case requestTimestampTooFarInFuture
    case replayDetected
    case replayCapacityExceeded
    case invalidResponse
    case responseBodyMismatch
    case responseSignatureMismatch
    case invalidValidationPolicy
}

public enum RelaySessionRole: String, Codable, Equatable, Sendable {
    case client
    case server

    fileprivate var peer: RelaySessionRole {
        switch self {
        case .client:
            .server
        case .server:
            .client
        }
    }
}

public struct RelaySessionIdentifier: Codable, Equatable, Hashable, Sendable {
    public let rawValue: String

    public init(rawValue: String) throws {
        guard rawValue.utf8.count == 36, let value = UUID(uuidString: rawValue) else {
            throw RelaySessionError.invalidSessionIdentifier
        }
        self.rawValue = value.uuidString.lowercased()
    }

    public static func random() -> RelaySessionIdentifier {
        RelaySessionIdentifier(canonicalUUID: UUID())
    }

    private init(canonicalUUID: UUID) {
        rawValue = canonicalUUID.uuidString.lowercased()
    }

    private enum CodingKeys: String, CodingKey {
        case rawValue
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(rawValue: container.decode(String.self, forKey: .rawValue))
    }
}

public struct RelaySessionChallenge: Codable, Equatable, Sendable {
    public let sessionID: RelaySessionIdentifier
    public let serverPublicKey: Data
    public let serverNonceCommitment: Data
    public let expiresAtUnixMilliseconds: Int64

    public init(
        sessionID: RelaySessionIdentifier,
        serverPublicKey: Data,
        serverNonceCommitment: Data,
        expiresAtUnixMilliseconds: Int64
    ) throws {
        guard serverPublicKey.count == RelayCrypto.curve25519KeyLength else {
            throw RelaySessionError.invalidPublicKey
        }
        guard serverNonceCommitment.count == RelayCrypto.sha256Length else {
            throw RelaySessionError.invalidNonceCommitment
        }
        guard RelayTime.isValidUnixMilliseconds(expiresAtUnixMilliseconds) else {
            throw RelaySessionError.invalidTimestamp
        }

        self.sessionID = sessionID
        self.serverPublicKey = serverPublicKey
        self.serverNonceCommitment = serverNonceCommitment
        self.expiresAtUnixMilliseconds = expiresAtUnixMilliseconds
    }

    public var expirationDate: Date {
        RelayTime.date(fromUnixMilliseconds: expiresAtUnixMilliseconds)
    }

    private enum CodingKeys: String, CodingKey {
        case sessionID
        case serverPublicKey
        case serverNonceCommitment
        case expiresAtUnixMilliseconds
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(
            sessionID: container.decode(RelaySessionIdentifier.self, forKey: .sessionID),
            serverPublicKey: container.decode(Data.self, forKey: .serverPublicKey),
            serverNonceCommitment: container.decode(Data.self, forKey: .serverNonceCommitment),
            expiresAtUnixMilliseconds: container.decode(Int64.self, forKey: .expiresAtUnixMilliseconds)
        )
    }
}

public struct RelayPairingRequest: Codable, Equatable, Sendable {
    public let sessionID: RelaySessionIdentifier
    public let serverNonceCommitment: Data
    public let clientPublicKey: Data
    public let clientNonce: Data

    public init(
        sessionID: RelaySessionIdentifier,
        serverNonceCommitment: Data,
        clientPublicKey: Data,
        clientNonce: Data
    ) throws {
        guard serverNonceCommitment.count == RelayCrypto.sha256Length else {
            throw RelaySessionError.invalidNonceCommitment
        }
        guard clientPublicKey.count == RelayCrypto.curve25519KeyLength else {
            throw RelaySessionError.invalidPublicKey
        }
        guard clientNonce.count == RelayCrypto.nonceLength else {
            throw RelaySessionError.invalidNonce
        }
        self.sessionID = sessionID
        self.serverNonceCommitment = serverNonceCommitment
        self.clientPublicKey = clientPublicKey
        self.clientNonce = clientNonce
    }

    private enum CodingKeys: String, CodingKey {
        case sessionID
        case serverNonceCommitment
        case clientPublicKey
        case clientNonce
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(
            sessionID: container.decode(RelaySessionIdentifier.self, forKey: .sessionID),
            serverNonceCommitment: container.decode(Data.self, forKey: .serverNonceCommitment),
            clientPublicKey: container.decode(Data.self, forKey: .clientPublicKey),
            clientNonce: container.decode(Data.self, forKey: .clientNonce)
        )
    }
}

public struct RelayPairingCandidateIdentifier: Codable, Equatable, Hashable, Sendable {
    public let rawValue: String

    public init(rawValue: String) throws {
        guard let value = UUID(uuidString: rawValue) else {
            throw RelaySessionError.invalidCandidateIdentifier
        }
        self.rawValue = value.uuidString.lowercased()
    }

    public static func random() -> RelayPairingCandidateIdentifier {
        try! RelayPairingCandidateIdentifier(rawValue: UUID().uuidString)
    }

    private enum CodingKeys: String, CodingKey {
        case rawValue
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(rawValue: container.decode(String.self, forKey: .rawValue))
    }
}

public struct RelayShortAuthenticationString: Equatable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let digits: String

    fileprivate init(digits: String) {
        self.digits = digits
    }

    public var formattedDigits: String {
        "\(digits.prefix(3)) \(digits.suffix(3))"
    }

    public var accessibilityDigits: String {
        digits.map(String.init).joined(separator: " ")
    }

    public static func == (lhs: RelayShortAuthenticationString, rhs: RelayShortAuthenticationString) -> Bool {
        RelayCrypto.constantTimeEqual(Data(lhs.digits.utf8), Data(rhs.digits.utf8))
    }

    public var description: String { "<relay numeric comparison: redacted>" }
    public var debugDescription: String { description }
}

public struct RelayPairingCandidate: Codable, Equatable, Sendable {
    public let candidateID: RelayPairingCandidateIdentifier
    public let sessionID: RelaySessionIdentifier
    public let serverNonce: Data
    public let expiresAtUnixMilliseconds: Int64
    public let serverProof: Data

    public init(
        candidateID: RelayPairingCandidateIdentifier,
        sessionID: RelaySessionIdentifier,
        serverNonce: Data,
        expiresAtUnixMilliseconds: Int64,
        serverProof: Data
    ) throws {
        guard serverNonce.count == RelayCrypto.nonceLength else { throw RelaySessionError.invalidNonce }
        guard RelayTime.isValidUnixMilliseconds(expiresAtUnixMilliseconds) else { throw RelaySessionError.invalidTimestamp }
        guard serverProof.count == RelayCrypto.sha256Length else { throw RelaySessionError.invalidProof }
        self.candidateID = candidateID
        self.sessionID = sessionID
        self.serverNonce = serverNonce
        self.expiresAtUnixMilliseconds = expiresAtUnixMilliseconds
        self.serverProof = serverProof
    }

    public var expirationDate: Date { RelayTime.date(fromUnixMilliseconds: expiresAtUnixMilliseconds) }

    private enum CodingKeys: String, CodingKey {
        case candidateID
        case sessionID
        case serverNonce
        case expiresAtUnixMilliseconds
        case serverProof
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(
            candidateID: container.decode(RelayPairingCandidateIdentifier.self, forKey: .candidateID),
            sessionID: container.decode(RelaySessionIdentifier.self, forKey: .sessionID),
            serverNonce: container.decode(Data.self, forKey: .serverNonce),
            expiresAtUnixMilliseconds: container.decode(Int64.self, forKey: .expiresAtUnixMilliseconds),
            serverProof: container.decode(Data.self, forKey: .serverProof)
        )
    }
}

public enum RelayPairingConfirmationDecision: String, Codable, Equatable, Sendable {
    case codesMatch
    case notMyMac
}

public struct RelayPairingConfirmation: Codable, Equatable, Sendable {
    public let candidateID: RelayPairingCandidateIdentifier
    public let decision: RelayPairingConfirmationDecision
    public let clientConfirmationMAC: Data?

    public init(
        candidateID: RelayPairingCandidateIdentifier,
        decision: RelayPairingConfirmationDecision,
        clientConfirmationMAC: Data?
    ) throws {
        switch decision {
        case .codesMatch:
            guard clientConfirmationMAC?.count == RelayCrypto.sha256Length else { throw RelaySessionError.invalidProof }
        case .notMyMac:
            guard clientConfirmationMAC == nil else { throw RelaySessionError.invalidProof }
        }
        self.candidateID = candidateID
        self.decision = decision
        self.clientConfirmationMAC = clientConfirmationMAC
    }

    private enum CodingKeys: String, CodingKey {
        case candidateID
        case decision
        case clientConfirmationMAC
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(
            candidateID: container.decode(RelayPairingCandidateIdentifier.self, forKey: .candidateID),
            decision: container.decode(RelayPairingConfirmationDecision.self, forKey: .decision),
            clientConfirmationMAC: container.decodeIfPresent(Data.self, forKey: .clientConfirmationMAC)
        )
    }
}

public enum RelayPairingConfirmationState: String, Codable, Equatable, Sendable {
    case waitingForMac
    case established
    case rejected
}

public struct RelayPairingAcceptance: Codable, Equatable, Sendable {
    public let candidateID: RelayPairingCandidateIdentifier
    public let sessionID: RelaySessionIdentifier
    public let expiresAtUnixMilliseconds: Int64
    public let serverConfirmationMAC: Data

    public init(
        candidateID: RelayPairingCandidateIdentifier,
        sessionID: RelaySessionIdentifier,
        expiresAtUnixMilliseconds: Int64,
        serverConfirmationMAC: Data
    ) throws {
        guard RelayTime.isValidUnixMilliseconds(expiresAtUnixMilliseconds) else {
            throw RelaySessionError.invalidTimestamp
        }
        guard serverConfirmationMAC.count == RelayCrypto.sha256Length else {
            throw RelaySessionError.invalidProof
        }

        self.candidateID = candidateID
        self.sessionID = sessionID
        self.expiresAtUnixMilliseconds = expiresAtUnixMilliseconds
        self.serverConfirmationMAC = serverConfirmationMAC
    }

    public var expirationDate: Date {
        RelayTime.date(fromUnixMilliseconds: expiresAtUnixMilliseconds)
    }

    private enum CodingKeys: String, CodingKey {
        case candidateID
        case sessionID
        case expiresAtUnixMilliseconds
        case serverConfirmationMAC
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(
            candidateID: container.decode(RelayPairingCandidateIdentifier.self, forKey: .candidateID),
            sessionID: container.decode(RelaySessionIdentifier.self, forKey: .sessionID),
            expiresAtUnixMilliseconds: container.decode(Int64.self, forKey: .expiresAtUnixMilliseconds),
            serverConfirmationMAC: container.decode(Data.self, forKey: .serverConfirmationMAC)
        )
    }
}

public struct RelayPairingConfirmationResponse: Codable, Equatable, Sendable {
    public let candidateID: RelayPairingCandidateIdentifier
    public let state: RelayPairingConfirmationState
    public let acceptance: RelayPairingAcceptance?

    public init(candidateID: RelayPairingCandidateIdentifier, state: RelayPairingConfirmationState, acceptance: RelayPairingAcceptance? = nil) throws {
        guard (state == .established) == (acceptance != nil) else { throw RelaySessionError.invalidRequest }
        self.candidateID = candidateID
        self.state = state
        self.acceptance = acceptance
    }

    private enum CodingKeys: String, CodingKey {
        case candidateID
        case state
        case acceptance
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(
            candidateID: container.decode(RelayPairingCandidateIdentifier.self, forKey: .candidateID),
            state: container.decode(RelayPairingConfirmationState.self, forKey: .state),
            acceptance: container.decodeIfPresent(RelayPairingAcceptance.self, forKey: .acceptance)
        )
    }
}

public struct RelaySessionIdentity: Equatable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let value: String

    fileprivate init(value: String) {
        self.value = value
    }

    public static func == (lhs: RelaySessionIdentity, rhs: RelaySessionIdentity) -> Bool {
        RelayCrypto.constantTimeEqual(Data(lhs.value.utf8), Data(rhs.value.utf8))
    }

    public var description: String {
        "<relay session identity: redacted>"
    }

    public var debugDescription: String {
        description
    }
}

public struct RelayMediaCapability: Equatable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let value: String

    fileprivate init(value: String) {
        self.value = value
    }

    public static func == (lhs: RelayMediaCapability, rhs: RelayMediaCapability) -> Bool {
        RelayCrypto.constantTimeEqual(Data(lhs.value.utf8), Data(rhs.value.utf8))
    }

    public func matches(_ candidate: String) -> Bool {
        RelayCrypto.constantTimeEqual(Data(value.utf8), Data(candidate.utf8))
    }

    public var description: String {
        "<relay media capability: redacted>"
    }

    public var debugDescription: String {
        description
    }
}

public struct RelayAuthenticatedRequest: Codable, Equatable, Sendable {
    public let sessionID: RelaySessionIdentifier
    public let signerRole: RelaySessionRole
    public let method: String
    public let requestTarget: String
    public let timestampUnixMilliseconds: Int64
    public let nonce: String
    public let bodySHA256: Data
    public let signature: Data

    public init(
        sessionID: RelaySessionIdentifier,
        signerRole: RelaySessionRole,
        method: String,
        requestTarget: String,
        timestampUnixMilliseconds: Int64,
        nonce: String,
        bodySHA256: Data,
        signature: Data
    ) throws {
        guard RelayCanonical.isValidMethod(method), RelayCanonical.isValidRequestTarget(requestTarget) else {
            throw RelaySessionError.invalidRequest
        }
        guard RelayTime.isValidUnixMilliseconds(timestampUnixMilliseconds) else {
            throw RelaySessionError.invalidTimestamp
        }
        guard RelayCanonical.isValidNonce(nonce) else {
            throw RelaySessionError.invalidNonce
        }
        guard bodySHA256.count == RelayCrypto.sha256Length, signature.count == RelayCrypto.sha256Length else {
            throw RelaySessionError.invalidProof
        }

        self.sessionID = sessionID
        self.signerRole = signerRole
        self.method = method
        self.requestTarget = requestTarget
        self.timestampUnixMilliseconds = timestampUnixMilliseconds
        self.nonce = nonce
        self.bodySHA256 = bodySHA256
        self.signature = signature
    }

    private enum CodingKeys: String, CodingKey {
        case sessionID
        case signerRole
        case method
        case requestTarget
        case timestampUnixMilliseconds
        case nonce
        case bodySHA256
        case signature
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(
            sessionID: container.decode(RelaySessionIdentifier.self, forKey: .sessionID),
            signerRole: container.decode(RelaySessionRole.self, forKey: .signerRole),
            method: container.decode(String.self, forKey: .method),
            requestTarget: container.decode(String.self, forKey: .requestTarget),
            timestampUnixMilliseconds: container.decode(Int64.self, forKey: .timestampUnixMilliseconds),
            nonce: container.decode(String.self, forKey: .nonce),
            bodySHA256: container.decode(Data.self, forKey: .bodySHA256),
            signature: container.decode(Data.self, forKey: .signature)
        )
    }
}

public struct RelayAuthenticatedResponse: Codable, Equatable, Sendable {
    public let sessionID: RelaySessionIdentifier
    public let signerRole: RelaySessionRole
    public let requestNonce: String
    public let statusCode: Int
    public let bodySHA256: Data
    public let signature: Data

    public init(
        sessionID: RelaySessionIdentifier,
        signerRole: RelaySessionRole,
        requestNonce: String,
        statusCode: Int,
        bodySHA256: Data,
        signature: Data
    ) throws {
        guard RelayCanonical.isValidNonce(requestNonce), RelayCanonical.isValidHTTPStatusCode(statusCode) else {
            throw RelaySessionError.invalidResponse
        }
        guard bodySHA256.count == RelayCrypto.sha256Length, signature.count == RelayCrypto.sha256Length else {
            throw RelaySessionError.invalidProof
        }

        self.sessionID = sessionID
        self.signerRole = signerRole
        self.requestNonce = requestNonce
        self.statusCode = statusCode
        self.bodySHA256 = bodySHA256
        self.signature = signature
    }

    private enum CodingKeys: String, CodingKey {
        case sessionID
        case signerRole
        case requestNonce
        case statusCode
        case bodySHA256
        case signature
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(
            sessionID: container.decode(RelaySessionIdentifier.self, forKey: .sessionID),
            signerRole: container.decode(RelaySessionRole.self, forKey: .signerRole),
            requestNonce: container.decode(String.self, forKey: .requestNonce),
            statusCode: container.decode(Int.self, forKey: .statusCode),
            bodySHA256: container.decode(Data.self, forKey: .bodySHA256),
            signature: container.decode(Data.self, forKey: .signature)
        )
    }
}

public struct RelayRequestValidationPolicy: Sendable, Equatable {
    public let maximumAgeMilliseconds: Int64
    public let allowedFutureSkewMilliseconds: Int64

    public init(maximumAge: TimeInterval = 30, allowedFutureSkew: TimeInterval = 5) throws {
        guard let maximumAgeMilliseconds = RelayTime.milliseconds(
            from: maximumAge,
            maximum: RelayLimits.maximumRequestAgeMilliseconds
        ), let allowedFutureSkewMilliseconds = RelayTime.milliseconds(
            from: allowedFutureSkew,
            maximum: RelayLimits.maximumFutureSkewMilliseconds
        ), maximumAgeMilliseconds > 0
        else {
            throw RelaySessionError.invalidValidationPolicy
        }

        self.maximumAgeMilliseconds = maximumAgeMilliseconds
        self.allowedFutureSkewMilliseconds = allowedFutureSkewMilliseconds
    }
}

public actor RelayReplayNonceStore {
    private struct Key: Hashable, Sendable {
        let sessionID: RelaySessionIdentifier
        let nonce: String
    }

    private let capacity: Int
    private var entries: [Key: Int64] = [:]

    public init(capacity: Int = 1_024) throws {
        guard (1 ... RelayLimits.maximumReplayCapacity).contains(capacity)
        else {
            throw RelaySessionError.invalidValidationPolicy
        }

        self.capacity = capacity
    }

    public func checkAndInsert(
        sessionID: RelaySessionIdentifier,
        nonce: String,
        validThroughUnixMilliseconds: Int64,
        now: Date
    ) throws {
        guard RelayCanonical.isValidNonce(nonce) else {
            throw RelaySessionError.invalidNonce
        }
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now) else {
            throw RelaySessionError.invalidTimestamp
        }
        guard RelayTime.isValidUnixMilliseconds(validThroughUnixMilliseconds),
              validThroughUnixMilliseconds >= nowMilliseconds
        else {
            throw RelaySessionError.invalidTimestamp
        }
        evictExpired(nowUnixMilliseconds: nowMilliseconds)

        let key = Key(sessionID: sessionID, nonce: nonce)
        guard entries[key] == nil else {
            throw RelaySessionError.replayDetected
        }
        guard entries.count < capacity else {
            throw RelaySessionError.replayCapacityExceeded
        }
        entries[key] = validThroughUnixMilliseconds
    }

    public func contains(sessionID: RelaySessionIdentifier, nonce: String, now: Date) throws -> Bool {
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now) else {
            throw RelaySessionError.invalidTimestamp
        }
        evictExpired(nowUnixMilliseconds: nowMilliseconds)
        return entries[Key(sessionID: sessionID, nonce: nonce)] != nil
    }

    public func count(now: Date) throws -> Int {
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now) else {
            throw RelaySessionError.invalidTimestamp
        }
        evictExpired(nowUnixMilliseconds: nowMilliseconds)
        return entries.count
    }

    private func evictExpired(nowUnixMilliseconds: Int64) {
        entries = entries.filter { $0.value >= nowUnixMilliseconds }
    }
}

public struct RelayServerPairingResult: Sendable {
    public let candidate: RelayPairingCandidate
    let provisionalSession: RelayEstablishedSession
}

public struct RelayPendingPairingCandidate: Equatable, Sendable {
    public let candidateID: RelayPairingCandidateIdentifier
    public let shortAuthenticationString: RelayShortAuthenticationString
    public let expiresAtUnixMilliseconds: Int64
    public let isMacApproved: Bool

    public var expirationDate: Date { RelayTime.date(fromUnixMilliseconds: expiresAtUnixMilliseconds) }
}

public struct RelayServerConfirmationResult: Sendable {
    public let response: RelayPairingConfirmationResponse
    let session: RelayEstablishedSession?
}

public actor RelayServerPairingContext {
    public nonisolated let sessionID: RelaySessionIdentifier

    private struct PendingCandidate: Sendable {
        let request: RelayPairingRequest
        let candidate: RelayPairingCandidate
        let keyMaterial: RelaySessionKeyMaterial
        var macApproved = false
        var clientConfirmed = false
    }

    private var challenge: RelaySessionChallenge
    private var serverPrivateKeyData: Data
    private var serverNonce: Data
    private let challengeTTLMilliseconds: Int64
    private let candidateTTLMilliseconds: Int64
    private let sessionTTLMilliseconds: Int64
    private let maximumCandidates: Int
    private var candidateAttempts = 0
    private var pendingCandidate: PendingCandidate?
    private var completed = false

    public init(
        sessionID: RelaySessionIdentifier = .random(),
        serverPrivateKeyData: Data? = nil,
        serverNonce: Data? = nil,
        now: Date = Date(),
        challengeTTL: TimeInterval = 120,
        candidateTTL: TimeInterval = 60,
        sessionTTL: TimeInterval = 7_200,
        maximumCandidates: Int = 3
    ) throws {
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now),
              let challengeTTLMilliseconds = RelayTime.milliseconds(
                  from: challengeTTL,
                  maximum: RelayLimits.maximumChallengeTTLMilliseconds
              ), challengeTTLMilliseconds > 0,
              let candidateTTLMilliseconds = RelayTime.milliseconds(
                  from: candidateTTL,
                  maximum: RelayLimits.maximumCandidateTTLMilliseconds
              ), candidateTTLMilliseconds > 0,
              let sessionTTLMilliseconds = RelayTime.milliseconds(
                  from: sessionTTL,
                  maximum: RelayLimits.maximumSessionTTLMilliseconds
              ), sessionTTLMilliseconds > 0,
              (1 ... RelayLimits.maximumPairingCandidates).contains(maximumCandidates),
              let challengeExpiration = RelayTime.adding(challengeTTLMilliseconds, to: nowMilliseconds)
        else {
            throw RelaySessionError.invalidValidationPolicy
        }

        let privateKeyData: Data
        if let serverPrivateKeyData {
            privateKeyData = serverPrivateKeyData
        } else {
            privateKeyData = Curve25519.KeyAgreement.PrivateKey().rawRepresentation
        }
        guard privateKeyData.count == RelayCrypto.curve25519KeyLength else {
            throw RelaySessionError.invalidPublicKey
        }

        let privateKey: Curve25519.KeyAgreement.PrivateKey
        do {
            privateKey = try Curve25519.KeyAgreement.PrivateKey(rawRepresentation: privateKeyData)
        } catch {
            throw RelaySessionError.invalidPublicKey
        }

        let committedServerNonce = serverNonce ?? RelayCrypto.randomBytes(count: RelayCrypto.nonceLength)
        guard committedServerNonce.count == RelayCrypto.nonceLength else {
            throw RelaySessionError.invalidNonce
        }
        let initialChallenge = try RelaySessionChallenge(
            sessionID: sessionID,
            serverPublicKey: privateKey.publicKey.rawRepresentation,
            serverNonceCommitment: RelayCrypto.serverNonceCommitment(committedServerNonce),
            expiresAtUnixMilliseconds: challengeExpiration
        )
        self.sessionID = sessionID
        challenge = initialChallenge
        self.serverPrivateKeyData = privateKeyData
        self.serverNonce = committedServerNonce
        self.challengeTTLMilliseconds = challengeTTLMilliseconds
        self.candidateTTLMilliseconds = candidateTTLMilliseconds
        self.sessionTTLMilliseconds = sessionTTLMilliseconds
        self.maximumCandidates = maximumCandidates
    }

    public func currentChallenge(now: Date) throws -> RelaySessionChallenge {
        guard !completed else { throw RelaySessionError.pairingAlreadyCompleted }
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now) else {
            throw RelaySessionError.invalidTimestamp
        }
        try discardExpiredCandidate(nowUnixMilliseconds: nowMilliseconds)
        guard candidateAttempts < maximumCandidates || pendingCandidate != nil else {
            throw RelaySessionError.pairingAttemptsExhausted
        }
        if pendingCandidate == nil, nowMilliseconds > challenge.expiresAtUnixMilliseconds {
            try rotateChallenge(nowUnixMilliseconds: nowMilliseconds)
        }
        return challenge
    }

    public func accept(_ request: RelayPairingRequest, now: Date) throws -> RelayServerPairingResult {
        guard !completed else {
            throw RelaySessionError.pairingAlreadyCompleted
        }
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now) else {
            throw RelaySessionError.invalidTimestamp
        }
        try discardExpiredCandidate(nowUnixMilliseconds: nowMilliseconds)
        guard pendingCandidate == nil else {
            throw RelaySessionError.pairingCandidateInProgress
        }
        guard candidateAttempts < maximumCandidates else {
            throw RelaySessionError.pairingAttemptsExhausted
        }
        if nowMilliseconds > challenge.expiresAtUnixMilliseconds {
            try rotateChallenge(nowUnixMilliseconds: nowMilliseconds)
        }
        guard request.sessionID == challenge.sessionID,
              RelayCrypto.constantTimeEqual(request.serverNonceCommitment, challenge.serverNonceCommitment)
        else {
            throw RelaySessionError.invalidRequest
        }

        let keyMaterial: RelaySessionKeyMaterial
        do {
            keyMaterial = try RelayCrypto.derivedKeyMaterial(
                sessionID: challenge.sessionID,
                ownPrivateKeyData: serverPrivateKeyData,
                peerPublicKeyData: request.clientPublicKey,
                transcript: RelayCanonical.pairingTranscript(
                    challenge: challenge,
                    request: request,
                    serverNonce: serverNonce
                )
            )
        } catch {
            throw error
        }
        let candidateID = RelayPairingCandidateIdentifier.random()
        let serverProof = RelayCrypto.candidateProof(
            keyMaterial: keyMaterial,
            challenge: challenge,
            request: request,
            serverNonce: serverNonce,
            candidateID: candidateID
        )
        guard let candidateExpiration = RelayTime.adding(candidateTTLMilliseconds, to: nowMilliseconds) else {
            throw RelaySessionError.invalidTimestamp
        }
        let candidate = try RelayPairingCandidate(
            candidateID: candidateID,
            sessionID: challenge.sessionID,
            serverNonce: serverNonce,
            expiresAtUnixMilliseconds: min(candidateExpiration, challenge.expiresAtUnixMilliseconds),
            serverProof: serverProof
        )
        candidateAttempts += 1
        pendingCandidate = PendingCandidate(request: request, candidate: candidate, keyMaterial: keyMaterial)
        return RelayServerPairingResult(
            candidate: candidate,
            provisionalSession: RelayEstablishedSession(
                sessionID: challenge.sessionID,
                role: .server,
                expiresAtUnixMilliseconds: challenge.expiresAtUnixMilliseconds,
                keyMaterial: keyMaterial
            )
        )
    }

    public func pendingCandidateSummary(now: Date) throws -> RelayPendingPairingCandidate? {
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now) else {
            throw RelaySessionError.invalidTimestamp
        }
        try discardExpiredCandidate(nowUnixMilliseconds: nowMilliseconds)
        guard candidateAttempts < maximumCandidates || pendingCandidate != nil else {
            throw RelaySessionError.pairingAttemptsExhausted
        }
        guard let pendingCandidate else { return nil }
        return RelayPendingPairingCandidate(
            candidateID: pendingCandidate.candidate.candidateID,
            shortAuthenticationString: RelayCrypto.shortAuthenticationString(
                keyMaterial: pendingCandidate.keyMaterial,
                challenge: challenge,
                request: pendingCandidate.request,
                serverNonce: serverNonce
            ),
            expiresAtUnixMilliseconds: pendingCandidate.candidate.expiresAtUnixMilliseconds,
            isMacApproved: pendingCandidate.macApproved
        )
    }

    public func approve(candidateID: RelayPairingCandidateIdentifier, now: Date) throws {
        try ensurePendingCandidate(candidateID: candidateID, now: now)
        pendingCandidate?.macApproved = true
    }

    public func reject(candidateID: RelayPairingCandidateIdentifier, now: Date) throws {
        try ensurePendingCandidate(candidateID: candidateID, now: now)
        pendingCandidate = nil
        if candidateAttempts < maximumCandidates,
           let nowMilliseconds = RelayTime.unixMilliseconds(for: now) {
            try rotateChallenge(nowUnixMilliseconds: nowMilliseconds)
        }
    }

    public func confirm(_ confirmation: RelayPairingConfirmation, now: Date) throws -> RelayServerConfirmationResult {
        try ensurePendingCandidate(candidateID: confirmation.candidateID, now: now)
        guard var pendingCandidate else { throw RelaySessionError.pairingCandidateNotFound }

        if confirmation.decision == .notMyMac {
            self.pendingCandidate = nil
            if candidateAttempts < maximumCandidates,
               let nowMilliseconds = RelayTime.unixMilliseconds(for: now) {
                try rotateChallenge(nowUnixMilliseconds: nowMilliseconds)
            }
            return RelayServerConfirmationResult(
                response: try RelayPairingConfirmationResponse(
                    candidateID: confirmation.candidateID,
                    state: .rejected
                ),
                session: nil
            )
        }

        let expectedConfirmation = RelayCrypto.clientConfirmationMAC(
            keyMaterial: pendingCandidate.keyMaterial,
            challenge: challenge,
            request: pendingCandidate.request,
            serverNonce: serverNonce,
            candidateID: pendingCandidate.candidate.candidateID
        )
        guard let clientConfirmationMAC = confirmation.clientConfirmationMAC,
              RelayCrypto.constantTimeEqual(expectedConfirmation, clientConfirmationMAC)
        else {
            throw RelaySessionError.clientConfirmationMismatch
        }
        pendingCandidate.clientConfirmed = true
        guard pendingCandidate.macApproved else {
            self.pendingCandidate = pendingCandidate
            return RelayServerConfirmationResult(
                response: try RelayPairingConfirmationResponse(
                    candidateID: confirmation.candidateID,
                    state: .waitingForMac
                ),
                session: nil
            )
        }
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now),
              let sessionExpiration = RelayTime.adding(sessionTTLMilliseconds, to: nowMilliseconds)
        else { throw RelaySessionError.invalidTimestamp }

        let acceptance = try RelayPairingAcceptance(
            candidateID: pendingCandidate.candidate.candidateID,
            sessionID: challenge.sessionID,
            expiresAtUnixMilliseconds: sessionExpiration,
            serverConfirmationMAC: RelayCrypto.serverConfirmationMAC(
                keyMaterial: pendingCandidate.keyMaterial,
                challenge: challenge,
                request: pendingCandidate.request,
                serverNonce: serverNonce,
                candidateID: pendingCandidate.candidate.candidateID,
                sessionExpirationUnixMilliseconds: sessionExpiration
            )
        )
        completed = true
        self.pendingCandidate = nil
        return RelayServerConfirmationResult(
            response: try RelayPairingConfirmationResponse(
                candidateID: confirmation.candidateID,
                state: .established,
                acceptance: acceptance
            ),
            session: RelayEstablishedSession(
                sessionID: challenge.sessionID,
                role: .server,
                expiresAtUnixMilliseconds: sessionExpiration,
                keyMaterial: pendingCandidate.keyMaterial
            )
        )
    }

    private func ensurePendingCandidate(candidateID: RelayPairingCandidateIdentifier, now: Date) throws {
        guard !completed else { throw RelaySessionError.pairingAlreadyCompleted }
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now) else {
            throw RelaySessionError.invalidTimestamp
        }
        let expiredCandidateID = pendingCandidate?.candidate.candidateID
        try discardExpiredCandidate(nowUnixMilliseconds: nowMilliseconds)
        if pendingCandidate == nil, expiredCandidateID == candidateID {
            throw RelaySessionError.confirmationExpired
        }
        guard let pendingCandidate else { throw RelaySessionError.pairingCandidateNotFound }
        guard pendingCandidate.candidate.candidateID == candidateID else { throw RelaySessionError.pairingCandidateNotFound }
    }

    private func discardExpiredCandidate(nowUnixMilliseconds: Int64) throws {
        guard let pendingCandidate,
              nowUnixMilliseconds > pendingCandidate.candidate.expiresAtUnixMilliseconds
        else { return }
        self.pendingCandidate = nil
        if candidateAttempts < maximumCandidates {
            try rotateChallenge(nowUnixMilliseconds: nowUnixMilliseconds)
        }
    }

    private func rotateChallenge(nowUnixMilliseconds: Int64) throws {
        guard let challengeExpiration = RelayTime.adding(challengeTTLMilliseconds, to: nowUnixMilliseconds) else {
            throw RelaySessionError.invalidTimestamp
        }
        let privateKey = Curve25519.KeyAgreement.PrivateKey()
        let nonce = RelayCrypto.randomBytes(count: RelayCrypto.nonceLength)
        challenge = try RelaySessionChallenge(
            sessionID: sessionID,
            serverPublicKey: privateKey.publicKey.rawRepresentation,
            serverNonceCommitment: RelayCrypto.serverNonceCommitment(nonce),
            expiresAtUnixMilliseconds: challengeExpiration
        )
        serverPrivateKeyData = privateKey.rawRepresentation
        serverNonce = nonce
    }
}

public struct RelayProvisionalSession: Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let candidateID: RelayPairingCandidateIdentifier
    public let shortAuthenticationString: RelayShortAuthenticationString
    let authenticationSession: RelayEstablishedSession

    private let challenge: RelaySessionChallenge
    private let request: RelayPairingRequest
    private let serverNonce: Data
    private let keyMaterial: RelaySessionKeyMaterial

    fileprivate init(
        candidate: RelayPairingCandidate,
        challenge: RelaySessionChallenge,
        request: RelayPairingRequest,
        keyMaterial: RelaySessionKeyMaterial
    ) {
        candidateID = candidate.candidateID
        shortAuthenticationString = RelayCrypto.shortAuthenticationString(
            keyMaterial: keyMaterial,
            challenge: challenge,
            request: request,
            serverNonce: candidate.serverNonce
        )
        authenticationSession = RelayEstablishedSession(
            sessionID: challenge.sessionID,
            role: .client,
            expiresAtUnixMilliseconds: candidate.expiresAtUnixMilliseconds,
            keyMaterial: keyMaterial
        )
        self.challenge = challenge
        self.request = request
        serverNonce = candidate.serverNonce
        self.keyMaterial = keyMaterial
    }

    public func confirmation(decision: RelayPairingConfirmationDecision) throws -> RelayPairingConfirmation {
        let mac = decision == .codesMatch
            ? RelayCrypto.clientConfirmationMAC(
                keyMaterial: keyMaterial,
                challenge: challenge,
                request: request,
                serverNonce: serverNonce,
                candidateID: candidateID
            )
            : nil
        return try RelayPairingConfirmation(
            candidateID: candidateID,
            decision: decision,
            clientConfirmationMAC: mac
        )
    }

    public func complete(with acceptance: RelayPairingAcceptance, now: Date) throws -> RelayEstablishedSession {
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now),
              nowMilliseconds <= authenticationSession.expiresAtUnixMilliseconds,
              acceptance.candidateID == candidateID,
              acceptance.sessionID == challenge.sessionID,
              acceptance.expiresAtUnixMilliseconds >= nowMilliseconds
        else { throw RelaySessionError.invalidRequest }
        let expectedConfirmation = RelayCrypto.serverConfirmationMAC(
            keyMaterial: keyMaterial,
            challenge: challenge,
            request: request,
            serverNonce: serverNonce,
            candidateID: candidateID,
            sessionExpirationUnixMilliseconds: acceptance.expiresAtUnixMilliseconds
        )
        guard RelayCrypto.constantTimeEqual(expectedConfirmation, acceptance.serverConfirmationMAC) else {
            throw RelaySessionError.serverConfirmationMismatch
        }
        return RelayEstablishedSession(
            sessionID: challenge.sessionID,
            role: .client,
            expiresAtUnixMilliseconds: acceptance.expiresAtUnixMilliseconds,
            keyMaterial: keyMaterial
        )
    }

    public var description: String { "RelayProvisionalSession(candidateID: \(candidateID.rawValue), secrets: redacted)" }
    public var debugDescription: String { description }
}

public struct RelayClientPairingAttempt: Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let request: RelayPairingRequest
    private let challenge: RelaySessionChallenge
    private let clientPrivateKeyData: Data

    public init(
        challenge: RelaySessionChallenge,
        clientPrivateKeyData: Data,
        clientNonce: Data,
        now: Date
    ) throws {
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now),
              nowMilliseconds <= challenge.expiresAtUnixMilliseconds
        else { throw RelaySessionError.expiredChallenge }
        guard clientPrivateKeyData.count == RelayCrypto.curve25519KeyLength else { throw RelaySessionError.invalidPublicKey }
        guard clientNonce.count == RelayCrypto.nonceLength else { throw RelaySessionError.invalidNonce }
        let privateKey: Curve25519.KeyAgreement.PrivateKey
        do { privateKey = try Curve25519.KeyAgreement.PrivateKey(rawRepresentation: clientPrivateKeyData) }
        catch { throw RelaySessionError.invalidPublicKey }
        request = try RelayPairingRequest(
            sessionID: challenge.sessionID,
            serverNonceCommitment: challenge.serverNonceCommitment,
            clientPublicKey: privateKey.publicKey.rawRepresentation,
            clientNonce: clientNonce
        )
        self.challenge = challenge
        self.clientPrivateKeyData = clientPrivateKeyData
    }

    public init(challenge: RelaySessionChallenge, now: Date) throws {
        let privateKey = Curve25519.KeyAgreement.PrivateKey()
        try self.init(
            challenge: challenge,
            clientPrivateKeyData: privateKey.rawRepresentation,
            clientNonce: RelayCrypto.randomBytes(count: RelayCrypto.nonceLength),
            now: now
        )
    }

    public func complete(with candidate: RelayPairingCandidate, now: Date) throws -> RelayProvisionalSession {
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now),
              nowMilliseconds <= challenge.expiresAtUnixMilliseconds,
              candidate.sessionID == challenge.sessionID,
              candidate.expiresAtUnixMilliseconds >= nowMilliseconds,
              candidate.expiresAtUnixMilliseconds <= challenge.expiresAtUnixMilliseconds,
              candidate.expiresAtUnixMilliseconds - nowMilliseconds <= RelayLimits.maximumCandidateTTLMilliseconds
        else { throw RelaySessionError.invalidRequest }
        guard RelayCrypto.constantTimeEqual(
            RelayCrypto.serverNonceCommitment(candidate.serverNonce),
            challenge.serverNonceCommitment
        ) else { throw RelaySessionError.nonceCommitmentMismatch }
        let transcript = RelayCanonical.pairingTranscript(
            challenge: challenge,
            request: request,
            serverNonce: candidate.serverNonce
        )
        let keyMaterial = try RelayCrypto.derivedKeyMaterial(
            sessionID: challenge.sessionID,
            ownPrivateKeyData: clientPrivateKeyData,
            peerPublicKeyData: challenge.serverPublicKey,
            transcript: transcript
        )
        let expectedProof = RelayCrypto.candidateProof(
            keyMaterial: keyMaterial,
            challenge: challenge,
            request: request,
            serverNonce: candidate.serverNonce,
            candidateID: candidate.candidateID
        )
        guard RelayCrypto.constantTimeEqual(expectedProof, candidate.serverProof) else {
            throw RelaySessionError.candidateProofMismatch
        }
        return RelayProvisionalSession(candidate: candidate, challenge: challenge, request: request, keyMaterial: keyMaterial)
    }

    public var description: String { "RelayClientPairingAttempt(sessionID: \(request.sessionID.rawValue), keyMaterial: redacted)" }
    public var debugDescription: String { description }
}

public struct RelayEstablishedSession: Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let sessionID: RelaySessionIdentifier
    public let role: RelaySessionRole
    public let expiresAtUnixMilliseconds: Int64
    public let sessionIdentity: RelaySessionIdentity
    public let mediaCapability: RelayMediaCapability

    private let requestSigningKey: Data
    private let peerRequestVerificationKey: Data
    private let serverResponseAuthenticationKey: Data

    fileprivate init(
        sessionID: RelaySessionIdentifier,
        role: RelaySessionRole,
        expiresAtUnixMilliseconds: Int64,
        keyMaterial: RelaySessionKeyMaterial
    ) {
        self.sessionID = sessionID
        self.role = role
        self.expiresAtUnixMilliseconds = expiresAtUnixMilliseconds
        sessionIdentity = RelaySessionIdentity(value: RelayCrypto.base64URLEncoded(keyMaterial.sessionIdentity))
        mediaCapability = RelayMediaCapability(value: RelayCrypto.base64URLEncoded(keyMaterial.mediaCapability))
        serverResponseAuthenticationKey = keyMaterial.serverToClientResponseKey
        switch role {
        case .client:
            requestSigningKey = keyMaterial.clientToServerRequestKey
            peerRequestVerificationKey = keyMaterial.serverToClientRequestKey
        case .server:
            requestSigningKey = keyMaterial.serverToClientRequestKey
            peerRequestVerificationKey = keyMaterial.clientToServerRequestKey
        }
    }

    public var expirationDate: Date {
        RelayTime.date(fromUnixMilliseconds: expiresAtUnixMilliseconds)
    }

    public var description: String {
        "RelayEstablishedSession(sessionID: \(sessionID.rawValue), role: \(role.rawValue), secrets: redacted)"
    }

    public var debugDescription: String {
        description
    }

    public func signRequest(
        method: String,
        requestTarget: String,
        timestamp: Date,
        nonce: String,
        body: Data
    ) throws -> RelayAuthenticatedRequest {
        guard let timestampUnixMilliseconds = RelayTime.unixMilliseconds(for: timestamp) else {
            throw RelaySessionError.invalidTimestamp
        }
        guard timestampUnixMilliseconds <= expiresAtUnixMilliseconds else {
            throw RelaySessionError.requestExpired
        }

        let bodySHA256 = RelayCrypto.sha256(body)
        let unsignedRequest = try RelayAuthenticatedRequest(
            sessionID: sessionID,
            signerRole: role,
            method: method,
            requestTarget: requestTarget,
            timestampUnixMilliseconds: timestampUnixMilliseconds,
            nonce: nonce,
            bodySHA256: bodySHA256,
            signature: Data(repeating: 0, count: RelayCrypto.sha256Length)
        )
        let signature = RelayCrypto.hmac(
            keyMaterial: requestSigningKey,
            message: RelayCanonical.authenticatedRequestTranscript(unsignedRequest)
        )
        return try RelayAuthenticatedRequest(
            sessionID: sessionID,
            signerRole: role,
            method: method,
            requestTarget: requestTarget,
            timestampUnixMilliseconds: timestampUnixMilliseconds,
            nonce: nonce,
            bodySHA256: bodySHA256,
            signature: signature
        )
    }

    public func verify(
        _ request: RelayAuthenticatedRequest,
        actualMethod: String,
        actualRequestTarget: String,
        body: Data,
        now: Date,
        policy: RelayRequestValidationPolicy,
        replayStore: RelayReplayNonceStore
    ) async throws {
        guard let nowUnixMilliseconds = RelayTime.unixMilliseconds(for: now) else {
            throw RelaySessionError.invalidTimestamp
        }
        guard request.sessionID == sessionID, request.signerRole == role.peer else {
            throw RelaySessionError.invalidRequest
        }
        guard RelayCanonical.isValidMethod(actualMethod),
              RelayCanonical.isValidRequestTarget(actualRequestTarget),
              RelayCrypto.constantTimeEqual(Data(request.method.utf8), Data(actualMethod.utf8)),
              RelayCrypto.constantTimeEqual(Data(request.requestTarget.utf8), Data(actualRequestTarget.utf8))
        else {
            throw RelaySessionError.invalidRequest
        }
        guard nowUnixMilliseconds <= expiresAtUnixMilliseconds else {
            throw RelaySessionError.requestExpired
        }
        guard request.timestampUnixMilliseconds <= expiresAtUnixMilliseconds else {
            throw RelaySessionError.requestExpired
        }
        guard let latestAllowedTimestamp = RelayTime.adding(
            policy.allowedFutureSkewMilliseconds,
            to: nowUnixMilliseconds
        ) else {
            throw RelaySessionError.invalidTimestamp
        }
        guard request.timestampUnixMilliseconds <= latestAllowedTimestamp else {
            throw RelaySessionError.requestTimestampTooFarInFuture
        }

        let oldestAllowedTimestamp: Int64
        let subtraction = nowUnixMilliseconds.subtractingReportingOverflow(policy.maximumAgeMilliseconds)
        if subtraction.overflow || subtraction.partialValue < 0 {
            oldestAllowedTimestamp = 0
        } else {
            oldestAllowedTimestamp = subtraction.partialValue
        }
        guard request.timestampUnixMilliseconds >= oldestAllowedTimestamp else {
            throw RelaySessionError.requestExpired
        }
        guard RelayCrypto.constantTimeEqual(RelayCrypto.sha256(body), request.bodySHA256) else {
            throw RelaySessionError.requestBodyMismatch
        }

        let expectedSignature = RelayCrypto.hmac(
            keyMaterial: peerRequestVerificationKey,
            message: RelayCanonical.authenticatedRequestTranscript(request)
        )
        guard RelayCrypto.constantTimeEqual(expectedSignature, request.signature) else {
            throw RelaySessionError.requestSignatureMismatch
        }
        guard let requestFreshnessExpiration = RelayTime.adding(
            policy.maximumAgeMilliseconds,
            to: request.timestampUnixMilliseconds
        ) else {
            throw RelaySessionError.invalidTimestamp
        }
        try await replayStore.checkAndInsert(
            sessionID: sessionID,
            nonce: request.nonce,
            validThroughUnixMilliseconds: min(requestFreshnessExpiration, expiresAtUnixMilliseconds),
            now: now
        )
    }

    public func verify(
        _ request: RelayAuthenticatedRequest,
        actualMethod: String,
        actualRequestTarget: String,
        body: Data,
        now: Date,
        replayStore: RelayReplayNonceStore
    ) async throws {
        try await verify(
            request,
            actualMethod: actualMethod,
            actualRequestTarget: actualRequestTarget,
            body: body,
            now: now,
            policy: try RelayRequestValidationPolicy(),
            replayStore: replayStore
        )
    }

    public func authenticateResponse(
        requestNonce: String,
        statusCode: Int,
        body: Data
    ) throws -> RelayAuthenticatedResponse {
        guard role == .server else {
            throw RelaySessionError.invalidResponse
        }
        let bodySHA256 = RelayCrypto.sha256(body)
        let unsignedResponse = try RelayAuthenticatedResponse(
            sessionID: sessionID,
            signerRole: .server,
            requestNonce: requestNonce,
            statusCode: statusCode,
            bodySHA256: bodySHA256,
            signature: Data(repeating: 0, count: RelayCrypto.sha256Length)
        )
        let signature = RelayCrypto.hmac(
            keyMaterial: serverResponseAuthenticationKey,
            message: RelayCanonical.authenticatedResponseTranscript(unsignedResponse)
        )
        return try RelayAuthenticatedResponse(
            sessionID: sessionID,
            signerRole: .server,
            requestNonce: requestNonce,
            statusCode: statusCode,
            bodySHA256: bodySHA256,
            signature: signature
        )
    }

    public func verifyResponse(
        _ response: RelayAuthenticatedResponse,
        requestNonce: String,
        actualStatusCode: Int,
        body: Data,
        now: Date
    ) throws {
        guard role == .client,
              response.sessionID == sessionID,
              response.signerRole == .server,
              RelayCanonical.isValidNonce(requestNonce),
              RelayCrypto.constantTimeEqual(Data(response.requestNonce.utf8), Data(requestNonce.utf8)),
              response.statusCode == actualStatusCode
        else {
            throw RelaySessionError.invalidResponse
        }
        guard let nowUnixMilliseconds = RelayTime.unixMilliseconds(for: now) else {
            throw RelaySessionError.invalidTimestamp
        }
        guard nowUnixMilliseconds <= expiresAtUnixMilliseconds else {
            throw RelaySessionError.requestExpired
        }
        guard RelayCrypto.constantTimeEqual(RelayCrypto.sha256(body), response.bodySHA256) else {
            throw RelaySessionError.responseBodyMismatch
        }
        let expectedSignature = RelayCrypto.hmac(
            keyMaterial: serverResponseAuthenticationKey,
            message: RelayCanonical.authenticatedResponseTranscript(response)
        )
        guard RelayCrypto.constantTimeEqual(expectedSignature, response.signature) else {
            throw RelaySessionError.responseSignatureMismatch
        }
    }
}

struct RelaySessionKeyMaterial: Sendable {
    let sessionIdentity: Data
    let clientToServerRequestKey: Data
    let serverToClientRequestKey: Data
    let serverToClientResponseKey: Data
    let mediaCapability: Data
    let candidateProofKey: Data
    let shortAuthenticationStringKey: Data
    let clientConfirmationKey: Data
    let serverConfirmationKey: Data
}

enum RelayCrypto {
    static let curve25519KeyLength = 32
    static let nonceLength = 32
    static let sha256Length = 32

    static func randomBytes(count: Int) -> Data {
        precondition((1 ... sha256Length).contains(count))
        return SymmetricKey(size: .bits256).withUnsafeBytes { Data($0.prefix(count)) }
    }

    static func derivedKeyMaterial(
        sessionID: RelaySessionIdentifier,
        ownPrivateKeyData: Data,
        peerPublicKeyData: Data,
        transcript: Data
    ) throws -> RelaySessionKeyMaterial {
        let privateKey: Curve25519.KeyAgreement.PrivateKey
        let peerPublicKey: Curve25519.KeyAgreement.PublicKey
        do {
            privateKey = try Curve25519.KeyAgreement.PrivateKey(rawRepresentation: ownPrivateKeyData)
            peerPublicKey = try Curve25519.KeyAgreement.PublicKey(rawRepresentation: peerPublicKeyData)
        } catch {
            throw RelaySessionError.invalidPublicKey
        }

        let sharedSecret: SharedSecret
        do {
            sharedSecret = try privateKey.sharedSecretFromKeyAgreement(with: peerPublicKey)
        } catch {
            throw RelaySessionError.invalidPublicKey
        }
        let pairingTranscriptSHA256 = sha256(transcript)
        let masterKey = sharedSecret.hkdfDerivedSymmetricKey(
            using: SHA256.self,
            salt: pairingTranscriptSHA256,
            sharedInfo: RelayCanonical.sessionMasterTranscript(
                sessionID: sessionID,
                pairingTranscriptSHA256: pairingTranscriptSHA256
            ),
            outputByteCount: sha256Length
        ).withUnsafeBytes { Data($0) }

        return RelaySessionKeyMaterial(
            sessionIdentity: derivedValue(masterKey: masterKey, label: "session-identity", sessionID: sessionID),
            clientToServerRequestKey: derivedValue(masterKey: masterKey, label: "request-client-to-server", sessionID: sessionID),
            serverToClientRequestKey: derivedValue(masterKey: masterKey, label: "request-server-to-client", sessionID: sessionID),
            serverToClientResponseKey: derivedValue(masterKey: masterKey, label: "response-server-to-client", sessionID: sessionID),
            mediaCapability: derivedValue(masterKey: masterKey, label: "media-capability", sessionID: sessionID),
            candidateProofKey: derivedValue(masterKey: masterKey, label: "candidate-proof", sessionID: sessionID),
            shortAuthenticationStringKey: derivedValue(masterKey: masterKey, label: "short-authentication-string", sessionID: sessionID),
            clientConfirmationKey: derivedValue(masterKey: masterKey, label: "client-confirmation", sessionID: sessionID),
            serverConfirmationKey: derivedValue(masterKey: masterKey, label: "server-confirmation", sessionID: sessionID)
        )
    }

    static func candidateProof(
        keyMaterial: RelaySessionKeyMaterial,
        challenge: RelaySessionChallenge,
        request: RelayPairingRequest,
        serverNonce: Data,
        candidateID: RelayPairingCandidateIdentifier
    ) -> Data {
        hmac(
            keyMaterial: keyMaterial.candidateProofKey,
            message: RelayCanonical.candidateTranscript(
                challenge: challenge,
                request: request,
                serverNonce: serverNonce,
                candidateID: candidateID
            )
        )
    }

    static func clientConfirmationMAC(
        keyMaterial: RelaySessionKeyMaterial,
        challenge: RelaySessionChallenge,
        request: RelayPairingRequest,
        serverNonce: Data,
        candidateID: RelayPairingCandidateIdentifier
    ) -> Data {
        hmac(
            keyMaterial: keyMaterial.clientConfirmationKey,
            message: RelayCanonical.clientConfirmationTranscript(
                challenge: challenge,
                request: request,
                serverNonce: serverNonce,
                candidateID: candidateID
            )
        )
    }

    static func serverConfirmationMAC(
        keyMaterial: RelaySessionKeyMaterial,
        challenge: RelaySessionChallenge,
        request: RelayPairingRequest,
        serverNonce: Data,
        candidateID: RelayPairingCandidateIdentifier,
        sessionExpirationUnixMilliseconds: Int64
    ) -> Data {
        hmac(
            keyMaterial: keyMaterial.serverConfirmationKey,
            message: RelayCanonical.serverConfirmationTranscript(
                challenge: challenge,
                request: request,
                serverNonce: serverNonce,
                candidateID: candidateID,
                sessionExpirationUnixMilliseconds: sessionExpirationUnixMilliseconds
            )
        )
    }

    static func shortAuthenticationString(
        keyMaterial: RelaySessionKeyMaterial,
        challenge: RelaySessionChallenge,
        request: RelayPairingRequest,
        serverNonce: Data
    ) -> RelayShortAuthenticationString {
        let material = hmac(
            keyMaterial: keyMaterial.shortAuthenticationStringKey,
            message: RelayCanonical.shortAuthenticationStringTranscript(
                challenge: challenge,
                request: request,
                serverNonce: serverNonce
            )
        )
        let value = material.prefix(8).reduce(UInt64(0)) { partial, byte in
            (partial << 8) | UInt64(byte)
        } % 1_000_000
        return RelayShortAuthenticationString(digits: String(format: "%06llu", value))
    }

    static func hmac(keyMaterial: Data, message: Data) -> Data {
        Data(HMAC<SHA256>.authenticationCode(for: message, using: SymmetricKey(data: keyMaterial)))
    }

    static func sha256(_ data: Data) -> Data {
        Data(SHA256.hash(data: data))
    }

    static func serverNonceCommitment(_ serverNonce: Data) -> Data {
        sha256(RelayCanonical.serverNonceCommitmentTranscript(serverNonce))
    }

    static func constantTimeEqual(_ lhs: Data, _ rhs: Data) -> Bool {
        let lhsBytes = [UInt8](lhs)
        let rhsBytes = [UInt8](rhs)
        let maximumCount = max(lhsBytes.count, rhsBytes.count)
        var difference = UInt(lhsBytes.count ^ rhsBytes.count)
        for index in 0 ..< maximumCount {
            let lhsByte = index < lhsBytes.count ? lhsBytes[index] : 0
            let rhsByte = index < rhsBytes.count ? rhsBytes[index] : 0
            difference |= UInt(lhsByte ^ rhsByte)
        }
        return difference == 0
    }

    static func base64URLEncoded(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    private static func derivedValue(
        masterKey: Data,
        label: String,
        sessionID: RelaySessionIdentifier
    ) -> Data {
        hmac(
            keyMaterial: masterKey,
            message: RelayCanonical.keyDerivationTranscript(label: label, sessionID: sessionID)
        )
    }
}

enum RelayCanonical {
    static func serverNonceCommitmentTranscript(_ serverNonce: Data) -> Data {
        transcript(
            domain: "bd-to-avp.relay.server-nonce-commitment.v3",
            fields: [("serverNonce", serverNonce)]
        )
    }

    static func pairingTranscript(
        challenge: RelaySessionChallenge,
        request: RelayPairingRequest,
        serverNonce: Data
    ) -> Data {
        transcript(
            domain: "bd-to-avp.relay.pairing-transcript.v3",
            fields: [
                ("challenge", challengeTranscript(challenge)),
                ("clientPublicKey", request.clientPublicKey),
                ("clientNonce", request.clientNonce),
                ("serverNonce", serverNonce),
            ]
        )
    }

    static func candidateTranscript(
        challenge: RelaySessionChallenge,
        request: RelayPairingRequest,
        serverNonce: Data,
        candidateID: RelayPairingCandidateIdentifier
    ) -> Data {
        transcript(
            domain: "bd-to-avp.relay.pairing-candidate.v3",
            fields: [
                ("pairingTranscript", pairingTranscript(challenge: challenge, request: request, serverNonce: serverNonce)),
                ("candidateID", Data(candidateID.rawValue.utf8)),
            ]
        )
    }

    static func shortAuthenticationStringTranscript(
        challenge: RelaySessionChallenge,
        request: RelayPairingRequest,
        serverNonce: Data
    ) -> Data {
        transcript(
            domain: "bd-to-avp.relay.short-authentication-string.v3",
            fields: [
                ("pairingTranscript", pairingTranscript(challenge: challenge, request: request, serverNonce: serverNonce)),
            ]
        )
    }

    static func clientConfirmationTranscript(
        challenge: RelaySessionChallenge,
        request: RelayPairingRequest,
        serverNonce: Data,
        candidateID: RelayPairingCandidateIdentifier
    ) -> Data {
        transcript(
            domain: "bd-to-avp.relay.client-confirmation.v3",
            fields: [
                ("candidateTranscript", candidateTranscript(challenge: challenge, request: request, serverNonce: serverNonce, candidateID: candidateID)),
            ]
        )
    }

    static func serverConfirmationTranscript(
        challenge: RelaySessionChallenge,
        request: RelayPairingRequest,
        serverNonce: Data,
        candidateID: RelayPairingCandidateIdentifier,
        sessionExpirationUnixMilliseconds: Int64
    ) -> Data {
        transcript(
            domain: "bd-to-avp.relay.server-confirmation.v3",
            fields: [
                ("candidateTranscript", candidateTranscript(challenge: challenge, request: request, serverNonce: serverNonce, candidateID: candidateID)),
                ("sessionExpiresAtUnixMilliseconds", integerData(sessionExpirationUnixMilliseconds)),
            ]
        )
    }

    static func authenticatedRequestTranscript(_ request: RelayAuthenticatedRequest) -> Data {
        transcript(
            domain: "bd-to-avp.relay.authenticated-request.v2",
            fields: [
                ("sessionID", Data(request.sessionID.rawValue.utf8)),
                ("signerRole", Data(request.signerRole.rawValue.utf8)),
                ("method", Data(request.method.utf8)),
                ("requestTarget", Data(request.requestTarget.utf8)),
                ("timestampUnixMilliseconds", integerData(request.timestampUnixMilliseconds)),
                ("nonce", Data(request.nonce.utf8)),
                ("bodySHA256", request.bodySHA256),
            ]
        )
    }

    static func authenticatedResponseTranscript(_ response: RelayAuthenticatedResponse) -> Data {
        transcript(
            domain: "bd-to-avp.relay.authenticated-response.v1",
            fields: [
                ("sessionID", Data(response.sessionID.rawValue.utf8)),
                ("signerRole", Data(response.signerRole.rawValue.utf8)),
                ("requestNonce", Data(response.requestNonce.utf8)),
                ("statusCode", integerData(Int64(response.statusCode))),
                ("bodySHA256", response.bodySHA256),
            ]
        )
    }

    static func sessionMasterTranscript(
        sessionID: RelaySessionIdentifier,
        pairingTranscriptSHA256: Data
    ) -> Data {
        transcript(
            domain: "bd-to-avp.relay.session-master.v4",
            fields: [
                ("sessionID", Data(sessionID.rawValue.utf8)),
                ("pairingTranscriptSHA256", pairingTranscriptSHA256),
            ]
        )
    }

    static func keyDerivationTranscript(label: String, sessionID: RelaySessionIdentifier) -> Data {
        transcript(
            domain: "bd-to-avp.relay.key-derivation.v3",
            fields: [
                ("label", Data(label.utf8)),
                ("sessionID", Data(sessionID.rawValue.utf8)),
            ]
        )
    }

    static func isValidMethod(_ value: String) -> Bool {
        let bytes = value.utf8
        guard (1 ... RelayLimits.maximumMethodLength).contains(bytes.count) else {
            return false
        }
        return bytes.allSatisfy { byte in
            (byte >= Character("A").asciiValue! && byte <= Character("Z").asciiValue!)
                || (byte >= Character("0").asciiValue! && byte <= Character("9").asciiValue!)
                || "!#$%&'*+-.^_`|~".utf8.contains(byte)
        }
    }

    static func isValidRequestTarget(_ value: String) -> Bool {
        let bytes = value.utf8
        guard (1 ... RelayLimits.maximumRequestTargetLength).contains(bytes.count),
              bytes.first == Character("/").asciiValue
        else {
            return false
        }
        return bytes.allSatisfy { byte in
            byte >= 33 && byte <= 126 && byte != Character("#").asciiValue
        }
    }

    static func isValidNonce(_ value: String) -> Bool {
        let bytes = value.utf8
        return (RelayLimits.minimumNonceLength ... RelayLimits.maximumNonceLength).contains(bytes.count)
            && bytes.allSatisfy { byte in
                (byte >= Character("A").asciiValue! && byte <= Character("Z").asciiValue!)
                    || (byte >= Character("a").asciiValue! && byte <= Character("z").asciiValue!)
                    || (byte >= Character("0").asciiValue! && byte <= Character("9").asciiValue!)
                    || byte == Character("-").asciiValue
                    || byte == Character("_").asciiValue
            }
    }

    static func isValidHTTPStatusCode(_ value: Int) -> Bool {
        (100 ... 599).contains(value)
    }

    private static func challengeTranscript(_ challenge: RelaySessionChallenge) -> Data {
        transcript(
            domain: "bd-to-avp.relay.challenge.v3",
            fields: [
                ("sessionID", Data(challenge.sessionID.rawValue.utf8)),
                ("serverPublicKey", challenge.serverPublicKey),
                ("serverNonceCommitment", challenge.serverNonceCommitment),
                ("expiresAtUnixMilliseconds", integerData(challenge.expiresAtUnixMilliseconds)),
            ]
        )
    }

    private static func transcript(domain: String, fields: [(String, Data)]) -> Data {
        var data = Data(domain.utf8)
        data.append(0)
        for (name, value) in fields {
            let nameData = Data(name.utf8)
            data.append(lengthData(nameData.count))
            data.append(nameData)
            data.append(lengthData(value.count))
            data.append(value)
        }
        return data
    }

    private static func lengthData(_ value: Int) -> Data {
        var bigEndianValue = UInt64(value).bigEndian
        return withUnsafeBytes(of: &bigEndianValue) { Data($0) }
    }

    private static func integerData(_ value: Int64) -> Data {
        var bigEndianValue = UInt64(bitPattern: value).bigEndian
        return withUnsafeBytes(of: &bigEndianValue) { Data($0) }
    }
}

enum RelayLimits {
    static let maximumUnixMilliseconds: Int64 = 253_402_300_799_999
    static let maximumChallengeTTLMilliseconds: Int64 = 10 * 60 * 1_000
    static let maximumCandidateTTLMilliseconds: Int64 = 60 * 1_000
    static let maximumSessionTTLMilliseconds: Int64 = 24 * 60 * 60 * 1_000
    static let maximumRequestAgeMilliseconds: Int64 = 5 * 60 * 1_000
    static let maximumFutureSkewMilliseconds: Int64 = 60 * 1_000
    static let maximumReplayCapacity = 4_096
    static let maximumPairingCandidates = 3
    static let maximumMethodLength = 32
    static let maximumRequestTargetLength = 8_192
    static let minimumNonceLength = 16
    static let maximumNonceLength = 128
}

enum RelayTime {
    static func unixMilliseconds(for date: Date) -> Int64? {
        let seconds = date.timeIntervalSince1970
        guard seconds.isFinite, seconds >= 0 else {
            return nil
        }
        let scaled = seconds * 1_000
        guard scaled.isFinite else {
            return nil
        }
        let rounded = scaled.rounded(.towardZero)
        guard rounded >= 0, rounded <= Double(RelayLimits.maximumUnixMilliseconds) else {
            return nil
        }
        return Int64(rounded)
    }

    static func date(fromUnixMilliseconds value: Int64) -> Date {
        Date(timeIntervalSince1970: TimeInterval(value) / 1_000)
    }

    static func isValidUnixMilliseconds(_ value: Int64) -> Bool {
        (0 ... RelayLimits.maximumUnixMilliseconds).contains(value)
    }

    static func milliseconds(from interval: TimeInterval, maximum: Int64) -> Int64? {
        guard interval.isFinite,
              interval >= 0,
              (0 ... RelayLimits.maximumUnixMilliseconds).contains(maximum)
        else {
            return nil
        }
        let scaled = interval * 1_000
        guard scaled.isFinite else {
            return nil
        }
        let rounded = scaled.rounded(.toNearestOrAwayFromZero)
        guard rounded >= 0, rounded <= Double(maximum) else {
            return nil
        }
        return Int64(rounded)
    }

    static func adding(_ intervalMilliseconds: Int64, to timestampMilliseconds: Int64) -> Int64? {
        guard intervalMilliseconds >= 0, isValidUnixMilliseconds(timestampMilliseconds) else {
            return nil
        }
        let result = timestampMilliseconds.addingReportingOverflow(intervalMilliseconds)
        guard !result.overflow, isValidUnixMilliseconds(result.partialValue) else {
            return nil
        }
        return result.partialValue
    }
}

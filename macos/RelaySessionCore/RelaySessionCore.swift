import CryptoKit
import Foundation

public enum RelaySessionError: Error, Equatable, Sendable {
    case invalidSessionIdentifier
    case invalidPairingCode
    case invalidPublicKey
    case invalidNonce
    case invalidProof
    case invalidTimestamp
    case invalidRequest
    case expiredChallenge
    case pairingProofMismatch
    case acceptanceProofMismatch
    case pairingAlreadyCompleted
    case pairingAttemptsExhausted
    case requestBodyMismatch
    case requestSignatureMismatch
    case requestExpired
    case requestTimestampTooFarInFuture
    case replayDetected
    case replayCapacityExceeded
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

public struct RelayPairingCode: Equatable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public static let characterCount = 16
    public static let alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

    fileprivate let rawValue: String

    public init(_ enteredValue: String) throws {
        guard enteredValue.utf8.count <= 64 else {
            throw RelaySessionError.invalidPairingCode
        }
        var canonicalBytes: [UInt8] = []
        canonicalBytes.reserveCapacity(Self.characterCount)

        for byte in enteredValue.utf8 {
            if byte == Character("-").asciiValue || byte == Character(" ").asciiValue {
                continue
            }
            let uppercaseByte = byte >= Character("a").asciiValue! && byte <= Character("z").asciiValue!
                ? byte - 32
                : byte
            guard Self.alphabet.utf8.contains(uppercaseByte) else {
                throw RelaySessionError.invalidPairingCode
            }
            canonicalBytes.append(uppercaseByte)
        }

        guard canonicalBytes.count == Self.characterCount else {
            throw RelaySessionError.invalidPairingCode
        }
        rawValue = String(decoding: canonicalBytes, as: UTF8.self)
    }

    public static func random() -> RelayPairingCode {
        let alphabetBytes = Array(alphabet.utf8)
        let randomBytes = RelayCrypto.randomBytes(count: characterCount)
        let canonicalBytes = randomBytes.map { alphabetBytes[Int($0 & 31)] }
        return RelayPairingCode(canonicalValue: String(decoding: canonicalBytes, as: UTF8.self))
    }

    public var formattedValue: String {
        let bytes = Array(rawValue.utf8)
        return stride(from: 0, to: bytes.count, by: 4)
            .map { String(decoding: bytes[$0 ..< min($0 + 4, bytes.count)], as: UTF8.self) }
            .joined(separator: "-")
    }

    public static func == (lhs: RelayPairingCode, rhs: RelayPairingCode) -> Bool {
        RelayCrypto.constantTimeEqual(Data(lhs.rawValue.utf8), Data(rhs.rawValue.utf8))
    }

    public var description: String {
        "<relay pairing code: redacted>"
    }

    public var debugDescription: String {
        description
    }

    private init(canonicalValue: String) {
        rawValue = canonicalValue
    }
}

public struct RelaySessionChallenge: Codable, Equatable, Sendable {
    public let sessionID: RelaySessionIdentifier
    public let serverPublicKey: Data
    public let serverNonce: Data
    public let expiresAtUnixMilliseconds: Int64

    public init(
        sessionID: RelaySessionIdentifier,
        serverPublicKey: Data,
        serverNonce: Data,
        expiresAtUnixMilliseconds: Int64
    ) throws {
        guard serverPublicKey.count == RelayCrypto.curve25519KeyLength else {
            throw RelaySessionError.invalidPublicKey
        }
        guard serverNonce.count == RelayCrypto.nonceLength else {
            throw RelaySessionError.invalidNonce
        }
        guard RelayTime.isValidUnixMilliseconds(expiresAtUnixMilliseconds) else {
            throw RelaySessionError.invalidTimestamp
        }

        self.sessionID = sessionID
        self.serverPublicKey = serverPublicKey
        self.serverNonce = serverNonce
        self.expiresAtUnixMilliseconds = expiresAtUnixMilliseconds
    }

    public var expirationDate: Date {
        RelayTime.date(fromUnixMilliseconds: expiresAtUnixMilliseconds)
    }

    private enum CodingKeys: String, CodingKey {
        case sessionID
        case serverPublicKey
        case serverNonce
        case expiresAtUnixMilliseconds
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(
            sessionID: container.decode(RelaySessionIdentifier.self, forKey: .sessionID),
            serverPublicKey: container.decode(Data.self, forKey: .serverPublicKey),
            serverNonce: container.decode(Data.self, forKey: .serverNonce),
            expiresAtUnixMilliseconds: container.decode(Int64.self, forKey: .expiresAtUnixMilliseconds)
        )
    }
}

public struct RelayPairingRequest: Codable, Equatable, Sendable {
    public let sessionID: RelaySessionIdentifier
    public let clientPublicKey: Data
    public let clientNonce: Data
    public let pairingProof: Data

    public init(
        sessionID: RelaySessionIdentifier,
        clientPublicKey: Data,
        clientNonce: Data,
        pairingProof: Data
    ) throws {
        guard clientPublicKey.count == RelayCrypto.curve25519KeyLength else {
            throw RelaySessionError.invalidPublicKey
        }
        guard clientNonce.count == RelayCrypto.nonceLength else {
            throw RelaySessionError.invalidNonce
        }
        guard pairingProof.count == RelayCrypto.sha256Length else {
            throw RelaySessionError.invalidProof
        }

        self.sessionID = sessionID
        self.clientPublicKey = clientPublicKey
        self.clientNonce = clientNonce
        self.pairingProof = pairingProof
    }

    private enum CodingKeys: String, CodingKey {
        case sessionID
        case clientPublicKey
        case clientNonce
        case pairingProof
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(
            sessionID: container.decode(RelaySessionIdentifier.self, forKey: .sessionID),
            clientPublicKey: container.decode(Data.self, forKey: .clientPublicKey),
            clientNonce: container.decode(Data.self, forKey: .clientNonce),
            pairingProof: container.decode(Data.self, forKey: .pairingProof)
        )
    }
}

public struct RelayPairingAcceptance: Codable, Equatable, Sendable {
    public let sessionID: RelaySessionIdentifier
    public let expiresAtUnixMilliseconds: Int64
    public let serverProof: Data

    public init(
        sessionID: RelaySessionIdentifier,
        expiresAtUnixMilliseconds: Int64,
        serverProof: Data
    ) throws {
        guard RelayTime.isValidUnixMilliseconds(expiresAtUnixMilliseconds) else {
            throw RelaySessionError.invalidTimestamp
        }
        guard serverProof.count == RelayCrypto.sha256Length else {
            throw RelaySessionError.invalidProof
        }

        self.sessionID = sessionID
        self.expiresAtUnixMilliseconds = expiresAtUnixMilliseconds
        self.serverProof = serverProof
    }

    public var expirationDate: Date {
        RelayTime.date(fromUnixMilliseconds: expiresAtUnixMilliseconds)
    }

    private enum CodingKeys: String, CodingKey {
        case sessionID
        case expiresAtUnixMilliseconds
        case serverProof
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(
            sessionID: container.decode(RelaySessionIdentifier.self, forKey: .sessionID),
            expiresAtUnixMilliseconds: container.decode(Int64.self, forKey: .expiresAtUnixMilliseconds),
            serverProof: container.decode(Data.self, forKey: .serverProof)
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
    public let acceptance: RelayPairingAcceptance
    public let session: RelayEstablishedSession
}

public actor RelayServerPairingContext {
    public nonisolated let challenge: RelaySessionChallenge
    public nonisolated let pairingCode: RelayPairingCode

    private let serverPrivateKeyData: Data
    private let sessionTTLMilliseconds: Int64
    private let maximumFailedAttempts: Int
    private var failedAttempts = 0
    private var completed = false

    public init(
        sessionID: RelaySessionIdentifier = .random(),
        pairingCode: RelayPairingCode = .random(),
        serverPrivateKeyData: Data? = nil,
        serverNonce: Data? = nil,
        now: Date = Date(),
        challengeTTL: TimeInterval = 120,
        sessionTTL: TimeInterval = 7_200,
        maximumFailedAttempts: Int = 5
    ) throws {
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now),
              let challengeTTLMilliseconds = RelayTime.milliseconds(
                  from: challengeTTL,
                  maximum: RelayLimits.maximumChallengeTTLMilliseconds
              ), challengeTTLMilliseconds > 0,
              let sessionTTLMilliseconds = RelayTime.milliseconds(
                  from: sessionTTL,
                  maximum: RelayLimits.maximumSessionTTLMilliseconds
              ), sessionTTLMilliseconds > 0,
              (1 ... RelayLimits.maximumPairingAttempts).contains(maximumFailedAttempts),
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

        self.challenge = try RelaySessionChallenge(
            sessionID: sessionID,
            serverPublicKey: privateKey.publicKey.rawRepresentation,
            serverNonce: serverNonce ?? RelayCrypto.randomBytes(count: RelayCrypto.nonceLength),
            expiresAtUnixMilliseconds: challengeExpiration
        )
        self.pairingCode = pairingCode
        self.serverPrivateKeyData = privateKeyData
        self.sessionTTLMilliseconds = sessionTTLMilliseconds
        self.maximumFailedAttempts = maximumFailedAttempts
    }

    public func accept(_ request: RelayPairingRequest, now: Date) throws -> RelayServerPairingResult {
        guard !completed else {
            throw RelaySessionError.pairingAlreadyCompleted
        }
        guard failedAttempts < maximumFailedAttempts else {
            throw RelaySessionError.pairingAttemptsExhausted
        }
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now) else {
            throw RelaySessionError.invalidTimestamp
        }
        guard nowMilliseconds <= challenge.expiresAtUnixMilliseconds else {
            throw RelaySessionError.expiredChallenge
        }
        guard request.sessionID == challenge.sessionID else {
            throw RelaySessionError.invalidRequest
        }

        let expectedPairingProof = RelayCrypto.pairingProof(
            pairingCode: pairingCode,
            challenge: challenge,
            clientPublicKey: request.clientPublicKey,
            clientNonce: request.clientNonce
        )
        guard RelayCrypto.constantTimeEqual(expectedPairingProof, request.pairingProof) else {
            recordFailedAttempt()
            if failedAttempts >= maximumFailedAttempts {
                throw RelaySessionError.pairingAttemptsExhausted
            }
            throw RelaySessionError.pairingProofMismatch
        }

        let keyMaterial: RelaySessionKeyMaterial
        do {
            keyMaterial = try RelayCrypto.derivedKeyMaterial(
                sessionID: challenge.sessionID,
                ownPrivateKeyData: serverPrivateKeyData,
                peerPublicKeyData: request.clientPublicKey,
                transcript: RelayCanonical.pairingTranscript(challenge: challenge, request: request)
            )
        } catch {
            recordFailedAttempt()
            throw error
        }
        guard let sessionExpiration = RelayTime.adding(sessionTTLMilliseconds, to: nowMilliseconds) else {
            throw RelaySessionError.invalidTimestamp
        }

        let serverProof = RelayCrypto.acceptanceProof(
            keyMaterial: keyMaterial,
            challenge: challenge,
            request: request,
            sessionExpirationUnixMilliseconds: sessionExpiration
        )
        let acceptance = try RelayPairingAcceptance(
            sessionID: challenge.sessionID,
            expiresAtUnixMilliseconds: sessionExpiration,
            serverProof: serverProof
        )
        completed = true
        return RelayServerPairingResult(
            acceptance: acceptance,
            session: RelayEstablishedSession(
                sessionID: challenge.sessionID,
                role: .server,
                expiresAtUnixMilliseconds: sessionExpiration,
                keyMaterial: keyMaterial
            )
        )
    }

    public func remainingFailedAttempts() -> Int {
        maximumFailedAttempts - failedAttempts
    }

    private func recordFailedAttempt() {
        if failedAttempts < maximumFailedAttempts {
            failedAttempts += 1
        }
    }
}

public struct RelayClientPairingAttempt: Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let request: RelayPairingRequest
    private let challenge: RelaySessionChallenge
    private let pendingKeyMaterial: RelaySessionKeyMaterial

    public init(
        challenge: RelaySessionChallenge,
        pairingCode: RelayPairingCode,
        clientPrivateKeyData: Data,
        clientNonce: Data,
        now: Date
    ) throws {
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now) else {
            throw RelaySessionError.invalidTimestamp
        }
        guard nowMilliseconds <= challenge.expiresAtUnixMilliseconds else {
            throw RelaySessionError.expiredChallenge
        }
        guard clientPrivateKeyData.count == RelayCrypto.curve25519KeyLength else {
            throw RelaySessionError.invalidPublicKey
        }

        let privateKey: Curve25519.KeyAgreement.PrivateKey
        do {
            privateKey = try Curve25519.KeyAgreement.PrivateKey(rawRepresentation: clientPrivateKeyData)
        } catch {
            throw RelaySessionError.invalidPublicKey
        }

        let request = try RelayPairingRequest(
            sessionID: challenge.sessionID,
            clientPublicKey: privateKey.publicKey.rawRepresentation,
            clientNonce: clientNonce,
            pairingProof: RelayCrypto.pairingProof(
                pairingCode: pairingCode,
                challenge: challenge,
                clientPublicKey: privateKey.publicKey.rawRepresentation,
                clientNonce: clientNonce
            )
        )
        self.challenge = challenge
        self.request = request
        pendingKeyMaterial = try RelayCrypto.derivedKeyMaterial(
            sessionID: challenge.sessionID,
            ownPrivateKeyData: clientPrivateKeyData,
            peerPublicKeyData: challenge.serverPublicKey,
            transcript: RelayCanonical.pairingTranscript(challenge: challenge, request: request)
        )
    }

    public init(challenge: RelaySessionChallenge, pairingCode: RelayPairingCode, now: Date) throws {
        let privateKey = Curve25519.KeyAgreement.PrivateKey()
        try self.init(
            challenge: challenge,
            pairingCode: pairingCode,
            clientPrivateKeyData: privateKey.rawRepresentation,
            clientNonce: RelayCrypto.randomBytes(count: RelayCrypto.nonceLength),
            now: now
        )
    }

    public func complete(with acceptance: RelayPairingAcceptance, now: Date) throws -> RelayEstablishedSession {
        guard let nowMilliseconds = RelayTime.unixMilliseconds(for: now) else {
            throw RelaySessionError.invalidTimestamp
        }
        guard nowMilliseconds <= challenge.expiresAtUnixMilliseconds else {
            throw RelaySessionError.expiredChallenge
        }
        let sessionLifetime = acceptance.expiresAtUnixMilliseconds.subtractingReportingOverflow(nowMilliseconds)
        guard acceptance.sessionID == challenge.sessionID,
              !sessionLifetime.overflow,
              (0 ... RelayLimits.maximumSessionTTLMilliseconds).contains(sessionLifetime.partialValue)
        else {
            throw RelaySessionError.invalidRequest
        }

        let expectedServerProof = RelayCrypto.acceptanceProof(
            keyMaterial: pendingKeyMaterial,
            challenge: challenge,
            request: request,
            sessionExpirationUnixMilliseconds: acceptance.expiresAtUnixMilliseconds
        )
        guard RelayCrypto.constantTimeEqual(expectedServerProof, acceptance.serverProof) else {
            throw RelaySessionError.acceptanceProofMismatch
        }
        return RelayEstablishedSession(
            sessionID: challenge.sessionID,
            role: .client,
            expiresAtUnixMilliseconds: acceptance.expiresAtUnixMilliseconds,
            keyMaterial: pendingKeyMaterial
        )
    }

    public var description: String {
        "RelayClientPairingAttempt(sessionID: \(request.sessionID.rawValue), keyMaterial: redacted)"
    }

    public var debugDescription: String {
        description
    }
}

public struct RelayEstablishedSession: Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let sessionID: RelaySessionIdentifier
    public let role: RelaySessionRole
    public let expiresAtUnixMilliseconds: Int64
    public let sessionIdentity: RelaySessionIdentity
    public let mediaCapability: RelayMediaCapability

    private let requestSigningKey: Data
    private let peerRequestVerificationKey: Data

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
}

struct RelaySessionKeyMaterial: Sendable {
    let sessionIdentity: Data
    let clientToServerRequestKey: Data
    let serverToClientRequestKey: Data
    let mediaCapability: Data
    let acceptanceProofKey: Data
}

enum RelayCrypto {
    static let curve25519KeyLength = 32
    static let nonceLength = 32
    static let sha256Length = 32

    static func randomBytes(count: Int) -> Data {
        precondition((1 ... sha256Length).contains(count))
        return SymmetricKey(size: .bits256).withUnsafeBytes { Data($0.prefix(count)) }
    }

    static func pairingProof(
        pairingCode: RelayPairingCode,
        challenge: RelaySessionChallenge,
        clientPublicKey: Data,
        clientNonce: Data
    ) -> Data {
        let pairingKey = HKDF<SHA256>.deriveKey(
            inputKeyMaterial: SymmetricKey(data: Data(pairingCode.rawValue.utf8)),
            salt: Data("bd-to-avp.relay.pairing-code.salt.v2".utf8),
            info: Data("bd-to-avp.relay.pairing-code.key.v2".utf8),
            outputByteCount: sha256Length
        )
        return Data(HMAC<SHA256>.authenticationCode(
            for: RelayCanonical.pairingProofTranscript(
                challenge: challenge,
                clientPublicKey: clientPublicKey,
                clientNonce: clientNonce
            ),
            using: pairingKey
        ))
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
        let masterKey = sharedSecret.hkdfDerivedSymmetricKey(
            using: SHA256.self,
            salt: sha256(transcript),
            sharedInfo: Data("bd-to-avp.relay.session-master.v2".utf8),
            outputByteCount: sha256Length
        ).withUnsafeBytes { Data($0) }

        return RelaySessionKeyMaterial(
            sessionIdentity: derivedValue(masterKey: masterKey, label: "session-identity", sessionID: sessionID),
            clientToServerRequestKey: derivedValue(masterKey: masterKey, label: "request-client-to-server", sessionID: sessionID),
            serverToClientRequestKey: derivedValue(masterKey: masterKey, label: "request-server-to-client", sessionID: sessionID),
            mediaCapability: derivedValue(masterKey: masterKey, label: "media-capability", sessionID: sessionID),
            acceptanceProofKey: derivedValue(masterKey: masterKey, label: "acceptance-proof", sessionID: sessionID)
        )
    }

    static func acceptanceProof(
        keyMaterial: RelaySessionKeyMaterial,
        challenge: RelaySessionChallenge,
        request: RelayPairingRequest,
        sessionExpirationUnixMilliseconds: Int64
    ) -> Data {
        hmac(
            keyMaterial: keyMaterial.acceptanceProofKey,
            message: RelayCanonical.acceptanceTranscript(
                challenge: challenge,
                request: request,
                sessionExpirationUnixMilliseconds: sessionExpirationUnixMilliseconds
            )
        )
    }

    static func hmac(keyMaterial: Data, message: Data) -> Data {
        Data(HMAC<SHA256>.authenticationCode(for: message, using: SymmetricKey(data: keyMaterial)))
    }

    static func sha256(_ data: Data) -> Data {
        Data(SHA256.hash(data: data))
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
    static func pairingProofTranscript(
        challenge: RelaySessionChallenge,
        clientPublicKey: Data,
        clientNonce: Data
    ) -> Data {
        transcript(
            domain: "bd-to-avp.relay.pairing-proof.v2",
            fields: [
                ("challenge", challengeTranscript(challenge)),
                ("clientPublicKey", clientPublicKey),
                ("clientNonce", clientNonce),
            ]
        )
    }

    static func pairingTranscript(challenge: RelaySessionChallenge, request: RelayPairingRequest) -> Data {
        pairingProofTranscript(
            challenge: challenge,
            clientPublicKey: request.clientPublicKey,
            clientNonce: request.clientNonce
        )
    }

    static func acceptanceTranscript(
        challenge: RelaySessionChallenge,
        request: RelayPairingRequest,
        sessionExpirationUnixMilliseconds: Int64
    ) -> Data {
        transcript(
            domain: "bd-to-avp.relay.pairing-acceptance.v2",
            fields: [
                ("pairingTranscript", pairingTranscript(challenge: challenge, request: request)),
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

    static func keyDerivationTranscript(label: String, sessionID: RelaySessionIdentifier) -> Data {
        transcript(
            domain: "bd-to-avp.relay.key-derivation.v2",
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

    private static func challengeTranscript(_ challenge: RelaySessionChallenge) -> Data {
        transcript(
            domain: "bd-to-avp.relay.challenge.v2",
            fields: [
                ("sessionID", Data(challenge.sessionID.rawValue.utf8)),
                ("serverPublicKey", challenge.serverPublicKey),
                ("serverNonce", challenge.serverNonce),
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
    static let maximumSessionTTLMilliseconds: Int64 = 24 * 60 * 60 * 1_000
    static let maximumRequestAgeMilliseconds: Int64 = 5 * 60 * 1_000
    static let maximumFutureSkewMilliseconds: Int64 = 60 * 1_000
    static let maximumReplayCapacity = 4_096
    static let maximumPairingAttempts = 5
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

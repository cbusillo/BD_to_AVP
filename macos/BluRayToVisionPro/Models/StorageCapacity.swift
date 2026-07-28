import Foundation

enum StorageCapacityState: String, Codable, Sendable {
    case known
    case unknown
    case conflicting
}

enum StorageCapacitySufficiency: String, Codable, Sendable {
    case sufficient
    case insufficient
    case unknown
}

enum StorageCapacityProvenance: String, Codable, CaseIterable, Hashable, Sendable {
    case swiftImportantUsage = "swift_url_resource_important_usage"
    case swiftAvailable = "swift_url_resource_available"
    case workerStatVFS = "worker_posix_statvfs"
}

enum StorageCapacityUnknownReason: String, Codable, Sendable {
    case unavailable
    case invalidReading = "invalid_reading"
    case zeroOnWritableVolume = "zero_on_writable_volume"
    case permissionDenied = "permission_denied"
    case notMounted = "not_mounted"
    case timeout
}

struct StorageCapacityEvidence: Equatable, Sendable {
    let provenance: StorageCapacityProvenance
    let availableBytes: Int64?
    let totalBytes: Int64?
    let writable: Bool?
    let readOnly: Bool?
    let unknownReason: StorageCapacityUnknownReason?
}

struct StorageCapacityResult: Equatable, Sendable {
    static let publicCapacityQuantumBytes: Int64 = 16 * 1_024 * 1_024 * 1_024

    let state: StorageCapacityState
    let sufficiency: StorageCapacitySufficiency
    let requiredBytes: Int64?
    let availableBytes: Int64?
    let lowerAvailableBytes: Int64?
    let upperAvailableBytes: Int64?
    let provenance: [StorageCapacityProvenance]
    let unknownReason: StorageCapacityUnknownReason?

    var isKnownInsufficient: Bool {
        state == .known && sufficiency == .insufficient
    }

    static func evaluate(
        requiredBytes: Int64?,
        evidence: [StorageCapacityEvidence]
    ) -> StorageCapacityResult {
        let provenance = Array(Set(evidence.map(\.provenance))).sorted { $0.rawValue < $1.rawValue }
        let suspiciousZero = evidence.contains { item in
            item.availableBytes == 0 && item.readOnly != true
        }
        let validAvailable = evidence.compactMap { item -> Int64? in
            guard let value = item.availableBytes, value >= 0 else {
                return nil
            }
            if value == 0, item.readOnly != true {
                return nil
            }
            return value
        }
        let lower = validAvailable.min()
        let upper = validAvailable.max()
        let decisionConflict: Bool
        if let requiredBytes, let lower, let upper {
            decisionConflict = lower < requiredBytes && upper >= requiredBytes
        } else {
            decisionConflict = false
        }
        let scaleConflict: Bool
        if let lower, let upper {
            scaleConflict = provenance.contains(.workerStatVFS)
                && lower < publicCapacityQuantumBytes
                && upper >= publicCapacityQuantumBytes * 2
        } else {
            scaleConflict = false
        }

        if decisionConflict || scaleConflict || (suspiciousZero && !validAvailable.isEmpty) {
            return StorageCapacityResult(
                state: .conflicting,
                sufficiency: .unknown,
                requiredBytes: requiredBytes,
                availableBytes: nil,
                lowerAvailableBytes: lower,
                upperAvailableBytes: upper,
                provenance: provenance,
                unknownReason: nil
            )
        }

        guard let availableBytes = lower else {
            let reason = suspiciousZero
                ? .zeroOnWritableVolume
                : evidence.compactMap(\.unknownReason).first ?? .unavailable
            return StorageCapacityResult(
                state: .unknown,
                sufficiency: .unknown,
                requiredBytes: requiredBytes,
                availableBytes: nil,
                lowerAvailableBytes: nil,
                upperAvailableBytes: nil,
                provenance: provenance,
                unknownReason: reason
            )
        }

        let sufficiency: StorageCapacitySufficiency
        if let requiredBytes {
            sufficiency = availableBytes < requiredBytes ? .insufficient : .sufficient
        } else {
            sufficiency = .unknown
        }
        return StorageCapacityResult(
            state: .known,
            sufficiency: sufficiency,
            requiredBytes: requiredBytes,
            availableBytes: availableBytes,
            lowerAvailableBytes: availableBytes,
            upperAvailableBytes: upper,
            provenance: provenance,
            unknownReason: nil
        )
    }
}

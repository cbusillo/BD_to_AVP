import Foundation

struct SourceInspection: Codable, Equatable {
    let name: String
    let resolution: String
    let frameRate: String
    let interlaced: Bool
    let sizeBytes: Int64?
    let durationSeconds: Double?

    init(
        name: String,
        resolution: String,
        frameRate: String,
        interlaced: Bool,
        sizeBytes: Int64? = nil,
        durationSeconds: Double? = nil
    ) {
        self.name = name
        self.resolution = resolution
        self.frameRate = frameRate
        self.interlaced = interlaced
        self.sizeBytes = sizeBytes
        self.durationSeconds = durationSeconds
    }

    enum CodingKeys: String, CodingKey {
        case name
        case resolution
        case frameRate = "frame_rate"
        case interlaced
        case sizeBytes = "size_bytes"
        case durationSeconds = "duration_seconds"
    }

    var formattedSize: String {
        guard let sizeBytes else {
            return "Not reported"
        }
        return ByteCountFormatter.string(fromByteCount: sizeBytes, countStyle: .file)
    }

    var scanDescription: String {
        interlaced ? "Interlaced" : "Progressive"
    }

    var formattedDuration: String {
        guard let durationSeconds, durationSeconds > 0 else {
            return "Not reported"
        }
        return Duration.seconds(durationSeconds).formatted(
            .time(pattern: .hourMinuteSecond(padHourToLength: 2))
        )
    }
}

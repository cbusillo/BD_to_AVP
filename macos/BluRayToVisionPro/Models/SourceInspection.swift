import Foundation

struct SourceInspection: Codable, Equatable {
    let name: String
    let resolution: String
    let frameRate: String
    let interlaced: Bool
    let sizeBytes: Int64?

    enum CodingKeys: String, CodingKey {
        case name
        case resolution
        case frameRate = "frame_rate"
        case interlaced
        case sizeBytes = "size_bytes"
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
}

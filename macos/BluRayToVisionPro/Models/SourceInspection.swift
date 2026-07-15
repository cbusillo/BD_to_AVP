import Foundation

struct SourceTitle: Codable, Equatable, Identifiable {
    let id: String
    let name: String
    let outputName: String
    let durationSeconds: Double
    let resolution: String
    let frameRate: String
    let mainFeature: Bool

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case outputName = "output_name"
        case durationSeconds = "duration_seconds"
        case resolution
        case frameRate = "frame_rate"
        case mainFeature = "main_feature"
    }

    var formattedDuration: String {
        guard durationSeconds > 0 else {
            return "Not reported"
        }
        return Duration.seconds(durationSeconds).formatted(
            .time(pattern: .hourMinuteSecond(padHourToLength: 2))
        )
    }
}

enum DiscTitleSelection: Equatable {
    case main
    case all
    case custom(Set<String>)

    func resolvedTitles(in inspection: SourceInspection) -> [SourceTitle] {
        switch self {
        case .main:
            return inspection.mainTitle.map { [$0] } ?? []
        case .all:
            return inspection.titles
        case .custom(let identifiers):
            return inspection.titles.filter { identifiers.contains($0.id) }
        }
    }

    var isMain: Bool {
        if case .main = self { return true }
        return false
    }

    var isAll: Bool {
        if case .all = self { return true }
        return false
    }

    var isCustom: Bool {
        if case .custom = self { return true }
        return false
    }
}

struct SourceInspection: Codable, Equatable {
    let name: String
    let resolution: String
    let frameRate: String
    let interlaced: Bool
    let sizeBytes: Int64?
    let durationSeconds: Double?
    let titles: [SourceTitle]

    init(
        name: String,
        resolution: String,
        frameRate: String,
        interlaced: Bool,
        sizeBytes: Int64? = nil,
        durationSeconds: Double? = nil,
        titles: [SourceTitle] = []
    ) {
        self.name = name
        self.resolution = resolution
        self.frameRate = frameRate
        self.interlaced = interlaced
        self.sizeBytes = sizeBytes
        self.durationSeconds = durationSeconds
        self.titles = titles
    }

    enum CodingKeys: String, CodingKey {
        case name
        case resolution
        case frameRate = "frame_rate"
        case interlaced
        case sizeBytes = "size_bytes"
        case durationSeconds = "duration_seconds"
        case titles
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = try container.decode(String.self, forKey: .name)
        resolution = try container.decode(String.self, forKey: .resolution)
        frameRate = try container.decode(String.self, forKey: .frameRate)
        interlaced = try container.decode(Bool.self, forKey: .interlaced)
        sizeBytes = try container.decodeIfPresent(Int64.self, forKey: .sizeBytes)
        durationSeconds = try container.decodeIfPresent(Double.self, forKey: .durationSeconds)
        titles = try container.decodeIfPresent([SourceTitle].self, forKey: .titles) ?? []
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(name, forKey: .name)
        try container.encode(resolution, forKey: .resolution)
        try container.encode(frameRate, forKey: .frameRate)
        try container.encode(interlaced, forKey: .interlaced)
        try container.encodeIfPresent(sizeBytes, forKey: .sizeBytes)
        try container.encodeIfPresent(durationSeconds, forKey: .durationSeconds)
        if !titles.isEmpty {
            try container.encode(titles, forKey: .titles)
        }
    }

    var mainTitle: SourceTitle? {
        titles.first(where: \.mainFeature) ?? titles.first
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

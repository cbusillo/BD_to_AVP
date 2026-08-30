import Foundation

enum StereoFormat: String, Codable, CaseIterable, Hashable, Sendable {
    case mvHEVC = "mv-hevc"
    case sideBySide = "side-by-side"
    case overUnder = "over-under"
    case unsupported

    static var sbs: StereoFormat { .sideBySide }

    var displayName: String {
        switch self {
        case .mvHEVC:
            return "MV-HEVC"
        case .sideBySide:
            return "SBS"
        case .overUnder:
            return "OVER-UNDER"
        case .unsupported:
            return "UNSUPPORTED"
        }
    }
}

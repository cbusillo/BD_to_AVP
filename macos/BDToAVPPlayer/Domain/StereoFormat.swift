import Foundation

enum StereoFormat: String, Codable, CaseIterable, Hashable, Sendable {
    case mvHEVC = "mv-hevc"
    case sideBySide = "side-by-side"
    case overUnder = "over-under"
    /// A Blu-ray 3D ISO, disc folder, or MVC Matroska file, decoded on the headset.
    case bluray3D = "blu-ray-3d"
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
        case .bluray3D:
            return "BLU-RAY 3D"
        case .unsupported:
            return "UNSUPPORTED"
        }
    }
}

import Foundation

enum Bluray3DSourceOpener {
    enum Kind: Equatable {
        case discImage
        case discFolder(root: URL)
        case matroska
    }

    static let fileExtensions: Set<String> = ["iso", "mkv"]

    /// What a picked URL is. A folder qualifies when it is a disc root that holds BDMV, or the
    /// BDMV folder itself.
    static func kind(of url: URL, fileManager: FileManager = .default) -> Kind? {
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory) else { return nil }
        if isDirectory.boolValue {
            if fileManager.fileExists(atPath: url.appendingPathComponent("BDMV/index.bdmv").path) {
                return .discFolder(root: url)
            }
            if url.lastPathComponent.uppercased() == "BDMV",
               fileManager.fileExists(atPath: url.appendingPathComponent("index.bdmv").path) {
                return .discFolder(root: url.deletingLastPathComponent())
            }
            return nil
        }
        switch url.pathExtension.lowercased() {
        case "iso": return .discImage
        case "mkv": return .matroska
        default: return nil
        }
    }

    /// Opens a source. Call it off the main thread: it reads the disc's navigation files. With
    /// `reads`, the title's stream reads retry through a dropped share connection.
    static func open(_ url: URL, reads: ShareConnectionMonitor? = nil) throws -> Bluray3DSource {
        switch kind(of: url) {
        case .discImage:
            return try BlurayDiscSource(path: url.path, container: .discImage, reads: reads)
        case let .discFolder(root):
            return try BlurayDiscSource(path: root.path, container: .discFolder, reads: reads)
        case .matroska:
            return try MatroskaMVCSource(url: url, reads: reads)
        case nil:
            throw Bluray3DSourceError.unrecognized
        }
    }
}

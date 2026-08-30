import Foundation

struct MediaItem: Codable, Equatable, Hashable, Identifiable, Sendable {
    let id: String
    let title: String
    let fileName: String
    let format: StereoFormat

    init(id: String, title: String, fileName: String, format: StereoFormat) {
        self.id = id
        self.title = title
        self.fileName = fileName
        self.format = format
    }

    init(id: String = UUID().uuidString, url: URL, format: StereoFormat, title: String? = nil) {
        let fileName = url.lastPathComponent
        self.init(
            id: id,
            title: title ?? url.deletingPathExtension().lastPathComponent,
            fileName: fileName,
            format: format
        )
    }
}

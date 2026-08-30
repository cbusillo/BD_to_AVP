import Foundation

struct MediaItem: Codable, Equatable, Hashable, Identifiable, Sendable {
    let id: String
    let title: String
    let fileName: String
    let sourceURL: URL
    let format: StereoFormat

    init(id: String, title: String, fileName: String, sourceURL: URL, format: StereoFormat) {
        self.id = id
        self.title = title
        self.fileName = fileName
        self.sourceURL = sourceURL
        self.format = format
    }

    init(url: URL, format: StereoFormat, title: String? = nil) {
        let fileName = url.lastPathComponent
        self.init(
            id: url.standardizedFileURL.absoluteString,
            title: title ?? url.deletingPathExtension().lastPathComponent,
            fileName: fileName,
            sourceURL: url,
            format: format
        )
    }
}

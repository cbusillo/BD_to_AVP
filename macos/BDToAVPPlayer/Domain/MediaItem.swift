import Foundation

struct MediaItem: Codable, Equatable, Hashable, Identifiable, Sendable {
    let id: String
    let title: String
    let fileName: String
    let format: StereoFormat
    /// The title a Blu-ray 3D source plays, chosen when it was added.
    let bluray3DTitle: Bluray3DTitle?

    init(id: String, title: String, fileName: String, format: StereoFormat, bluray3DTitle: Bluray3DTitle? = nil) {
        self.id = id
        self.title = title
        self.fileName = fileName
        self.format = format
        self.bluray3DTitle = bluray3DTitle
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

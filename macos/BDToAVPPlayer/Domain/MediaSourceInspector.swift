import Foundation

/// What a source file or disc folder holds: its format, and for Blu-ray 3D the title it plays.
struct MediaSourceInspection: Codable, Equatable, Sendable {
    /// Bumped when the readers learn to play sources they refused before, so Locations inspect
    /// their files again instead of keeping an old answer. 2: titles made of several clips.
    static let currentReaderVersion = 2

    let format: StereoFormat
    let bluray3DTitle: Bluray3DTitle?
    /// The reader version that made this inspection; nil before versions were recorded.
    let readerVersion: Int?

    init(format: StereoFormat, bluray3DTitle: Bluray3DTitle?, readerVersion: Int? = Self.currentReaderVersion) {
        self.format = format
        self.bluray3DTitle = bluray3DTitle
        self.readerVersion = readerVersion
    }

    /// Why the player cannot play this source, or nil when it can.
    var unplayableReason: String? {
        switch format {
        case .mvHEVC, .sideBySide, .overUnder:
            return nil
        case .bluray3D:
            guard let bluray3DTitle else { return Bluray3DUnsupportedReason.notStereo3D.message }
            return bluray3DTitle.unsupportedReason?.message
        case .unsupported:
            return "Not a 3D format this player supports."
        }
    }
}

/// Inspects sources for the local library and for Locations, so both decide formats the same way.
struct MediaSourceInspector: Sendable {
    typealias FormatInspector = @Sendable (URL) async throws -> StereoFormat
    typealias BlurayInspector = @Sendable (URL) async throws -> Bluray3DSourceInfo

    let formatInspector: FormatInspector
    let blurayInspector: BlurayInspector

    init(
        formatInspector: @escaping FormatInspector = { try await MediaFormatInspector.inspect(url: $0) },
        blurayInspector: @escaping BlurayInspector = { url in
            try await Task.detached(priority: .userInitiated) { try Bluray3DSourceOpener.open(url).info }.value
        }
    ) {
        self.formatInspector = formatInspector
        self.blurayInspector = blurayInspector
    }

    func inspect(_ url: URL) async throws -> MediaSourceInspection {
        guard Bluray3DSourceOpener.kind(of: url) != nil else {
            return MediaSourceInspection(format: try await formatInspector(url), bluray3DTitle: nil)
        }
        let info = try await blurayInspector(url)
        guard let title = info.mainTitle ?? info.titles.first else {
            throw Bluray3DSourceError.titleUnavailable
        }
        return MediaSourceInspection(format: .bluray3D, bluray3DTitle: title)
    }
}

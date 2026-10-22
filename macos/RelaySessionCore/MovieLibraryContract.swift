import CryptoKit
import Foundation

enum MovieLibraryContract {
    static let version = 1
    static let serviceType = "_bdtoavp-movies._tcp"
    static let challengePath = "/movies/v1/challenge"
    static let pairingPath = "/movies/v1/pairing"
    static let confirmationPath = "/movies/v1/confirm"
    static let catalogPath = "/movies/v1/catalog"
    static let forgetPath = "/movies/v1/forget"
    static let bytesPrefix = "/movies/v1/bytes/"
    static let maximumChunkBytes = 1_048_576
    static let maximumMovies = 500
    static let maximumRoots = 8
    static let maximumPeers = 16
    static let supportedExtensions: Set<String> = ["mov", "mp4", "m4v"]

    // Live playback of Blu-ray 3D ISOs. Additive to catalog version 1: older clients ignore
    // `liveTitles`, and ISOs never appear in `movies`.
    static let liveVersion = 1
    static let liveExtensions: Set<String> = ["iso"]
    static let maximumLiveTitles = 64
    static let maximumLiveAudio = 32
    static let maximumCatalogBytes = 1_048_576
    static let liveStartPath = "/movies/v1/live/start"
    static let liveCancelPath = "/movies/v1/live/cancel"
    static let liveReleasePath = "/movies/v1/live/release"
    static let liveStatusPrefix = "/movies/v1/live/status/"

    static func digest(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    static func isDigest(_ value: String) -> Bool {
        value.utf8.count == 64 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }
}

enum MovieLibraryError: Error, LocalizedError, Equatable {
    case unavailable, incompatible, needsPairing, invalidResponse, sourceChanged, invalidRange, revoked
    case credentialStore(Int32)

    var errorDescription: String? {
        switch self {
        case .unavailable: "The Mac is unavailable. Keep its app open with movie sharing enabled, then try again."
        case .incompatible: "Update both apps to use this movie library."
        case .needsPairing: "Connect to the Mac and confirm the pairing code again."
        case .invalidResponse: "The Mac's response could not be verified. Reconnect before trying again."
        case .sourceChanged: "The movie changed or is no longer available on the Mac. Refresh the library and choose it again."
        case .invalidRange: "The requested part of the movie is unavailable."
        case .revoked: "Access to this Mac was removed. Pair again to restore access."
        case .credentialStore: "Saved pairing could not be accessed. Unlock the device or keychain, then try again."
        }
    }
}

struct MovieLibraryChallenge: Codable, Sendable {
    let version: Int
    let challenge: RelaySessionChallenge
    init(challenge: RelaySessionChallenge) {
        version = MovieLibraryContract.version
        self.challenge = challenge
    }
}

struct MoviePairingRequest: Codable, Sendable {
    let request: RelayPairingRequest
    let name: String
    func validate() throws {
        guard !name.isEmpty, name.utf8.count <= 512,
              !name.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
        else { throw MovieLibraryError.invalidResponse }
    }
}

struct SharedMovie: Codable, Identifiable, Equatable, Sendable {
    let id: String
    let rootID: String
    let fileName: String
    let byteCount: Int64
    let revision: String

    var title: String { (fileName as NSString).deletingPathExtension }
    var resumeID: String { "shared:\(id):\(revision)" }

    func validate() throws {
        guard MovieLibraryContract.isDigest(id), MovieLibraryContract.isDigest(revision),
              UUID(uuidString: rootID) != nil, byteCount > 0,
              fileName.utf8.count <= 512, !fileName.isEmpty,
              !fileName.contains("/"), !fileName.contains("\\"),
              !fileName.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains),
              MovieLibraryContract.supportedExtensions.contains((fileName as NSString).pathExtension.lowercased())
        else { throw MovieLibraryError.invalidResponse }
    }
}

struct SharedMovieRoot: Codable, Identifiable, Equatable, Sendable {
    let id: String
    let name: String
    let isAvailable: Bool
}

struct SharedLiveAudio: Codable, Equatable, Sendable {
    let pid: Int
    let language: String?
    let label: String
    let isDefault: Bool

    func validate() throws {
        guard (1...8_191).contains(pid), !label.isEmpty, label.utf8.count <= 128,
              !label.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains),
              language.map({ $0.utf8.count <= 16 && $0.unicodeScalars.allSatisfy { CharacterSet.alphanumerics.contains($0) } }) ?? true
        else { throw MovieLibraryError.invalidResponse }
    }
}

/// A Blu-ray 3D ISO the Mac can stream live. Clients skip a title whose `liveVersion` they do
/// not know. `state` is `checking` until the Mac's background inspection finishes.
struct SharedLiveTitle: Codable, Identifiable, Equatable, Sendable {
    enum State: String, Codable, Sendable {
        case checking, eligible, ineligible
    }

    let liveVersion: Int
    let id: String
    let rootID: String
    let fileName: String
    let revision: String
    let state: State
    let reason: String?
    let durationSeconds: Double?
    let audio: [SharedLiveAudio]

    var title: String { (fileName as NSString).deletingPathExtension }
    var defaultAudio: SharedLiveAudio? { audio.first(where: \.isDefault) ?? audio.first }

    /// The track to start with: the first track in the viewer's most preferred language that the
    /// title has, else the Mac's default. `preferredLanguages` are BCP 47 identifiers such as
    /// `Locale.preferredLanguages`; track languages are ISO 639 codes as the disc gives them.
    func startingAudio(preferredLanguages: [String]) -> SharedLiveAudio? {
        for preferred in preferredLanguages {
            guard let wanted = Self.languageKey(preferred) else { continue }
            if let match = audio.first(where: { $0.language.flatMap(Self.languageKey) == wanted }) {
                return match
            }
        }
        return defaultAudio
    }

    /// ISO 639-2 bibliographic codes Foundation does not resolve, mapped to their terminology form.
    private static let bibliographicLanguageCodes = [
        "alb": "sqi", "arm": "hye", "baq": "eus", "bur": "mya", "chi": "zho", "cze": "ces",
        "dut": "nld", "fre": "fra", "geo": "kat", "ger": "deu", "gre": "ell", "ice": "isl",
        "mac": "mkd", "mao": "mri", "may": "msa", "per": "fas", "rum": "ron", "slo": "slk",
        "tib": "bod", "wel": "cym",
    ]

    private static func languageKey(_ identifier: String) -> String? {
        let lowered = identifier.lowercased()
        let code = bibliographicLanguageCodes[lowered] ?? lowered
        guard let language = Locale.Language(identifier: code).languageCode,
              language != .unavailable, language.identifier != "und", language.identifier != "mul"
        else { return nil }
        return language.identifier(.alpha3) ?? language.identifier
    }

    init(
        id: String, rootID: String, fileName: String, revision: String, state: State,
        reason: String? = nil, durationSeconds: Double? = nil, audio: [SharedLiveAudio] = []
    ) {
        liveVersion = MovieLibraryContract.liveVersion
        self.id = id
        self.rootID = rootID
        self.fileName = fileName
        self.revision = revision
        self.state = state
        self.reason = reason
        self.durationSeconds = durationSeconds
        self.audio = audio
    }

    func validate() throws {
        guard liveVersion == MovieLibraryContract.liveVersion,
              MovieLibraryContract.isDigest(id), MovieLibraryContract.isDigest(revision),
              UUID(uuidString: rootID) != nil,
              fileName.utf8.count <= 512, !fileName.isEmpty,
              !fileName.contains("/"), !fileName.contains("\\"),
              !fileName.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains),
              MovieLibraryContract.liveExtensions.contains((fileName as NSString).pathExtension.lowercased()),
              audio.count <= MovieLibraryContract.maximumLiveAudio,
              Set(audio.map(\.pid)).count == audio.count,
              reason.map({ $0.utf8.count <= 512 }) ?? true,
              durationSeconds.map({ $0.isFinite && $0 >= 0 }) ?? true,
              state != .eligible || !audio.isEmpty
        else { throw MovieLibraryError.invalidResponse }
        for track in audio { try track.validate() }
    }
}

struct SharedMovieCatalog: Codable, Equatable, Sendable {
    let version: Int
    let roots: [SharedMovieRoot]
    let movies: [SharedMovie]
    let truncated: Bool
    /// Encoded only when non-empty. A title with an unknown `liveVersion` is skipped on decode.
    let liveTitles: [SharedLiveTitle]

    init(roots: [SharedMovieRoot], movies: [SharedMovie], truncated: Bool = false, liveTitles: [SharedLiveTitle] = []) {
        version = MovieLibraryContract.version
        self.roots = roots
        self.movies = movies
        self.truncated = truncated
        self.liveTitles = liveTitles
    }

    private enum CodingKeys: String, CodingKey {
        case version, roots, movies, truncated, liveTitles
    }

    /// Reads only `liveVersion`, so a future title shape can be skipped without failing the catalog.
    private struct LiveVersionProbe: Decodable {
        let liveVersion: Int
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        version = try container.decode(Int.self, forKey: .version)
        roots = try container.decode([SharedMovieRoot].self, forKey: .roots)
        movies = try container.decode([SharedMovie].self, forKey: .movies)
        truncated = try container.decode(Bool.self, forKey: .truncated)
        var titles: [SharedLiveTitle] = []
        if container.contains(.liveTitles), try !container.decodeNil(forKey: .liveTitles) {
            var list = try container.nestedUnkeyedContainer(forKey: .liveTitles)
            var count = 0
            while !list.isAtEnd {
                count += 1
                guard count <= MovieLibraryContract.maximumLiveTitles else { throw MovieLibraryError.invalidResponse }
                let element = try list.superDecoder()
                if try LiveVersionProbe(from: element).liveVersion == MovieLibraryContract.liveVersion {
                    titles.append(try SharedLiveTitle(from: element))
                }
            }
        }
        liveTitles = titles
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(version, forKey: .version)
        try container.encode(roots, forKey: .roots)
        try container.encode(movies, forKey: .movies)
        try container.encode(truncated, forKey: .truncated)
        if !liveTitles.isEmpty { try container.encode(liveTitles, forKey: .liveTitles) }
    }

    func validate() throws {
        guard version == MovieLibraryContract.version else { throw MovieLibraryError.incompatible }
        guard roots.count <= MovieLibraryContract.maximumRoots,
              movies.count <= MovieLibraryContract.maximumMovies,
              liveTitles.count <= MovieLibraryContract.maximumLiveTitles,
              Set(roots.map(\.id)).count == roots.count,
              Set(movies.map(\.id)).count == movies.count,
              Set(liveTitles.map(\.id)).count == liveTitles.count,
              roots.allSatisfy({ UUID(uuidString: $0.id) != nil && !$0.name.isEmpty && $0.name.utf8.count <= 512 })
        else { throw MovieLibraryError.invalidResponse }
        let rootIDs = Set(roots.filter(\.isAvailable).map(\.id))
        for movie in movies {
            try movie.validate()
            guard rootIDs.contains(movie.rootID) else { throw MovieLibraryError.invalidResponse }
        }
        for title in liveTitles {
            try title.validate()
            guard rootIDs.contains(title.rootID) else { throw MovieLibraryError.invalidResponse }
        }
    }
}

// MARK: - Live playback routes

/// Body of `POST liveStartPath`. `issuedAt` is the signed timestamp of the first start request
/// for `startID`, in Unix milliseconds; every retry and cleanup request for it repeats it.
struct LiveStartRequest: Codable, Equatable, Sendable {
    let liveVersion: Int
    let startID: UUID
    let issuedAt: Int64
    let titleID: String
    let revision: String
    let audioPID: Int

    init(startID: UUID, issuedAt: Int64, titleID: String, revision: String, audioPID: Int) {
        liveVersion = MovieLibraryContract.liveVersion
        self.startID = startID
        self.issuedAt = issuedAt
        self.titleID = titleID
        self.revision = revision
        self.audioPID = audioPID
    }

    func validate() throws {
        guard liveVersion == MovieLibraryContract.liveVersion,
              MovieLibraryContract.isDigest(titleID), MovieLibraryContract.isDigest(revision),
              (1...8_191).contains(audioPID), issuedAt > 0
        else { throw MovieLibraryError.invalidResponse }
    }
}

/// Body of `POST liveCancelPath` and `POST liveReleasePath`.
struct LiveStartReference: Codable, Equatable, Hashable, Sendable {
    let startID: UUID
    let issuedAt: Int64
}

/// Signed 202 body of an admitted start. It is how the player learns the stream ID, which is
/// also the ID of the stream session it derives with `liveStreamSession(streamID:)`.
struct LiveStartResponse: Codable, Equatable, Sendable {
    let liveVersion: Int
    let streamID: UUID
    let state: String

    init(streamID: UUID, state: String = "starting") {
        liveVersion = MovieLibraryContract.liveVersion
        self.streamID = streamID
        self.state = state
    }
}

/// Signed 409 or 429 body of a start that was not admitted.
struct LiveStartRefusal: Codable, Equatable, Sendable {
    enum Reason: String, Codable, Sendable {
        case busy, cancelled, conflict, sourceChanged, rateLimited
    }

    let liveVersion: Int
    let outcome: Reason
    let message: String?

    init(outcome: Reason, message: String? = nil) {
        liveVersion = MovieLibraryContract.liveVersion
        self.outcome = outcome
        self.message = message
    }
}

/// Signed 200 body of `POST liveReleasePath`. `drained` is true only when the slot was held by
/// exactly that start identity and has now been released.
struct LiveReleaseResponse: Codable, Equatable, Sendable {
    let drained: Bool
}

/// Signed 200 body of `GET liveStatusPrefix + <streamID>`. A stream that is not current is
/// `unknown`.
struct LiveStreamStatus: Codable, Equatable, Sendable {
    enum State: String, Codable, Sendable {
        case starting, live, complete, failed, stopped, unknown
    }

    struct Failure: Codable, Equatable, Sendable {
        let code: String
        let message: String
    }

    let liveVersion: Int
    let state: State
    let earliestMilliseconds: Int64?
    let latestMilliseconds: Int64?
    let failure: Failure?
    let stopReason: String?
    let macBusyConverting: Bool

    init(
        state: State, earliestMilliseconds: Int64? = nil, latestMilliseconds: Int64? = nil,
        failure: Failure? = nil, stopReason: String? = nil, macBusyConverting: Bool = false
    ) {
        liveVersion = MovieLibraryContract.liveVersion
        self.state = state
        self.earliestMilliseconds = earliestMilliseconds
        self.latestMilliseconds = latestMilliseconds
        self.failure = failure
        self.stopReason = stopReason
        self.macBusyConverting = macBusyConverting
    }
}

struct MovieByteRequest: Equatable, Sendable {
    let movieID: String
    let revision: String
    let offset: Int64
    let count: Int

    var requestTarget: String {
        "\(MovieLibraryContract.bytesPrefix)\(movieID)/\(revision)/\(offset)/\(count)"
    }

    init(movie: SharedMovie, offset: Int64, count: Int) throws {
        guard offset >= 0, offset < movie.byteCount, count > 0,
              count <= MovieLibraryContract.maximumChunkBytes,
              Int64(count) <= movie.byteCount - offset
        else { throw MovieLibraryError.invalidRange }
        movieID = movie.id
        revision = movie.revision
        self.offset = offset
        self.count = count
    }

    init(requestTarget: String) throws {
        guard requestTarget.hasPrefix(MovieLibraryContract.bytesPrefix) else { throw MovieLibraryError.invalidRange }
        let parts = requestTarget.dropFirst(MovieLibraryContract.bytesPrefix.count).split(separator: "/", omittingEmptySubsequences: false)
        guard parts.count == 4,
              MovieLibraryContract.isDigest(String(parts[0])), MovieLibraryContract.isDigest(String(parts[1])),
              let offset = Int64(parts[2]), offset >= 0, String(offset) == parts[2],
              let count = Int(parts[3]), count > 0, count <= MovieLibraryContract.maximumChunkBytes,
              String(count) == parts[3]
        else { throw MovieLibraryError.invalidRange }
        movieID = String(parts[0])
        revision = String(parts[1])
        self.offset = offset
        self.count = count
    }
}

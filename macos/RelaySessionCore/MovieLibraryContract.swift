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

struct SharedMovieCatalog: Codable, Equatable, Sendable {
    let version: Int
    let roots: [SharedMovieRoot]
    let movies: [SharedMovie]
    let truncated: Bool

    init(roots: [SharedMovieRoot], movies: [SharedMovie], truncated: Bool = false) {
        version = MovieLibraryContract.version
        self.roots = roots
        self.movies = movies
        self.truncated = truncated
    }

    func validate() throws {
        guard version == MovieLibraryContract.version else { throw MovieLibraryError.incompatible }
        guard roots.count <= MovieLibraryContract.maximumRoots,
              movies.count <= MovieLibraryContract.maximumMovies,
              Set(roots.map(\.id)).count == roots.count,
              Set(movies.map(\.id)).count == movies.count,
              roots.allSatisfy({ UUID(uuidString: $0.id) != nil && !$0.name.isEmpty && $0.name.utf8.count <= 512 })
        else { throw MovieLibraryError.invalidResponse }
        let rootIDs = Set(roots.filter(\.isAvailable).map(\.id))
        for movie in movies {
            try movie.validate()
            guard rootIDs.contains(movie.rootID) else { throw MovieLibraryError.invalidResponse }
        }
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

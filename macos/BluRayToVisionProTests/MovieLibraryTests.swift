import AVFoundation
import CryptoKit
import Darwin
import Foundation
import XCTest
@testable import BluRayToVisionPro

final class MovieLibraryTests: XCTestCase {
    func testAnUnsignedLocalBuildKeepsItsPairingInAnOwnerOnlyFileNotTheKeychain() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("trust-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("local-build-movie-sharing.json")

        let store = MovieLibraryTrustStore.forRunningApp(isTeamSigned: false, localBuildDirectory: directory)
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
        let hostKey = try store.hostPrivateKey()
        try store.trustClient(publicKey: Data(repeating: 7, count: 32), name: "Headset")

        // A relaunch of the same local build finds the same identity and peers.
        let relaunched = MovieLibraryTrustStore.forRunningApp(isTeamSigned: false, localBuildDirectory: directory)
        XCTAssertEqual(try relaunched.hostPrivateKey(), hostKey)
        XCTAssertEqual(try relaunched.trustedClients().map(\.name), ["Headset"])
        let permissions = try FileManager.default.attributesOfItem(atPath: file.path)[.posixPermissions] as? NSNumber
        XCTAssertEqual(permissions?.int16Value, 0o600)
    }

    func testSecurityScopedBookmarkReopensApprovedDirectory() async throws {
        let fixture = try MovieTestFixture()
        defer { fixture.remove() }
        try Data(repeating: 1, count: 8).write(to: fixture.root.appendingPathComponent("Bookmarked.mov"))
        let bookmark = try fixture.root.bookmarkData(options: .withSecurityScope, includingResourceValuesForKeys: nil, relativeTo: nil)
        let catalog = try MovieCatalog(sources: [MovieSourceBookmark(id: fixture.rootID, name: "Approved", bookmark: bookmark)])
        let snapshot = try await catalog.snapshot()
        XCTAssertEqual(snapshot.movies.map(\.fileName), ["Bookmarked.mov"])
        await catalog.stop()
    }

    func testLoaderCancellationFinishesLoadingAndCancelsOutstandingRead() async throws {
        let started = expectation(description: "Range read started")
        let cancelled = expectation(description: "Range read cancelled")
        let movie = SharedMovie(id: String(repeating: "a", count: 64), rootID: UUID().uuidString, fileName: "Movie.mov", byteCount: 100_000, revision: String(repeating: "b", count: 64))
        let loader = try MovieResourceLoader(movie: movie) { _, _ in
            started.fulfill()
            do { try await Task.sleep(for: .seconds(30)) }
            catch { cancelled.fulfill(); throw error }
            throw MovieLibraryError.unavailable
        }
        let asset = loader.makeAsset()
        let loading = Task {
            do { _ = try await asset.load(.duration); return false }
            catch { return true }
        }
        await fulfillment(of: [started], timeout: 3)
        loader.cancel()
        await fulfillment(of: [cancelled], timeout: 3)
        let failed = await loading.value
        XCTAssertTrue(failed)
    }

    func testApprovedRootsRejectSymlinksReplacementAndSpecialFiles() async throws {
        let fixture = try MovieTestFixture()
        defer { fixture.remove() }
        let movieURL = fixture.root.appendingPathComponent("Movie.mov")
        try Data(repeating: 7, count: 32).write(to: movieURL)
        let outside = fixture.directory.appendingPathComponent("Outside.mov")
        try Data(repeating: 9, count: 32).write(to: outside)
        try FileManager.default.createSymbolicLink(at: fixture.root.appendingPathComponent("Escape.mov"), withDestinationURL: outside)
        try FileManager.default.createSymbolicLink(at: fixture.root.appendingPathComponent("EscapeFolder"), withDestinationURL: fixture.directory)
        let catalog = try fixture.catalog()
        let snapshot = try await catalog.snapshot()
        XCTAssertEqual(snapshot.movies.map(\.fileName), ["Movie.mov"])
        let movie = try XCTUnwrap(snapshot.movies.first)
        let range = try MovieByteRequest(movie: movie, offset: 8, count: 16)
        let bytes = try await catalog.read(range)
        XCTAssertEqual(bytes, Data(repeating: 7, count: 16))
        try FileManager.default.removeItem(at: movieURL)
        try FileManager.default.createSymbolicLink(at: movieURL, withDestinationURL: outside)
        await expectFailure { _ = try await catalog.read(range) }
        try FileManager.default.removeItem(at: movieURL)
        XCTAssertEqual(mkfifo(movieURL.path, 0o600), 0)
        await expectFailure { _ = try await catalog.read(range) }
        await catalog.stop()
        await expectFailure { _ = try await catalog.snapshot() }
    }

    func testLargeOffsetsAndChangedRevisionCannotReadStaleMovie() async throws {
        let fixture = try MovieTestFixture()
        defer { fixture.remove() }
        let file = fixture.root.appendingPathComponent("Large.mp4")
        XCTAssertTrue(FileManager.default.createFile(atPath: file.path, contents: Data()))
        let handle = try FileHandle(forWritingTo: file)
        defer { try? handle.close() }
        let offset: UInt64 = 5 * 1_024 * 1_024 * 1_024
        try handle.seek(toOffset: offset)
        try handle.write(contentsOf: Data([1, 2, 3, 4]))
        let catalog = try fixture.catalog()
        let snapshot = try await catalog.snapshot()
        let movie = try XCTUnwrap(snapshot.movies.first)
        XCTAssertEqual(movie.byteCount, Int64(offset + 4))
        let request = try MovieByteRequest(movie: movie, offset: Int64(offset), count: 4)
        XCTAssertEqual(try MovieByteRequest(requestTarget: request.requestTarget), request)
        let bytes = try await catalog.read(request)
        XCTAssertEqual(bytes, Data([1, 2, 3, 4]))
        try handle.seek(toOffset: offset)
        try handle.write(contentsOf: Data([4, 3, 2, 1]))
        await expectFailure { _ = try await catalog.read(request) }
        let refreshed = try await catalog.snapshot()
        XCTAssertNotEqual(refreshed.movies.first?.resumeID, movie.resumeID)
        for suffix in ["-1/1", "01/1", "0/0", "0/1048577", "9223372036854775808/1", "0/1?x=1", "0/1/extra"] {
            XCTAssertThrowsError(try MovieByteRequest(requestTarget: "\(MovieLibraryContract.bytesPrefix)\(movie.id)/\(movie.revision)/\(suffix)"))
        }
    }

    func testRememberedPairingStillRequiresPossessionAndForgettingRevokesAccess() async throws {
        let fixture = try MovieTestFixture()
        defer { fixture.remove() }
        try Data(repeating: 5, count: 64).write(to: fixture.root.appendingPathComponent("Shared.mov"))
        let hostTrust = MovieTestKeychain()
        let clientTrust = MovieTestKeychain()
        let host = try MovieLibraryHost(catalog: fixture.catalog(), trust: hostTrust.store)
        let transport = MovieTestTransport(host: host)
        let client = MovieLibraryClient(baseURL: MovieTestTransport.url, name: "Test Mac", transport: transport, trust: clientTrust.store)
        let prompt = try await client.beginPairing()
        XCTAssertFalse(prompt.isRemembered)
        let waiting = try await client.confirm(matches: true)
        XCTAssertFalse(waiting)
        await expectFailure { _ = try await client.catalog() }
        let pending = try await host.pendingCandidate()
        let candidate = try XCTUnwrap(pending)
        try await host.approve(candidate.candidateID)
        let complete = try await client.confirm(matches: true)
        XCTAssertTrue(complete)
        let catalog = try await client.catalog()
        let movie = try XCTUnwrap(catalog.movies.first)
        let bytes = try await client.bytes(movie: movie, offset: 1, count: 8)
        XCTAssertEqual(bytes, Data(repeating: 5, count: 8))
        await transport.tamperNextBody()
        await expectFailure { _ = try await client.bytes(movie: movie, offset: 8, count: 8) }
        let replayStatus = try await transport.replayLastRequest()
        XCTAssertEqual(replayStatus, 401)
        let advertisement = await host.advertisedBonjourService()
        XCTAssertNotNil(advertisement)
        await client.disconnect()
        let remembered = try await client.beginPairing()
        XCTAssertTrue(remembered.isRemembered)
        let automaticallyApproved = try await client.confirm(matches: true)
        XCTAssertTrue(automaticallyApproved)
        try await client.forget()
        XCTAssertTrue(try hostTrust.store.trustedClients().isEmpty)
        XCTAssertTrue(try clientTrust.store.trustedServers().isEmpty)
        await expectFailure { _ = try await client.catalog() }
        await host.stop()
    }

    func testRecognizedPublicKeyWithoutPrivateKeyCannotCompletePairing() async throws {
        let fixture = try MovieTestFixture()
        defer { fixture.remove() }
        let keychain = MovieTestKeychain()
        let honestKey = Curve25519.KeyAgreement.PrivateKey().publicKey.rawRepresentation
        try keychain.store.trustClient(publicKey: honestKey, name: "Vision Pro")
        let host = try MovieLibraryHost(catalog: fixture.catalog(), trust: keychain.store)
        let response = await host.handle(MovieTestTransport.rawRequest(path: MovieLibraryContract.challengePath), peer: .localNetwork)
        let challenge = try JSONDecoder().decode(MovieLibraryChallenge.self, from: response.body).challenge
        let forged = try RelayPairingRequest(sessionID: challenge.sessionID, serverNonceCommitment: challenge.serverNonceCommitment, clientPublicKey: honestKey, clientNonce: Data(repeating: 3, count: 32))
        let candidate = await host.handle(MovieTestTransport.rawRequest(path: MovieLibraryContract.pairingPath, method: "POST", body: try JSONEncoder().encode(MoviePairingRequest(request: forged, name: "Impostor"))), peer: .localNetwork)
        XCTAssertEqual(candidate.statusCode, 200)
        let summary = try await host.pendingCandidate()
        XCTAssertEqual(summary?.isMacApproved, true)
        let denied = await host.handle(MovieTestTransport.rawRequest(path: MovieLibraryContract.confirmationPath, method: "POST", body: Data("{}".utf8)), peer: .localNetwork)
        XCTAssertEqual(denied.statusCode, 401)
        let catalog = await host.handle(MovieTestTransport.rawRequest(path: MovieLibraryContract.catalogPath), peer: .localNetwork)
        XCTAssertEqual(catalog.statusCode, 401)
        await host.stop()
    }

    func testKeychainErrorsNeverReplaceExistingIdentity() throws {
        let keychain = MovieTestKeychain()
        let original = try keychain.store.hostPrivateKey()
        XCTAssertEqual(try keychain.store.hostPrivateKey(), original)
        let before = keychain.snapshot()
        keychain.setReadFailure(true)
        XCTAssertThrowsError(try keychain.store.hostPrivateKey())
        XCTAssertEqual(keychain.snapshot(), before)
        keychain.setReadFailure(false)
        keychain.replace(with: Data("broken".utf8))
        XCTAssertThrowsError(try keychain.store.hostPrivateKey())
        XCTAssertEqual(keychain.snapshot(), Data("broken".utf8))
    }

    func testPersistentIdentityRotatesNonceAndDefaultStillRotatesKey() async throws {
        let date = Date(timeIntervalSince1970: 1_700_000_000)
        for retain in [false, true] {
            let context = try RelayServerPairingContext(retainsServerIdentity: retain, now: date, challengeTTL: 1)
            let before = try await context.currentChallenge(now: date)
            let after = try await context.currentChallenge(now: date.addingTimeInterval(2))
            XCTAssertNotEqual(before.serverNonceCommitment, after.serverNonceCommitment)
            XCTAssertEqual(before.serverPublicKey == after.serverPublicKey, retain)
        }
    }

    func testAVFoundationReadsAndSeeksCompletedMovieThroughVerifiedRanges() async throws {
        let fixture = try MovieTestFixture()
        defer { fixture.remove() }
        let source = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("BDToAVPPlayer/Resources/Stereo-Check-SBS.mov")
        try FileManager.default.copyItem(at: source, to: fixture.root.appendingPathComponent("Stereo-SBS.mov"))
        let host = try MovieLibraryHost(catalog: fixture.catalog(), trust: MovieTestKeychain().store)
        let transport = MovieTestTransport(host: host)
        let client = MovieLibraryClient(baseURL: MovieTestTransport.url, name: "Test Mac", transport: transport, trust: MovieTestKeychain().store)
        _ = try await client.beginPairing()
        let pending = try await host.pendingCandidate()
        try await host.approve(XCTUnwrap(pending).candidateID)
        let paired = try await client.confirm(matches: true)
        XCTAssertTrue(paired)
        let catalog = try await client.catalog()
        let movie = try XCTUnwrap(catalog.movies.first)
        let loader = try MovieResourceLoader(movie: movie) { offset, count in
            try await client.bytes(movie: movie, offset: offset, count: count)
        }
        defer { loader.cancel() }
        let asset = loader.makeAsset()
        let duration = try await asset.load(.duration)
        XCTAssertGreaterThan(duration.seconds, 1)
        let generator = AVAssetImageGenerator(asset: asset)
        let first = try await generator.image(at: .zero)
        let later = try await generator.image(at: CMTime(seconds: duration.seconds * 0.75, preferredTimescale: 600))
        XCTAssertGreaterThan(first.image.width, 0)
        XCTAssertEqual(later.image.width, first.image.width)
        let requestCount = await transport.byteRequests
        XCTAssertGreaterThan(requestCount, 1)
        await host.stop()
    }

    private func expectFailure(_ action: () async throws -> Void, file: StaticString = #filePath, line: UInt = #line) async {
        do { try await action(); XCTFail("Expected the request to fail", file: file, line: line) }
        catch { }
    }
}

private struct MovieTestFixture {
    let directory: URL
    let root: URL
    let rootID = UUID().uuidString
    init() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("MovieLibraryTests-\(UUID().uuidString)", isDirectory: true)
        root = directory.appendingPathComponent("Approved", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }
    func catalog() throws -> MovieCatalog {
        try MovieCatalog(sources: [MovieSourceBookmark(id: rootID, name: "Approved", bookmark: Data())], resolve: { _ in root })
    }
    func remove() { try? FileManager.default.removeItem(at: directory) }
}

private final class MovieTestKeychain: @unchecked Sendable {
    private let lock = NSLock()
    private var data: Data?
    private var fails = false
    var store: MovieLibraryTrustStore {
        MovieLibraryTrustStore(read: { [self] in
            try lock.withLock { if fails { throw MovieLibraryError.credentialStore(-25308) }; return data }
        }, write: { [self] value in lock.withLock { data = value } })
    }
    func snapshot() -> Data? { lock.withLock { data } }
    func replace(with data: Data) { lock.withLock { self.data = data } }
    func setReadFailure(_ value: Bool) { lock.withLock { fails = value } }
}

private actor MovieTestTransport: MovieHTTPTransport {
    static let url = URL(string: "http://192.168.1.2:49152")!
    let host: MovieLibraryHost
    private var tamper = false
    private var lastRequest: Data?
    private(set) var byteRequests = 0
    init(host: MovieLibraryHost) { self.host = host }
    func tamperNextBody() { tamper = true }
    func replayLastRequest() async throws -> Int {
        let request = try XCTUnwrap(lastRequest)
        return await host.handle(request, peer: .localNetwork).statusCode
    }
    func data(for request: URLRequest, maximumBytes: Int) async throws -> (Data, HTTPURLResponse) {
        let target = try XCTUnwrap(request.url?.path)
        var headers = request.allHTTPHeaderFields ?? [:]
        headers["Content-Length"] = String(request.httpBody?.count ?? 0)
        let raw = Self.rawRequest(path: target, method: request.httpMethod ?? "GET", body: request.httpBody ?? Data(), headers: headers)
        let response = await host.handle(raw, peer: .localNetwork)
        lastRequest = raw
        if target.hasPrefix(MovieLibraryContract.bytesPrefix) { byteRequests += 1 }
        var body = response.body
        if tamper { tamper = false; body.append(0) }
        XCTAssertLessThanOrEqual(body.count, maximumBytes)
        return (body, try XCTUnwrap(HTTPURLResponse(url: request.url!, statusCode: response.statusCode, httpVersion: "HTTP/1.1", headerFields: response.headers)))
    }
    static func rawRequest(path: String, method: String = "GET", body: Data = Data(), headers: [String: String] = [:]) -> Data {
        var text = "\(method) \(path) HTTP/1.1\r\nHost: 192.168.1.2\r\n"
        let headers = headers.filter { $0.key.lowercased() != "content-length" }
        for (key, value) in headers { text += "\(key): \(value)\r\n" }
        text += "Content-Length: \(body.count)\r\n\r\n"
        return Data(text.utf8) + body
    }
}

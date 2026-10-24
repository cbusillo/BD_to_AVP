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

    func testResumeKeySurvivesAFolderReAddAndChangeTimeChurnButNotAnEdit() async throws {
        let fixture = try MovieTestFixture()
        defer { fixture.remove() }
        let file = fixture.root.appendingPathComponent("Ocean Dreams.mov")
        try Data(repeating: 3, count: 64).write(to: file)
        let root = fixture.root
        func scan(approvalID: String) async throws -> SharedMovie {
            let catalog = try MovieCatalog(
                sources: [MovieSourceBookmark(id: approvalID, name: "Testers", bookmark: Data())],
                resolve: { _ in root }
            )
            let movie = try await catalog.snapshot().movies.first
            await catalog.stop()
            return try XCTUnwrap(movie)
        }

        let original = try await scan(approvalID: UUID().uuidString)
        let reAdded = try await scan(approvalID: UUID().uuidString)
        XCTAssertNotEqual(reAdded.id, original.id)
        XCTAssertEqual(reAdded.resumeID, original.resumeID)

        // A permission change moves the change time, as metadata churn on a share can.
        XCTAssertEqual(chmod(file.path, 0o600), 0)
        let churned = try await scan(approvalID: UUID().uuidString)
        XCTAssertNotEqual(churned.revision, reAdded.revision)
        XCTAssertEqual(churned.resumeID, original.resumeID)

        try Data(repeating: 4, count: 80).write(to: file)
        let edited = try await scan(approvalID: UUID().uuidString)
        XCTAssertNotEqual(edited.resumeID, original.resumeID)
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
    func catalog(maxListingAge: TimeInterval? = nil, now: @escaping @Sendable () -> Date = { Date() }) throws -> MovieCatalog {
        let root = root
        return try MovieCatalog(
            sources: [MovieSourceBookmark(id: rootID, name: "Approved", bookmark: Data())],
            maxListingAge: maxListingAge, now: now, resolve: { _ in root }
        )
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

// MARK: - Live ISO titles

private actor FakeLiveTitleInspector: LiveTitleInspecting {
    private(set) var calls = 0
    private(set) var inspectedPaths: [String] = []

    func inspect(_ lease: LiveSourceLease) async throws -> LiveTitleInspection {
        calls += 1
        inspectedPaths.append(lease.canonicalPath)
        let json = """
        {"eligible":true,"unsupported_reason":null,"playlist":1005,"duration_seconds":5400.0,
         "audio_streams":[{"pid":4352,"coding_type":134,"format":6,"rate":12,"language":"eng"},
                          {"pid":4353,"coding_type":129,"format":6,"rate":12,"language":"fra"}]}
        """
        return try JSONDecoder().decode(LiveTitleInspection.self, from: Data(json.utf8))
    }
}

/// The shipped v0.3.3 catalog types, copied as they were, to prove an old player still reads a
/// catalog with live titles.
private struct ShippedMovieCatalog: Decodable {
    struct Movie: Decodable { let id: String; let rootID: String; let fileName: String; let byteCount: Int64; let revision: String }
    struct Root: Decodable { let id: String; let name: String; let isAvailable: Bool }
    let version: Int
    let roots: [Root]
    let movies: [Movie]
    let truncated: Bool

    func validate() throws {
        guard version == 1 else { throw MovieLibraryError.incompatible }
        for movie in movies where !["mov", "mp4", "m4v"].contains((movie.fileName as NSString).pathExtension.lowercased()) {
            throw MovieLibraryError.invalidResponse
        }
    }
}

private final class LiveHostHarness: @unchecked Sendable {
    let fixture: MovieTestFixture
    let clock = LiveTestClock(Date())
    let starter: FakeLiveRelayStreamStarter
    let inspector = FakeLiveTitleInspector()
    let index: LiveTitleIndex
    let admission: LiveStartAdmission
    let host: MovieLibraryHost
    let transport: MovieTestTransport
    let client: MovieLibraryClient
    let hostTrust = MovieTestKeychain()

    init(
        streamStates: [LiveRelayStreamState] = [.live(earliestMs: 0, latestMs: 6_000)],
        open: LiveSourceAuthorizer.Opener? = nil,
        busy: Bool = false
    ) throws {
        fixture = try MovieTestFixture()
        try Data(repeating: 3, count: 4_096).write(to: fixture.root.appendingPathComponent("Rainforest.iso"))
        try Data(repeating: 4, count: 64).write(to: fixture.root.appendingPathComponent("Shared.mov"))
        let clock = clock
        starter = FakeLiveRelayStreamStarter(holdsStarts: false, streamStates: streamStates)
        var authorizer = LiveSourceAuthorizer()
        if let open { authorizer.open = open }
        index = LiveTitleIndex(storeURL: nil, authorizer: authorizer, inspector: inspector, now: { clock.now })
        admission = LiveStartAdmission(
            starter: starter, policy: try RelayRequestValidationPolicy(), now: { clock.now }, watchInterval: .milliseconds(20)
        )
        let services = MovieLibraryLiveServices(admission: admission, index: index, authorizer: authorizer, isBusyConverting: { busy })
        host = try MovieLibraryHost(catalog: fixture.catalog(), trust: hostTrust.store, now: { clock.now }, live: services)
        transport = MovieTestTransport(host: host)
        client = MovieLibraryClient(
            baseURL: MovieTestTransport.url, name: "Test Mac", transport: transport, trust: MovieTestKeychain().store,
            clock: { clock.now }
        )
    }

    func pair() async throws {
        _ = try await client.beginPairing()
        let pending = try await host.pendingCandidate()
        try await host.approve(XCTUnwrap(pending).candidateID)
        let paired = try await client.confirm(matches: true)
        XCTAssertTrue(paired)
    }

    /// Lists the catalog until the ISO's inspection has finished.
    func eligibleTitle() async throws -> SharedLiveTitle {
        _ = try await client.catalog()
        await index.waitUntilIdle()
        let catalog = try await client.catalog()
        let title = try XCTUnwrap(catalog.liveTitles.first)
        XCTAssertEqual(title.state, .eligible)
        return title
    }

    func start(_ title: SharedLiveTitle, startID: UUID = UUID(), audioPID: Int = 4352) async throws -> (LiveStartResult, LiveStartReference) {
        let issuedAt = try XCTUnwrap(RelayTime.unixMilliseconds(for: clock.now))
        let request = LiveStartRequest(startID: startID, issuedAt: issuedAt, titleID: title.id, revision: title.revision, audioPID: audioPID)
        let result = try await client.startLive(request, signedAt: Date(timeIntervalSince1970: TimeInterval(issuedAt) / 1_000))
        return (result, LiveStartReference(startID: startID, issuedAt: issuedAt))
    }

    func started(_ title: SharedLiveTitle) async throws -> (UUID, LiveStartReference) {
        let (result, reference) = try await start(title)
        guard case let .started(response) = result else {
            XCTFail("Expected an admitted start, got \(result)")
            throw MovieLibraryError.unavailable
        }
        return (response.streamID, reference)
    }

    struct RelayAnswer {
        let status: Int
        let body: Data
        /// Whether the response verified under the derived stream session.
        let isSigned: Bool
        var ended: RelayLiveStreamEnded? { try? JSONDecoder().decode(RelayLiveStreamEnded.self, from: body) }
    }

    enum Capability { case derived, missing, wrong }

    /// A relay request signed with the stream session derived from the client's parent session.
    func relay(
        _ path: String,
        streamID: UUID,
        method: String = "GET",
        body: Data = Data(),
        capability: Capability = .derived,
        session: RelayEstablishedSession? = nil
    ) async throws -> RelayAnswer {
        let derived: RelayEstablishedSession
        if let session { derived = session } else { derived = try await client.liveStreamSession(streamID: streamID).session }
        let nonce = UUID().uuidString.lowercased()
        let proof = try derived.signRequest(method: method, requestTarget: path, timestamp: clock.now, nonce: nonce, body: body)
        var headers = [RelayWireContract.authenticationHeader: try JSONEncoder().encode(proof).base64EncodedString()]
        switch capability {
        case .derived: headers[RelayWireContract.mediaCapabilityHeader] = derived.mediaCapability.value
        case .missing: break
        case .wrong: headers[RelayWireContract.mediaCapabilityHeader] = String(repeating: "A", count: 43)
        }
        let response = await host.handle(
            MovieTestTransport.rawRequest(path: path, method: method, body: body, headers: headers), peer: .localNetwork
        )
        var isSigned = false
        if let header = response.headers[RelayWireContract.responseAuthenticationHeader],
           let data = Data(base64Encoded: header),
           let responseProof = try? JSONDecoder().decode(RelayAuthenticatedResponse.self, from: data) {
            try derived.verifyResponse(responseProof, requestNonce: nonce, actualStatusCode: response.statusCode, body: response.body, now: clock.now)
            isSigned = true
        }
        return RelayAnswer(status: response.statusCode, body: response.body, isSigned: isSigned)
    }

    func waitUntilSlotIsEmpty() async throws {
        let deadline = Date().addingTimeInterval(5)
        while await !admission.isEmpty {
            guard Date() < deadline else { throw MovieLibraryError.unavailable }
            try await Task.sleep(for: .milliseconds(5))
        }
    }

    func stream(_ index: Int = 0) async throws -> FakeLiveRelayStream {
        let launched = await starter.launched
        guard launched.indices.contains(index) else { throw MovieLibraryError.unavailable }
        return launched[index]
    }

    func lastStream() async throws -> FakeLiveRelayStream {
        let launched = await starter.launched
        return try XCTUnwrap(launched.last)
    }

    func remove() { fixture.remove() }
}

final class MovieLibraryLiveTests: XCTestCase {
    func testCatalogWithLiveTitlesStillDecodesForShippedPlayersAndSkipsUnknownVersions() async throws {
        let harness = try LiveHostHarness()
        defer { harness.remove() }
        try await harness.pair()
        let title = try await harness.eligibleTitle()
        XCTAssertEqual(title.fileName, "Rainforest.iso")
        XCTAssertEqual(title.audio.map(\.pid), [4352, 4353])
        XCTAssertEqual(title.audio.first?.label, "English DTS-HD Master Audio")
        XCTAssertEqual(title.defaultAudio?.pid, 4352)

        // The host encodes this same type; re-encoding the verified catalog gives its wire shape.
        let encoded = try JSONEncoder().encode(try await harness.client.catalog())
        let shipped = try JSONDecoder().decode(ShippedMovieCatalog.self, from: encoded)
        XCTAssertNoThrow(try shipped.validate())
        XCTAssertEqual(shipped.movies.map(\.fileName), ["Shared.mov"], "ISOs never enter movies")

        let withoutLive = SharedMovieCatalog(roots: [], movies: [])
        let plain = try JSONEncoder().encode(withoutLive)
        XCTAssertFalse(String(decoding: plain, as: UTF8.self).contains("liveTitles"))
        XCTAssertEqual(try JSONDecoder().decode(SharedMovieCatalog.self, from: plain).liveTitles, [])

        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        var titles = try XCTUnwrap(object["liveTitles"] as? [[String: Any]])
        titles.append(["liveVersion": 2, "someFutureShape": true])
        object["liveTitles"] = titles
        let future = try JSONDecoder().decode(SharedMovieCatalog.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertEqual(future.liveTitles.map(\.id), [title.id])
        XCTAssertNoThrow(try future.validate())
        await harness.host.stop()
    }

    func testIndexInspectsOncePerRevisionAndAgainAfterTheFileChanges() async throws {
        let harness = try LiveHostHarness()
        defer { harness.remove() }
        try await harness.pair()
        let first = try await harness.client.catalog()
        XCTAssertEqual(first.liveTitles.first?.state, .checking)
        _ = try await harness.eligibleTitle()
        _ = try await harness.client.catalog()
        await harness.index.waitUntilIdle()
        var calls = await harness.inspector.calls
        XCTAssertEqual(calls, 1)
        let paths = await harness.inspector.inspectedPaths
        XCTAssertEqual(paths.first?.hasSuffix("/Approved/Rainforest.iso"), true)

        let handle = try FileHandle(forWritingTo: harness.fixture.root.appendingPathComponent("Rainforest.iso"))
        try handle.seekToEnd()
        try handle.write(contentsOf: Data([9]))
        try handle.close()
        let changed = try await harness.client.catalog()
        XCTAssertEqual(changed.liveTitles.first?.state, .checking)
        await harness.index.waitUntilIdle()
        calls = await harness.inspector.calls
        XCTAssertEqual(calls, 2)
        await harness.host.stop()
    }

    func testAListingIsServedFromTheLastReadAndRereadInTheBackgroundOnceItIsOld() async throws {
        let fixture = try MovieTestFixture()
        defer { fixture.remove() }
        let clock = LiveTestClock()
        try Data(repeating: 1, count: 8).write(to: fixture.root.appendingPathComponent("First.mov"))
        let catalog = try fixture.catalog(maxListingAge: 30, now: { clock.now })
        let first = try await catalog.listing()
        XCTAssertEqual(first.catalog.movies.map(\.fileName), ["First.mov"])

        try Data(repeating: 2, count: 8).write(to: fixture.root.appendingPathComponent("Second.mov"))
        clock.advance(by: 10)
        let fresh = try await catalog.listing()
        XCTAssertEqual(fresh.catalog.movies.map(\.fileName), ["First.mov"], "A young listing is served without a walk")

        clock.advance(by: 30)
        let stale = try await catalog.listing()
        XCTAssertEqual(stale.catalog.movies.map(\.fileName), ["First.mov"], "An old listing is served while the reread runs")
        var reread = stale
        for _ in 0 ..< 500 where reread.catalog.movies.count < 2 {
            try await Task.sleep(for: .milliseconds(10))
            reread = try await catalog.listing()
        }
        XCTAssertEqual(reread.catalog.movies.map(\.fileName), ["First.mov", "Second.mov"])
        await catalog.stop()
    }

    func testAFolderThatCouldNotBeOpenedIsOpenedAgainOnTheNextRead() async throws {
        let fixture = try MovieTestFixture()
        defer { fixture.remove() }
        try Data(repeating: 1, count: 8).write(to: fixture.root.appendingPathComponent("Shared.mov"))
        let consent = LockedFlag()
        let root = fixture.root
        let catalog = try MovieCatalog(
            sources: [MovieSourceBookmark(id: fixture.rootID, name: "Approved", bookmark: Data())],
            resolve: { _ in
                guard consent.isSet else { throw CocoaError(.fileReadNoPermission) }
                return root
            }
        )
        let denied = try await catalog.read()
        XCTAssertEqual(denied.catalog.roots.map(\.isAvailable), [false])
        let unreadable = await catalog.unreadableRootIDs()
        XCTAssertEqual(unreadable, [fixture.rootID])

        consent.set()
        let allowed = try await catalog.read()
        XCTAssertEqual(allowed.catalog.movies.map(\.fileName), ["Shared.mov"])
        let readable = await catalog.unreadableRootIDs()
        XCTAssertTrue(readable.isEmpty)
        await catalog.stop()
    }

    func testPreparingTheCatalogChecksISOsBeforeAnyHeadsetAsks() async throws {
        let harness = try LiveHostHarness()
        defer { harness.remove() }
        try await harness.host.prepareCatalog()
        await harness.index.waitUntilIdle()
        let calls = await harness.inspector.calls
        XCTAssertEqual(calls, 1)
        await harness.host.stop()
    }

    func testAuthorizerRefusesASymlinkANewInodeAndAMovedRoot() async throws {
        let fixture = try MovieTestFixture()
        defer { fixture.remove() }
        let iso = fixture.root.appendingPathComponent("Disc.iso")
        try Data(repeating: 1, count: 128).write(to: iso)
        let catalog = try fixture.catalog()
        let authorizer = LiveSourceAuthorizer()
        let listed = try await catalog.listing().liveCandidates
        let candidate = try XCTUnwrap(listed.first)
        let lease = try await authorizer.authorize(candidate, revision: candidate.revision)
        XCTAssertTrue(lease.canonicalPath.hasSuffix("/Approved/Disc.iso"))
        XCTAssertNotNil(lease.retainer as? LiveSourceDescriptorHolding)

        let outside = fixture.directory.appendingPathComponent("Outside.iso")
        try Data(repeating: 2, count: 128).write(to: outside)
        try FileManager.default.removeItem(at: iso)
        try FileManager.default.createSymbolicLink(at: iso, withDestinationURL: outside)
        await assertAuthorization(authorizer, candidate, fails: .changed)

        try FileManager.default.removeItem(at: iso)
        try Data(repeating: 1, count: 128).write(to: iso)
        await assertAuthorization(authorizer, candidate, fails: .changed)

        let relisted = try await catalog.listing().liveCandidates
        let fresh = try XCTUnwrap(relisted.first)
        try FileManager.default.moveItem(at: fixture.root, to: fixture.directory.appendingPathComponent("Moved", isDirectory: true))
        try FileManager.default.createDirectory(at: fixture.root, withIntermediateDirectories: true)
        await assertAuthorization(authorizer, fresh, fails: .changed)
        await catalog.stop()
    }

    func testRefusedOpenListsTheTitleIneligibleWithThePermissionMessage() async throws {
        let harness = try LiveHostHarness(open: { _, _ in .failure(MovieFileAccessError(code: EACCES)) })
        defer { harness.remove() }
        try await harness.pair()
        _ = try await harness.client.catalog()
        await harness.index.waitUntilIdle()
        let catalog = try await harness.client.catalog()
        let title = try XCTUnwrap(catalog.liveTitles.first)
        XCTAssertEqual(title.state, .ineligible)
        XCTAssertEqual(title.reason, LiveSourceAuthorizationError.permissionMessage)
        let inspections = await harness.inspector.calls
        XCTAssertEqual(inspections, 0, "the worker is never asked to open what the app could not")

        let (result, _) = try await harness.start(title)
        XCTAssertEqual(result, .refused(LiveStartRefusal(outcome: .sourceChanged, message: LiveSourceAuthorizationError.permissionMessage)))
        await harness.host.stop()
    }

    func testStartStatusRelayAndCancelThroughTheHost() async throws {
        let harness = try LiveHostHarness(busy: true)
        defer { harness.remove() }
        try await harness.pair()
        let title = try await harness.eligibleTitle()
        let (streamID, reference) = try await harness.started(title)

        // The host answers 202 once admission is reserved and launches the starter in a task.
        await harness.starter.waitForCalls(1)
        let calls = await harness.starter.calls
        let call = try XCTUnwrap(calls.first)
        XCTAssertEqual(call.id, streamID)
        XCTAssertEqual(call.request.playlist, 1005, "the playlist comes from the index")
        XCTAssertEqual(call.request.audioPID, 4352)
        XCTAssertNotNil(call.request.source.retainer as? LiveSourceDescriptorHolding)

        let status = try await harness.client.liveStatus(streamID: streamID)
        XCTAssertTrue(status.state == .starting || status.state == .live)
        XCTAssertTrue(status.macBusyConverting)

        let playlist = try await harness.relay(RelayWireContract.playlistPath, streamID: streamID)
        XCTAssertEqual(playlist.status, 200)
        XCTAssertTrue(playlist.isSigned)
        let stream = try await harness.stream()
        let renewals = await stream.leaseRenewals
        XCTAssertGreaterThanOrEqual(renewals, 2, "status and relay requests both renew the lease")

        let parentSignedRelay = await harness.host.handle(
            MovieTestTransport.rawRequest(path: RelayWireContract.playlistPath), peer: .localNetwork
        )
        XCTAssertEqual(parentSignedRelay.statusCode, 401)

        let cancel = try await harness.client.cancelLive(reference)
        XCTAssertEqual(cancel, .acknowledged)
        let stops = await stream.stopCalls
        XCTAssertEqual(stops, [.cancelled])
        let after = try await harness.client.liveStatus(streamID: streamID)
        XCTAssertEqual(after.state, .stopped)
        XCTAssertEqual(after.stopReason, LiveRelayStopReason.cancelled.rawValue)
        let unknown = try await harness.client.liveStatus(streamID: UUID())
        XCTAssertEqual(unknown.state, .unknown)
        await harness.host.stop()
    }

    func testConcurrentRetriesOfOneStartLaunchExactlyOneWorker() async throws {
        let harness = try LiveHostHarness()
        defer { harness.remove() }
        try await harness.pair()
        let title = try await harness.eligibleTitle()
        let startID = UUID()
        async let first = harness.start(title, startID: startID)
        async let second = harness.start(title, startID: startID)
        let outcomes = try await [first.0, second.0]
        guard case let .started(a) = outcomes[0], case let .started(b) = outcomes[1] else {
            return XCTFail("Expected both retries to report the start: \(outcomes)")
        }
        XCTAssertEqual(a.streamID, b.streamID)
        await harness.starter.waitForCalls(1)
        let calls = await harness.starter.calls.count
        XCTAssertEqual(calls, 1)
        await harness.host.stop()
    }

    func testEveryLiveRelayRouteRefusesAMissingOrWrongCapabilityWithASignedForbidden() async throws {
        let harness = try LiveHostHarness()
        defer { harness.remove() }
        try await harness.pair()
        let title = try await harness.eligibleTitle()
        let (streamID, _) = try await harness.started(title)
        let routes: [(String, String, Data)] = [
            (RelayWireContract.playlistPath, "GET", Data()),
            (RelayWireContract.playlistSnapshotPath, "GET", Data()),
            (RelayWireContract.mediaPathPrefix + "init.mp4", "GET", Data()),
            (RelayWireContract.positionPath, "POST", try JSONEncoder().encode(RelayViewerPosition(positionMilliseconds: 1_000))),
            (RelayWireContract.cancelPath, "POST", Data()),
        ]
        for (path, method, body) in routes {
            for capability in [LiveHostHarness.Capability.missing, .wrong] {
                let answer = try await harness.relay(path, streamID: streamID, method: method, body: body, capability: capability)
                XCTAssertEqual(answer.status, 403, "\(path) \(capability)")
                XCTAssertTrue(answer.isSigned, "\(path) \(capability)")
            }
        }
        let stream = try await harness.stream()
        let reached = await stream.requests.count
        XCTAssertEqual(reached, 0, "no refused request reaches the stream")
        await harness.host.stop()
    }

    func testStaleAndNeverIssuedStreamsGetSignedEndsWhileARevokedParentGetsAnUnsigned401() async throws {
        let harness = try LiveHostHarness()
        defer { harness.remove() }
        try await harness.pair()
        let title = try await harness.eligibleTitle()
        let (firstID, _) = try await harness.started(title)
        let (secondID, _) = try await harness.started(title)
        XCTAssertNotEqual(firstID, secondID)

        let replaced = try await harness.relay(RelayWireContract.playlistSnapshotPath, streamID: firstID)
        XCTAssertEqual(replaced.status, 410)
        XCTAssertTrue(replaced.isSigned)
        XCTAssertEqual(replaced.ended, RelayLiveStreamEnded(error: .streamEnded, code: LiveRelayStopReason.replaced.rawValue, message: nil))

        let neverIssued = try await harness.relay(RelayWireContract.playlistSnapshotPath, streamID: UUID())
        XCTAssertEqual(neverIssued.status, 410)
        XCTAssertTrue(neverIssued.isSigned)
        XCTAssertEqual(neverIssued.ended?.code, RelayLiveStreamEnded.notCurrentCode)

        let derived = try await harness.client.liveStreamSession(streamID: secondID).session
        try await harness.client.forget()
        let revoked = try await harness.relay(RelayWireContract.playlistSnapshotPath, streamID: secondID, session: derived)
        XCTAssertEqual(revoked.status, 401)
        XCTAssertFalse(revoked.isSigned)
        XCTAssertTrue(revoked.body.isEmpty)
        let stream = try await harness.lastStream()
        let stops = await stream.stopCalls
        XCTAssertEqual(stops, [.sessionEnded], "forgetting the device drains its stream")
        await harness.host.stop()
    }

    func testAStreamThatEndedLongAgoStillGetsASignedEndAfterManyStreams() async throws {
        let harness = try LiveHostHarness()
        defer { harness.remove() }
        try await harness.pair()
        let title = try await harness.eligibleTitle()
        let (firstID, _) = try await harness.started(title)
        for _ in 0..<17 {
            harness.clock.advance(by: 11)
            _ = try await harness.started(title)
        }
        let answer = try await harness.relay(RelayWireContract.playlistPath, streamID: firstID)
        XCTAssertEqual(answer.status, 410)
        XCTAssertTrue(answer.isSigned)
        XCTAssertEqual(answer.ended?.code, RelayLiveStreamEnded.notCurrentCode)
        await harness.host.stop()
    }

    func testReleaseDrainsOnlyItsOwnStartAndStillWorksAfterTheSigningWindow() async throws {
        let harness = try LiveHostHarness()
        defer { harness.remove() }
        try await harness.pair()
        let title = try await harness.eligibleTitle()
        let (_, reference) = try await harness.started(title)
        let other = try await harness.client.releaseLive(LiveStartReference(startID: UUID(), issuedAt: reference.issuedAt))
        XCTAssertFalse(other)
        let stream = try await harness.stream()
        var stops = await stream.stopCalls
        XCTAssertEqual(stops, [])

        harness.clock.advance(by: 80)
        let expiredCancel = try await harness.client.cancelLive(reference)
        XCTAssertEqual(expiredCancel, .expired)
        let drained = try await harness.client.releaseLive(reference)
        XCTAssertTrue(drained)
        stops = await stream.stopCalls
        XCTAssertEqual(stops, [.cancelled])
        let again = try await harness.client.releaseLive(reference)
        XCTAssertFalse(again)
        await harness.host.stop()
    }

    func testAStreamThatStopsByItselfReleasesTheSlotAndReportsWhy() async throws {
        let harness = try LiveHostHarness()
        defer { harness.remove() }
        try await harness.pair()
        let title = try await harness.eligibleTitle()
        let (streamID, _) = try await harness.started(title)
        let stream = try await harness.stream()
        await stream.stop(.idle)
        try await harness.waitUntilSlotIsEmpty()
        let status = try await harness.client.liveStatus(streamID: streamID)
        XCTAssertEqual(status.state, .stopped)
        XCTAssertEqual(status.stopReason, LiveRelayStopReason.idle.rawValue)
        let answer = try await harness.relay(RelayWireContract.playlistPath, streamID: streamID)
        XCTAssertEqual(answer.ended, RelayLiveStreamEnded(error: .streamEnded, code: "idle", message: nil))
        await harness.host.stop()
    }

    func testAFailedStreamReleasesTheSlotAndKeepsItsReason() async throws {
        let harness = try LiveHostHarness(streamStates: [.starting, .failed(code: "live_start_timeout", message: "Too slow.")])
        defer { harness.remove() }
        try await harness.pair()
        let title = try await harness.eligibleTitle()
        let (streamID, _) = try await harness.started(title)
        let stream = try await harness.stream()
        await stream.advance()
        try await harness.waitUntilSlotIsEmpty()
        let status = try await harness.client.liveStatus(streamID: streamID)
        XCTAssertEqual(status.state, .failed)
        XCTAssertEqual(status.failure, .init(code: "live_start_timeout", message: "Too slow."))
        await harness.host.stop()
    }

    func testQuitAndParentExpiryDrainAndAwaitTheWorker() async throws {
        let harness = try LiveHostHarness()
        defer { harness.remove() }
        try await harness.pair()
        let title = try await harness.eligibleTitle()
        _ = try await harness.started(title)
        let stream = try await harness.stream()
        await stream.holdStop()
        let quit = Task { await harness.host.stopForAppQuit() }
        let deadline = Date().addingTimeInterval(5)
        while await stream.stopCalls.isEmpty, Date() < deadline { try await Task.sleep(for: .milliseconds(5)) }
        try await Task.sleep(for: .milliseconds(50))
        let finishedEarly = await stream.isStopFinished
        XCTAssertFalse(finishedEarly)
        await stream.releaseStop()
        await quit.value
        let finished = await stream.isStopFinished
        XCTAssertTrue(finished, "quit returns only after the stream's cleanup")
        let stops = await stream.stopCalls
        XCTAssertEqual(stops.first, .appQuit)

        let expiring = try LiveHostHarness()
        defer { expiring.remove() }
        try await expiring.pair()
        let expiringTitle = try await expiring.eligibleTitle()
        _ = try await expiring.started(expiringTitle)
        let expiringStream = try await expiring.stream()
        expiring.clock.advance(by: 25 * 60 * 60)
        _ = await expiring.host.handle(MovieTestTransport.rawRequest(path: MovieLibraryContract.catalogPath), peer: .localNetwork)
        let expiryStops = await expiringStream.stopCalls
        XCTAssertEqual(expiryStops, [.sessionEnded])
        let empty = await expiring.admission.isEmpty
        XCTAssertTrue(empty)
        await expiring.host.stop()
    }

    private func assertAuthorization(
        _ authorizer: LiveSourceAuthorizer,
        _ candidate: LiveCatalogCandidate,
        fails expected: LiveSourceAuthorizationError,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async {
        do {
            _ = try await authorizer.authorize(candidate, revision: candidate.revision)
            XCTFail("Expected authorization to fail", file: file, line: line)
        } catch {
            XCTAssertEqual(error as? LiveSourceAuthorizationError, expected, file: file, line: line)
        }
    }
}

// MARK: - Source I/O never blocks the app

@MainActor
private final class MainActorTicker {
    private(set) var ticks = 0
    private var task: Task<Void, Never>?

    func start() {
        task = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                self?.ticks += 1
                try? await Task.sleep(for: .milliseconds(10))
            }
        }
    }

    func stop() { task?.cancel() }
}

/// Stands in for an `open` or bookmark resolution on a network share that waits on macOS consent.
private let blockingFileCall: TimeInterval = 2

@MainActor
final class MovieLibrarySourceAccessTests: XCTestCase {
    func testABlockedRootOpenListsTheFolderUnavailableAfterTheLimitWithoutFreezingTheApp() async throws {
        let fixture = try MovieTestFixture()
        defer { fixture.remove() }
        let ticker = MainActorTicker()
        ticker.start()
        defer { ticker.stop() }
        let root = fixture.root
        let started = Date()

        let catalog = try await MovieCatalog.open(
            sources: [MovieSourceBookmark(id: fixture.rootID, name: "Share", bookmark: Data())],
            deadline: 0.3,
            resolve: { _ in
                Thread.sleep(forTimeInterval: blockingFileCall)
                return root
            }
        )
        let elapsed = Date().timeIntervalSince(started)

        XCTAssertLessThan(elapsed, 1.5, "the open gives up at its limit")
        XCTAssertGreaterThanOrEqual(ticker.ticks, 10, "the main actor kept running while the open was blocked")
        let snapshot = try await catalog.snapshot()
        XCTAssertEqual(snapshot.roots.map(\.isAvailable), [false])
        await catalog.stop()
    }

    func testABlockedSourceOpenFailsTheTitleAfterTheLimitWhileTheIndexStaysResponsive() async throws {
        let fixture = try MovieTestFixture()
        defer { fixture.remove() }
        try Data(repeating: 1, count: 256).write(to: fixture.root.appendingPathComponent("Share.iso"))
        let catalog = try fixture.catalog()
        let candidates = try await catalog.listing().liveCandidates
        var authorizer = LiveSourceAuthorizer(deadline: 0.3)
        authorizer.open = { root, components in
            Thread.sleep(forTimeInterval: blockingFileCall)
            return root.openFileReportingErrno(components)
        }
        let inspector = FakeLiveTitleInspector()
        let index = LiveTitleIndex(storeURL: nil, authorizer: authorizer, inspector: inspector)
        let ticker = MainActorTicker()
        ticker.start()
        defer { ticker.stop() }
        let started = Date()

        let first = await index.titles(for: candidates)
        XCTAssertEqual(first.map(\.state), [.checking])
        let whileChecking = Date()
        let second = await index.titles(for: candidates)
        XCTAssertEqual(second.map(\.state), [.checking])
        XCTAssertLessThan(Date().timeIntervalSince(whileChecking), 0.2, "the index answers while the open is blocked")
        await index.waitUntilIdle()
        let elapsed = Date().timeIntervalSince(started)

        XCTAssertLessThan(elapsed, 1.5, "the title fails at the limit, not when the open returns")
        XCTAssertGreaterThanOrEqual(ticker.ticks, 10)
        let after = await index.titles(for: candidates)
        let title = try XCTUnwrap(after.first)
        XCTAssertEqual(title.state, .ineligible)
        XCTAssertEqual(title.reason, LiveSourceAuthorizationError.unavailable.message)
        let inspections = await inspector.calls
        XCTAssertEqual(inspections, 0)
        await catalog.stop()
    }
}

/// An inspection whose worker keeps running after a cancel until the test lets it exit.
private actor GatedLiveTitleInspector: LiveTitleInspecting {
    private(set) var started = 0
    private(set) var exited = false
    private(set) var sawCancel = false
    private var exitGate: CheckedContinuation<Void, Never>?
    private var released = false

    func inspect(_ lease: LiveSourceLease) async throws -> LiveTitleInspection {
        started += 1
        await withTaskCancellationHandler {
            await waitForExit()
        } onCancel: {
            Task { await self.noteCancel() }
        }
        exited = true
        throw CancellationError()
    }

    func releaseExit() {
        released = true
        exitGate?.resume()
        exitGate = nil
    }

    private func noteCancel() { sawCancel = true }

    private func waitForExit() async {
        guard !released else { return }
        await withCheckedContinuation { exitGate = $0 }
    }
}

final class LiveTitleIndexLifecycleTests: XCTestCase {
    private func candidates(in fixture: MovieTestFixture) async throws -> (MovieCatalog, [LiveCatalogCandidate]) {
        try Data(repeating: 1, count: 256).write(to: fixture.root.appendingPathComponent("Disc.iso"))
        let catalog = try fixture.catalog()
        return (catalog, try await catalog.listing().liveCandidates)
    }

    func testStopWaitsForARunningInspectionsWorkerToExit() async throws {
        let fixture = try MovieTestFixture()
        defer { fixture.remove() }
        let (catalog, listed) = try await candidates(in: fixture)
        let inspector = GatedLiveTitleInspector()
        let index = LiveTitleIndex(storeURL: nil, inspector: inspector)
        _ = await index.titles(for: listed)
        let deadline = Date().addingTimeInterval(5)
        while await inspector.started == 0, Date() < deadline { try await Task.sleep(for: .milliseconds(5)) }

        let stopped = LockedBox()
        let stopping = Task {
            await index.stop()
            stopped.set()
        }
        while await !inspector.sawCancel, Date() < deadline { try await Task.sleep(for: .milliseconds(5)) }
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertFalse(stopped.value, "stop returns only after the worker exits")
        await inspector.releaseExit()
        await stopping.value
        let exited = await inspector.exited
        XCTAssertTrue(exited)
        await catalog.stop()
    }

    func testStopDuringAuthorizationNeverStartsTheWorker() async throws {
        let fixture = try MovieTestFixture()
        defer { fixture.remove() }
        let (catalog, listed) = try await candidates(in: fixture)
        var authorizer = LiveSourceAuthorizer(deadline: 5)
        authorizer.open = { root, components in
            Thread.sleep(forTimeInterval: 0.3)
            return root.openFileReportingErrno(components)
        }
        let inspector = FakeLiveTitleInspector()
        let index = LiveTitleIndex(storeURL: nil, authorizer: authorizer, inspector: inspector)
        _ = await index.titles(for: listed)
        try await Task.sleep(for: .milliseconds(50))
        await index.stop()
        let calls = await inspector.calls
        XCTAssertEqual(calls, 0)
        await catalog.stop()
    }

    func testTheCacheIsWrittenAndReadInTheBackgroundAndAnOversizedFileIsIgnored() async throws {
        let fixture = try MovieTestFixture()
        defer { fixture.remove() }
        let (catalog, listed) = try await candidates(in: fixture)
        let store = fixture.directory.appendingPathComponent("support/live-titles.v1.json")

        let first = LiveTitleIndex(storeURL: store, inspector: FakeLiveTitleInspector())
        _ = await first.titles(for: listed)
        await first.waitUntilIdle()
        await first.stop()
        XCTAssertTrue(FileManager.default.fileExists(atPath: store.path), "stop waits for the cache write")

        let secondInspector = FakeLiveTitleInspector()
        let second = LiveTitleIndex(storeURL: store, inspector: secondInspector)
        let cached = await second.titles(for: listed)
        XCTAssertEqual(cached.map(\.state), [.eligible])
        let calls = await secondInspector.calls
        XCTAssertEqual(calls, 0)
        await second.stop()

        // The same valid cache, padded past the limit: only the size bound refuses it.
        var padded = try Data(contentsOf: store)
        padded.append(Data(repeating: 0x20, count: LiveTitleIndex.maximumStoreBytes))
        try padded.write(to: store)
        let oversized = LiveTitleIndex(storeURL: store, inspector: FakeLiveTitleInspector())
        let fresh = await oversized.titles(for: listed)
        XCTAssertEqual(fresh.map(\.state), [.checking])
        await oversized.stop()
        await catalog.stop()
    }
}


private final class LockedFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    var isSet: Bool { lock.withLock { value } }
    func set() { lock.withLock { value = true } }
}

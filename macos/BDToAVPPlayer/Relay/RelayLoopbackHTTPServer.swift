import Foundation
import Network

/// AVFoundation must fetch HLS segments over HTTP. Only this device can reach
/// the listener; every upstream byte still passes through the signed client.
final class RelayLoopbackHTTPServer: @unchecked Sendable {
    typealias ResourceLoader = @Sendable (URL) async throws -> (Data, HTTPURLResponse)
    private let queue = DispatchQueue(label: "com.shinycomputers.bd-to-avp.player.loopback")
    private let listener: NWListener
    private let upstreamBaseURL: URL
    private let loadResource: ResourceLoader
    private let token = UUID().uuidString
    private var startup: CheckedContinuation<URL, Error>?
    private var stopped = false
    private var connections: [UUID: NWConnection] = [:]
    private var tasks: [UUID: Task<Void, Never>] = [:]

    private init(serverBaseURL: URL, loadResource: @escaping ResourceLoader) throws {
        upstreamBaseURL = serverBaseURL
        self.loadResource = loadResource
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: .any)
        listener = try NWListener(using: parameters)
    }

    static func start(serverBaseURL: URL, loadResource: @escaping ResourceLoader) async throws -> (RelayLoopbackHTTPServer, URL) {
        let server = try RelayLoopbackHTTPServer(serverBaseURL: serverBaseURL, loadResource: loadResource)
        let url: URL = try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<URL, Error>) in
                server.queue.async { [server] in
                    guard !server.stopped else {
                        continuation.resume(throwing: CancellationError())
                        return
                    }
                    server.startup = continuation
                    server.listener.stateUpdateHandler = { [weak server] state in
                        guard let server else { return }
                        switch state {
                        case .ready:
                            guard let port = server.listener.port,
                                  let url = URL(string: "http://127.0.0.1:\(port.rawValue)/\(server.token)\(RelayWireContract.playlistPath)")
                            else { server.stopOnQueue(); return }
                            server.startup?.resume(returning: url)
                            server.startup = nil
                        case .failed:
                            server.stopOnQueue()
                        default: break
                        }
                    }
                    server.listener.newConnectionHandler = { [weak server] connection in
                        guard let server else { connection.cancel(); return }
                        server.accept(connection)
                    }
                    server.listener.start(queue: server.queue)
                    server.queue.asyncAfter(deadline: .now() + 10) { [weak server] in
                        guard let server, server.startup != nil else { return }
                        server.stopOnQueue()
                    }
                }
            }
        } onCancel: {
            server.cancelAllRequests()
        }
        return (server, url)
    }

    func cancelAllRequests() {
        queue.async { self.stopOnQueue() }
    }

    deinit {
        listener.cancel()
        connections.values.forEach { $0.cancel() }
        tasks.values.forEach { $0.cancel() }
    }

    private func stopOnQueue() {
        stopped = true
        startup?.resume(throwing: URLError(.cancelled))
        startup = nil
        listener.cancel()
        connections.values.forEach { $0.cancel() }
        connections.removeAll()
        tasks.values.forEach { $0.cancel() }
        tasks.removeAll()
    }

    private func accept(_ connection: NWConnection) {
        guard !stopped, connections.count < 8 else { connection.cancel(); return }
        let id = UUID()
        connections[id] = connection
        connection.stateUpdateHandler = { [weak self] state in
            switch state {
            case .failed, .cancelled: self?.finish(id)
            default: break
            }
        }
        connection.start(queue: queue)
        queue.asyncAfter(deadline: .now() + 30) { [weak self] in self?.finish(id) }
        receive(connection, id: id, accumulated: Data())
    }

    private func finish(_ id: UUID) {
        connections.removeValue(forKey: id)?.cancel()
        tasks.removeValue(forKey: id)?.cancel()
    }

    private func receive(_ connection: NWConnection, id: UUID, accumulated: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 16 * 1024) { [weak self] data, _, complete, error in
            guard let self, self.connections[id] != nil else { return }
            guard error == nil else { self.finish(id); return }
            let bytes = accumulated + (data ?? Data())
            do {
                let request = try RelayHTTPParser.parse(bytes, limits: RelayHTTPParsingLimits(maximumBodyBytes: 0))
                self.tasks[id] = Task { [weak self] in
                    guard let self else { return }
                    let response = await self.response(to: request)
#if BD_TO_AVP_QUALIFICATION
                    FileHandle.standardOutput.write(Data("RELAY_QUALIFICATION media_response status=\(response.statusCode) bytes=\(response.body.count)\n".utf8))
#endif
                    self.queue.async { [weak self] in
                        guard let self, self.connections[id] != nil else { return }
                        connection.send(content: response.serialized(), completion: .contentProcessed { [weak self] _ in
                            self?.finish(id)
                        })
                    }
                }
            } catch RelayHTTPParseError.incomplete where !complete {
                self.receive(connection, id: id, accumulated: bytes)
            } catch {
                self.finish(id)
            }
        }
    }

    private func response(to request: RelayHTTPRequest) async -> RelayHTTPResponse {
        guard request.method == "GET" else { return .empty(statusCode: 405) }
        let prefix = "/\(token)"
        guard request.requestTarget.hasPrefix(prefix + "/"),
              let customBase = RelayHLSResourceLoader.customPlaylistURL(for: upstreamBaseURL),
              var components = URLComponents(url: customBase, resolvingAgainstBaseURL: false)
        else { return .empty(statusCode: 404) }
        let path = String(request.requestTarget.dropFirst(prefix.count))
        // Reject escaped/ambiguous targets before URLComponents can normalize them.
        guard !path.contains("%"), !path.contains("?"), !path.contains("#"),
              path.split(separator: "/", omittingEmptySubsequences: false).dropFirst().allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." })
        else { return .empty(statusCode: 404) }
        components.path = path
        guard let url = components.url,
              RelayHLSResourceLoader.resolveURL(url, serverBaseURL: upstreamBaseURL) != nil,
              path == RelayWireContract.playlistPath || path.hasPrefix(RelayWireContract.mediaPathPrefix)
        else { return .empty(statusCode: 404) }
        do {
            var (data, _) = try await loadResource(url)
            try Task.checkCancellation()
            let contentType: String
            if path == RelayWireContract.playlistPath {
                data = try Self.rewritePlaylist(data, prefix: prefix)
                contentType = "application/vnd.apple.mpegurl"
            } else {
                contentType = "video/mp4"
            }
            return try Self.mediaResponse(data: data, contentType: contentType, range: request.header(named: "range"))
        } catch {
            return .empty(statusCode: 503)
        }
    }

    /// The host emits only root-relative media identifiers. Reject any other
    /// URI instead of allowing AVFoundation to bypass response authentication.
    static func rewritePlaylist(_ data: Data, prefix: String) throws -> Data {
        guard let text = String(data: data, encoding: .utf8), text.hasPrefix("#EXTM3U\n") else {
            throw RelayTransportError.invalidRelayURL
        }
        func localPath(_ path: String) throws -> String {
            let base = URL(string: "http://localhost")!
            guard path.hasPrefix(RelayWireContract.mediaPathPrefix), !path.contains("%"),
                  let url = URL(string: "bdtoavprelay://localhost\(path)"),
                  RelayHLSResourceLoader.resolveURL(url, serverBaseURL: base) != nil
            else { throw RelayTransportError.invalidRelayURL }
            return prefix + path
        }
        let lines = try text.components(separatedBy: "\n").map { line in
            if line.hasPrefix("#EXT-X-MAP:URI=\""), line.hasSuffix("\"") {
                let path = String(line.dropFirst("#EXT-X-MAP:URI=\"".count).dropLast())
                return "#EXT-X-MAP:URI=\"\(try localPath(path))\""
            }
            if !line.isEmpty && !line.hasPrefix("#") { return try localPath(line) }
            guard !line.contains("URI=") else { throw RelayTransportError.invalidRelayURL }
            return line
        }
        return Data(lines.joined(separator: "\n").utf8)
    }

    static func mediaResponse(data: Data, contentType: String, range: String?) throws -> RelayHTTPResponse {
        var headers = ["content-type": contentType, "accept-ranges": "bytes"]
        guard let range else { return RelayHTTPResponse(statusCode: 200, headers: headers, body: data) }
        guard range.hasPrefix("bytes="), !data.isEmpty else {
            return RelayHTTPResponse(statusCode: 416, headers: ["content-range": "bytes */\(data.count)"])
        }
        let parts = range.dropFirst(6).split(separator: "-", omittingEmptySubsequences: false)
        guard parts.count == 2,
              parts.allSatisfy({ $0.utf8.allSatisfy { $0 >= 48 && $0 <= 57 } })
        else { return RelayHTTPResponse(statusCode: 416, headers: ["content-range": "bytes */\(data.count)"]) }
        let start: Int
        let end: Int
        if parts[0].isEmpty, let suffix = Int(parts[1]), suffix > 0 {
            start = max(0, data.count - suffix)
            end = data.count - 1
        } else if let lower = Int(parts[0]), lower < data.count,
                  parts[1].isEmpty || Int(parts[1]) != nil {
            start = lower
            end = min(Int(parts[1]) ?? (data.count - 1), data.count - 1)
        } else { return RelayHTTPResponse(statusCode: 416, headers: ["content-range": "bytes */\(data.count)"]) }
        guard end >= start else { return RelayHTTPResponse(statusCode: 416, headers: ["content-range": "bytes */\(data.count)"]) }
        headers["content-range"] = "bytes \(start)-\(end)/\(data.count)"
        return RelayHTTPResponse(statusCode: 206, headers: headers, body: data.subdata(in: start ..< end + 1))
    }
}

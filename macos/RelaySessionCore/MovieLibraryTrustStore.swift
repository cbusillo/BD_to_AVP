import CryptoKit
import Foundation
import Security

struct MovieTrustedPeer: Codable, Identifiable, Equatable, Sendable {
    let publicKey: Data
    let name: String
    var id: String { MovieLibraryContract.digest(publicKey) }
}

/// A single Keychain item makes a trust update atomic. Never reset it on an access error.
final class MovieLibraryTrustStore: @unchecked Sendable {
    private struct PeerCredential: Codable {
        let peer: MovieTrustedPeer
        let privateKey: Data
    }
    private struct Document: Codable {
        var version = 1
        var hostPrivateKey: Data?
        var clients: [MovieTrustedPeer] = []
        var servers: [PeerCredential] = []
    }
    private let lock = NSLock()
    private let read: @Sendable () throws -> Data?
    private let write: @Sendable (Data) throws -> Void

    init(service: String = "com.shinycomputers.bd-to-avp.movie-sharing.v1") {
        // Construct each query in its callback; no non-Sendable dictionary crosses executors.
        read = {
            let attributes: [String: Any] = [
                kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                kSecAttrAccount as String: "identity-and-trust", kSecAttrSynchronizable as String: false,
                kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne,
            ]
            var result: CFTypeRef?
            let status = SecItemCopyMatching(attributes as CFDictionary, &result)
            if status == errSecItemNotFound { return nil }
            guard status == errSecSuccess, let data = result as? Data else {
                throw MovieLibraryError.credentialStore(status)
            }
            return data
        }
        write = { data in
            let attributes: [String: Any] = [
                kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                kSecAttrAccount as String: "identity-and-trust", kSecAttrSynchronizable as String: false,
            ]
            let values: [String: Any] = [
                kSecValueData as String: data,
                kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            ]
            var status = SecItemUpdate(attributes as CFDictionary, values as CFDictionary)
            if status == errSecItemNotFound {
                status = SecItemAdd(attributes.merging(values) { _, new in new } as CFDictionary, nil)
            }
            guard status == errSecSuccess else { throw MovieLibraryError.credentialStore(status) }
        }
    }

    init(read: @escaping @Sendable () throws -> Data?, write: @escaping @Sendable (Data) throws -> Void) {
        self.read = read
        self.write = write
    }

    private func load() throws -> Document {
        guard let data = try read() else { return Document() }
        guard data.count <= 64 * 1_024, let document = try? JSONDecoder().decode(Document.self, from: data),
              document.version == 1,
              document.hostPrivateKey.map({ $0.count == 32 }) ?? true,
              document.clients.count <= MovieLibraryContract.maximumPeers,
              document.servers.count <= MovieLibraryContract.maximumPeers,
              Set(document.clients.map(\.id)).count == document.clients.count,
              Set(document.servers.map(\.peer.id)).count == document.servers.count,
              document.clients.allSatisfy({ $0.publicKey.count == 32 && $0.name.utf8.count <= 512 }),
              document.servers.allSatisfy({ $0.privateKey.count == 32 && $0.peer.publicKey.count == 32 && $0.peer.name.utf8.count <= 512 })
        else { throw MovieLibraryError.credentialStore(errSecDecode) }
        return document
    }

    func hostPrivateKey() throws -> Data {
        try lock.withLock {
            var document = try load()
            if let key = document.hostPrivateKey { return key }
            let key = Curve25519.KeyAgreement.PrivateKey().rawRepresentation
            document.hostPrivateKey = key
            try write(JSONEncoder().encode(document))
            return key
        }
    }

    func trustedClients() throws -> [MovieTrustedPeer] { try lock.withLock { try load().clients } }
    func trustedServers() throws -> [MovieTrustedPeer] { try lock.withLock { try load().servers.map(\.peer) } }

    func clientPrivateKey(for serverPublicKey: Data) throws -> Data? {
        try lock.withLock { try load().servers.first { $0.peer.publicKey == serverPublicKey }?.privateKey }
    }

    func trustClient(publicKey: Data, name: String) throws {
        try lock.withLock {
            guard publicKey.count == 32 else { throw MovieLibraryError.invalidResponse }
            var document = try load()
            document.clients.removeAll { $0.publicKey == publicKey }
            guard document.clients.count < MovieLibraryContract.maximumPeers else { throw MovieLibraryError.unavailable }
            document.clients.append(MovieTrustedPeer(publicKey: publicKey, name: String(name.prefix(100))))
            try write(JSONEncoder().encode(document))
        }
    }

    func trustServer(publicKey: Data, name: String, clientPrivateKey: Data) throws {
        try lock.withLock {
            guard publicKey.count == 32, clientPrivateKey.count == 32 else { throw MovieLibraryError.invalidResponse }
            var document = try load()
            document.servers.removeAll { $0.peer.publicKey == publicKey }
            guard document.servers.count < MovieLibraryContract.maximumPeers else { throw MovieLibraryError.unavailable }
            document.servers.append(PeerCredential(peer: MovieTrustedPeer(publicKey: publicKey, name: String(name.prefix(100))), privateKey: clientPrivateKey))
            try write(JSONEncoder().encode(document))
        }
    }

    func forgetClient(id: String) throws {
        try lock.withLock {
            var document = try load()
            document.clients.removeAll { $0.id == id }
            try write(JSONEncoder().encode(document))
        }
    }

    func forgetServer(publicKey: Data) throws {
        try lock.withLock {
            var document = try load()
            document.servers.removeAll { $0.peer.publicKey == publicKey }
            try write(JSONEncoder().encode(document))
        }
    }
}

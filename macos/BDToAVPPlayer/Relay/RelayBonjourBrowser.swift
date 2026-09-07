import Foundation
import Network

struct RelayDiscoveredEndpoint: Sendable, Identifiable, Equatable {
    let id: String
    let displayName: String
    let baseURL: URL
}

protocol RelayEndpointBrowsing: AnyObject {
    var discoveryStream: AsyncStream<[RelayDiscoveredEndpoint]> { get }
    func startBrowsing()
    func stopBrowsing()
}

private struct RelayBonjourDescriptor: Hashable {
    let name: String
    let type: String
    let domain: String
}

enum RelayBonjourProtocolFilter {
    static func accepts(_ txtRecord: NWTXTRecord?) -> Bool {
        guard let txtRecord else { return true }
        guard let entry = txtRecord.getEntry(for: "v"), let version = stringValue(of: entry) else {
            return false
        }
        return version == String(RelayWireContract.protocolVersion)
    }

    private static func stringValue(of entry: NWTXTRecord.Entry) -> String? {
        switch entry {
        case let .string(value):
            value
        case let .data(data):
            String(data: data, encoding: .utf8)
        case .empty, .none:
            nil
        @unknown default:
            nil
        }
    }
}

enum RelayBonjourEndpointFactory {
    static func endpoint(
        name: String,
        type: String,
        domain: String,
        addresses: [Data],
        hostName: String?,
        port: Int
    ) -> RelayDiscoveredEndpoint? {
        guard port > 0 else { return nil }
        let numericHosts = addresses.compactMap(numericHost(from:))
        let url = numericHosts.first(where: { $0.family == AF_INET }).flatMap { url(host: $0.host, port: port) }
            ?? numericHosts.first(where: { $0.family == AF_INET6 }).flatMap { url(host: $0.host, port: port) }
            ?? normalized(hostName).flatMap { url(host: $0, port: port) }
        guard let url else { return nil }
        return RelayDiscoveredEndpoint(
            id: "\(name).\(type).\(domain)",
            displayName: name,
            baseURL: url
        )
    }

    private static func numericHost(from data: Data) -> (family: Int32, host: String)? {
        data.withUnsafeBytes { bytes in
            guard let baseAddress = bytes.baseAddress, bytes.count >= MemoryLayout<sockaddr>.size else { return nil }
            let socketAddress = baseAddress.assumingMemoryBound(to: sockaddr.self)
            let addressLength = Int(socketAddress.pointee.sa_len)
            guard addressLength > 0, addressLength <= bytes.count else { return nil }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            guard getnameinfo(
                socketAddress,
                socklen_t(addressLength),
                &host,
                socklen_t(host.count),
                nil,
                0,
                NI_NUMERICHOST
            ) == 0 else { return nil }
            let value = String(cString: host)
            guard value != "0.0.0.0", value != "::" else { return nil }
            return (Int32(socketAddress.pointee.sa_family), value)
        }
    }

    private static func normalized(_ hostName: String?) -> String? {
        guard let value = hostName?.trimmingCharacters(in: CharacterSet(charactersIn: ".")), !value.isEmpty else {
            return nil
        }
        return value
    }

    private static func url(host: String, port: Int) -> URL? {
        if host.contains(":") {
            let escapedHost = host.replacingOccurrences(of: "%", with: "%25")
            return URL(string: "http://[\(escapedHost)]:\(port)")
        }
        var components = URLComponents()
        components.scheme = "http"
        components.host = host
        components.port = port
        return components.url
    }
}

private final class RelayBonjourDescriptorResolver: NSObject, NetServiceDelegate {
    private var services: [ObjectIdentifier: NetService] = [:]
    private var completions: [ObjectIdentifier: (RelayDiscoveredEndpoint?) -> Void] = [:]

    func resolve(_ descriptors: [RelayBonjourDescriptor], completion: @escaping ([RelayDiscoveredEndpoint]) -> Void) {
        guard !descriptors.isEmpty else {
            completion([])
            return
        }
        let lock = NSLock()
        var remaining = descriptors.count
        var endpoints: [RelayDiscoveredEndpoint] = []
        for descriptor in descriptors {
            let service = NetService(domain: descriptor.domain, type: descriptor.type, name: descriptor.name)
            let identifier = ObjectIdentifier(service)
            services[identifier] = service
            completions[identifier] = { endpoint in
                lock.withLock {
                    if let endpoint { endpoints.append(endpoint) }
                    remaining -= 1
                    if remaining == 0 {
                        completion(endpoints.sorted { $0.id < $1.id })
                    }
                }
            }
            service.delegate = self
            service.resolve(withTimeout: 5)
        }
    }

    func netServiceDidResolveAddress(_ sender: NetService) {
        finish(sender, endpoint: endpoint(for: sender))
    }

    func netService(_ sender: NetService, didNotResolve errorDict: [String: NSNumber]) {
        finish(sender, endpoint: nil)
    }

    private func endpoint(for service: NetService) -> RelayDiscoveredEndpoint? {
        RelayBonjourEndpointFactory.endpoint(
            name: service.name,
            type: service.type,
            domain: service.domain,
            addresses: service.addresses ?? [],
            hostName: service.hostName,
            port: service.port
        )
    }

    private func finish(_ service: NetService, endpoint: RelayDiscoveredEndpoint?) {
        let identifier = ObjectIdentifier(service)
        service.stop()
        services.removeValue(forKey: identifier)
        completions.removeValue(forKey: identifier)?(endpoint)
    }
}

final class RelayBonjourBrowser: RelayEndpointBrowsing {
    static let serviceType = RelayWireContract.bonjourServiceType

    let discoveryStream: AsyncStream<[RelayDiscoveredEndpoint]>
    private let continuation: AsyncStream<[RelayDiscoveredEndpoint]>.Continuation
    private let browser: NWBrowser
    private let queue: DispatchQueue
    private let resolver = RelayBonjourDescriptorResolver()
    private var discoveryGeneration = 0

    init(queue: DispatchQueue = .global(qos: .utility)) {
        self.queue = queue
        let parameters = NWParameters.tcp
        parameters.includePeerToPeer = true
        browser = NWBrowser(
            for: .bonjourWithTXTRecord(type: Self.serviceType, domain: nil),
            using: parameters
        )

        var streamContinuation: AsyncStream<[RelayDiscoveredEndpoint]>.Continuation!
        discoveryStream = AsyncStream { streamContinuation = $0 }
        continuation = streamContinuation
        browser.browseResultsChangedHandler = { [weak self] results, _ in
            let descriptors = results.compactMap { result -> RelayBonjourDescriptor? in
                guard case let .service(name: name, type: type, domain: domain, interface: _) = result.endpoint else { return nil }
                let txtRecord: NWTXTRecord? = if case let .bonjour(record) = result.metadata { record } else { nil }
                guard RelayBonjourProtocolFilter.accepts(txtRecord) else { return nil }
                return RelayBonjourDescriptor(name: name, type: type, domain: domain)
            }
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                discoveryGeneration &+= 1
                let generation = discoveryGeneration
                resolver.resolve(Array(Set(descriptors))) { [weak self] endpoints in
                    guard let self, discoveryGeneration == generation else { return }
                    continuation.yield(endpoints)
                }
            }
        }
        browser.stateUpdateHandler = { [weak self] state in
            if case .failed = state { self?.continuation.finish() }
        }
    }

    func startBrowsing() {
        browser.start(queue: queue)
    }

    func stopBrowsing() {
        browser.cancel()
        continuation.finish()
    }
}

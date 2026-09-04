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
        guard let host = service.hostName?.trimmingCharacters(in: CharacterSet(charactersIn: ".")),
              !host.isEmpty,
              service.port > 0,
              let url = URL(string: "http://\(host):\(service.port)")
        else { return nil }
        return RelayDiscoveredEndpoint(
            id: "\(service.name).\(service.type).\(service.domain)",
            displayName: service.name,
            baseURL: url
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
        let capturedContinuation = streamContinuation!

        browser.browseResultsChangedHandler = { [weak resolver] results, _ in
            let descriptors = results.compactMap { result -> RelayBonjourDescriptor? in
                guard case let .service(name: name, type: type, domain: domain, interface: _) = result.endpoint else { return nil }
                guard case let .bonjour(txtRecord) = result.metadata,
                      case let .string(version)? = txtRecord.getEntry(for: "v"),
                      version == String(RelayWireContract.protocolVersion)
                else { return nil }
                return RelayBonjourDescriptor(name: name, type: type, domain: domain)
            }
            DispatchQueue.main.async {
                resolver?.resolve(descriptors) { endpoints in
                    capturedContinuation.yield(endpoints)
                }
            }
        }
        browser.stateUpdateHandler = { state in
            if case .failed = state { capturedContinuation.finish() }
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

import Darwin
import Foundation
import Network

enum RelayNetworkServerError: Error, Equatable, Sendable {
    case unavailablePairingContext
    case invalidBonjourMetadata
}

final class RelayNetworkServer: @unchecked Sendable {
    private static let requestTimeout: TimeInterval = 10

    private let host: RelayHost
    private let queue: DispatchQueue
    private let resources: RelayNetworkServerResources
    private let lifecyclePollInterval: Duration

    private init(
        host: RelayHost,
        listener: NWListener,
        queue: DispatchQueue,
        lifecyclePollInterval: Duration
    ) {
        self.host = host
        self.queue = queue
        resources = RelayNetworkServerResources(
            queue: queue,
            listenerCancellation: { listener.cancel() },
            requestTimeout: Self.requestTimeout
        )
        self.lifecyclePollInterval = lifecyclePollInterval
    }

    static func start(
        host: RelayHost,
        serviceName: String = Host.current().localizedName ?? "BD to AVP",
        queue: DispatchQueue = DispatchQueue(label: "com.shinycomputers.bd-to-avp.relay", qos: .userInitiated),
        lifecyclePollInterval: Duration = .milliseconds(250)
    ) async throws -> RelayNetworkServer {
        guard let advertisement = await host.advertisedBonjourService() else {
            throw RelayNetworkServerError.unavailablePairingContext
        }
        guard advertisement.serviceType == RelayWireContract.bonjourServiceType,
              advertisement.txtRecord.count <= 255
        else {
            throw RelayNetworkServerError.invalidBonjourMetadata
        }

        let parameters = NWParameters.tcp
        parameters.includePeerToPeer = true
        let listener = try NWListener(using: parameters, on: .any)
        listener.service = NWListener.Service(
            name: String(serviceName.prefix(63)),
            type: advertisement.serviceType,
            domain: nil,
            txtRecord: advertisement.txtRecord
        )
        let server = RelayNetworkServer(
            host: host,
            listener: listener,
            queue: queue,
            lifecyclePollInterval: lifecyclePollInterval
        )
        listener.newConnectionHandler = { [weak server] connection in
            server?.accept(connection)
        }
        listener.stateUpdateHandler = { [weak server] state in
            guard case .failed = state else {
                return
            }
            server?.handleNetworkLoss()
        }
        listener.start(queue: queue)
        await server.startLifecycleMonitor()
        return server
    }

    func stopForAppQuit() async {
        await cancelNetworkResources()
        await host.stopForAppQuit()
    }

    func cancel() async {
        await cancelNetworkResources()
        await host.cancel()
    }

    func stop() async {
        await cancelNetworkResources()
        await host.stop()
    }

    private func accept(_ connection: NWConnection) {
        let peer = RelayNetworkPeerClassifier.classify(connection.endpoint)
        Task { [weak self] in
            guard let self,
                  peer == .localNetwork,
                  let identifier = await resources.register(cancellation: connection.cancel)
            else {
                connection.cancel()
                return
            }
            connection.start(queue: queue)
            receiveRequest(on: connection, identifier: identifier, accumulated: Data(), peer: peer)
        }
    }

    private func receiveRequest(
        on connection: NWConnection,
        identifier: UUID,
        accumulated: Data,
        peer: RelayHostPeer
    ) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1_024) { [weak self] content, _, _, error in
            guard let self else {
                connection.cancel()
                return
            }
            guard error == nil else {
                self.resources.finish(identifier)
                return
            }
            let nextData = accumulated + (content ?? Data())
            Task {
                if await self.host.needsMoreRequestBytes(nextData) {
                    self.receiveRequest(on: connection, identifier: identifier, accumulated: nextData, peer: peer)
                    return
                }
                let response = await self.host.handle(nextData, peer: peer)
                connection.send(content: response.serialized(), completion: .contentProcessed { [weak self] _ in
                    self?.resources.finish(identifier)
                })
            }
        }
    }

    private func handleNetworkLoss() {
        Task { [weak self] in
            await self?.networkLost()
        }
    }

    func networkLost() async {
        await host.networkLost()
        await cancelNetworkResources()
    }

    private func startLifecycleMonitor() async {
        let lifecyclePollInterval = lifecyclePollInterval
        let monitor = Task { [weak self, host] in
            while !Task.isCancelled {
                let lifecycle = await host.currentLifecycle()
                switch lifecycle {
                case .cancelled, .expired, .stopped:
                    await self?.cancelNetworkResources()
                    return
                case .pairing, .paired, .finished:
                    break
                }
                try? await Task.sleep(for: lifecyclePollInterval)
            }
        }
        await resources.setLifecycleMonitor(monitor)
    }

    private func cancelNetworkResources() async {
        await resources.cancelNetworkResources()
    }

    func networkResourcesAreCancelled() async -> Bool {
        await resources.isCancelled()
    }

    deinit {
        resources.cancelNetworkResourcesSynchronously()
    }
}

final class RelayNetworkServerResources: @unchecked Sendable {
    private static let maximumConcurrentConnections = 16

    private struct ActiveConnection {
        let cancellation: @Sendable () -> Void
        let timeout: DispatchWorkItem
    }

    private let queue: DispatchQueue
    private let queueKey = DispatchSpecificKey<Void>()
    private let listenerCancellation: @Sendable () -> Void
    private let requestTimeout: TimeInterval
    private var activeConnections: [UUID: ActiveConnection] = [:]
    private var lifecycleMonitor: Task<Void, Never>?
    private var cancelled = false

    init(
        queue: DispatchQueue,
        listenerCancellation: @escaping @Sendable () -> Void,
        requestTimeout: TimeInterval = 10
    ) {
        self.queue = queue
        self.listenerCancellation = listenerCancellation
        self.requestTimeout = requestTimeout
        queue.setSpecific(key: queueKey, value: ())
    }

    func register(cancellation: @escaping @Sendable () -> Void) async -> UUID? {
        await withCheckedContinuation { continuation in
            queue.async { [weak self] in
                guard let self,
                      !cancelled,
                      activeConnections.count < Self.maximumConcurrentConnections
                else {
                    cancellation()
                    continuation.resume(returning: nil)
                    return
                }
                let identifier = UUID()
                let timeout = DispatchWorkItem { [weak self] in
                    self?.finishOnQueue(identifier)
                }
                activeConnections[identifier] = ActiveConnection(cancellation: cancellation, timeout: timeout)
                queue.asyncAfter(deadline: .now() + requestTimeout, execute: timeout)
                continuation.resume(returning: identifier)
            }
        }
    }

    func finish(_ identifier: UUID) {
        queue.async { [weak self] in
            self?.finishOnQueue(identifier)
        }
    }

    func setLifecycleMonitor(_ monitor: Task<Void, Never>) async {
        await performOnQueue {
            guard !self.cancelled else {
                monitor.cancel()
                return
            }
            self.lifecycleMonitor?.cancel()
            self.lifecycleMonitor = monitor
        }
    }

    func cancelNetworkResources() async {
        await performOnQueue {
            self.cancelNetworkResourcesOnQueue()
        }
    }

    func cancelNetworkResourcesSynchronously() {
        if DispatchQueue.getSpecific(key: queueKey) != nil {
            cancelNetworkResourcesOnQueue()
        } else {
            queue.sync {
                cancelNetworkResourcesOnQueue()
            }
        }
    }

    func activeConnectionCount() async -> Int {
        await withCheckedContinuation { continuation in
            queue.async { [weak self] in
                continuation.resume(returning: self?.activeConnections.count ?? 0)
            }
        }
    }

    func isCancelled() async -> Bool {
        await withCheckedContinuation { continuation in
            queue.async { [weak self] in
                continuation.resume(returning: self?.cancelled ?? true)
            }
        }
    }

    private func performOnQueue(_ operation: @escaping @Sendable () -> Void) async {
        if DispatchQueue.getSpecific(key: queueKey) != nil {
            operation()
            return
        }
        await withCheckedContinuation { continuation in
            queue.async {
                operation()
                continuation.resume()
            }
        }
    }

    private func finishOnQueue(_ identifier: UUID) {
        guard let activeConnection = activeConnections.removeValue(forKey: identifier) else {
            return
        }
        activeConnection.timeout.cancel()
        activeConnection.cancellation()
    }

    private func cancelNetworkResourcesOnQueue() {
        guard !cancelled else {
            return
        }
        cancelled = true
        lifecycleMonitor?.cancel()
        lifecycleMonitor = nil
        listenerCancellation()
        let connections = activeConnections.values
        activeConnections.removeAll()
        for activeConnection in connections {
            activeConnection.timeout.cancel()
            activeConnection.cancellation()
        }
    }
}

enum RelayNetworkPeerClassifier {
    static func classify(_ endpoint: NWEndpoint) -> RelayHostPeer {
        guard case let .hostPort(host, _) = endpoint else {
            return .nonLocal
        }
        return classify(address: String(describing: host))
    }

    static func classify(address rawAddress: String) -> RelayHostPeer {
        let address = rawAddress
            .trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
            .split(separator: "%", maxSplits: 1, omittingEmptySubsequences: true)
            .first
            .map(String.init) ?? rawAddress
        if address == "::1" || address.hasPrefix("127.") {
            return .loopback
        }
        if isOnActiveLocalInterface(address) {
            return .localNetwork
        }
        return .nonLocal
    }

    private static func isOnActiveLocalInterface(_ address: String) -> Bool {
        if address.contains(":") {
            return isOnActiveIPv6Interface(address)
        }
        return isOnActiveIPv4Interface(address)
    }

    private static func isOnActiveIPv4Interface(_ address: String) -> Bool {
        var remote = in_addr()
        guard inet_pton(AF_INET, address, &remote) == 1,
              let interfaces = activeInterfaces()
        else {
            return false
        }
        defer { freeifaddrs(interfaces) }
        var current: UnsafeMutablePointer<ifaddrs>? = interfaces
        while let interface = current {
            defer { current = interface.pointee.ifa_next }
            guard isActiveNonLoopback(interface),
                  let localAddress = interface.pointee.ifa_addr,
                  let netmask = interface.pointee.ifa_netmask,
                  localAddress.pointee.sa_family == UInt8(AF_INET),
                  netmask.pointee.sa_family == UInt8(AF_INET)
            else {
                continue
            }
            let local = UnsafeRawPointer(localAddress).assumingMemoryBound(to: sockaddr_in.self).pointee.sin_addr.s_addr
            let mask = UnsafeRawPointer(netmask).assumingMemoryBound(to: sockaddr_in.self).pointee.sin_addr.s_addr
            if (remote.s_addr & mask) == (local & mask) {
                return true
            }
        }
        return false
    }

    private static func isOnActiveIPv6Interface(_ address: String) -> Bool {
        var remote = in6_addr()
        guard inet_pton(AF_INET6, address, &remote) == 1,
              let interfaces = activeInterfaces()
        else {
            return false
        }
        defer { freeifaddrs(interfaces) }
        let remoteBytes = withUnsafeBytes(of: remote) { Array($0) }
        var current: UnsafeMutablePointer<ifaddrs>? = interfaces
        while let interface = current {
            defer { current = interface.pointee.ifa_next }
            guard isActiveNonLoopback(interface),
                  let localAddress = interface.pointee.ifa_addr,
                  let netmask = interface.pointee.ifa_netmask,
                  localAddress.pointee.sa_family == UInt8(AF_INET6),
                  netmask.pointee.sa_family == UInt8(AF_INET6)
            else {
                continue
            }
            let local = UnsafeRawPointer(localAddress).assumingMemoryBound(to: sockaddr_in6.self).pointee.sin6_addr
            let mask = UnsafeRawPointer(netmask).assumingMemoryBound(to: sockaddr_in6.self).pointee.sin6_addr
            let localBytes = withUnsafeBytes(of: local) { Array($0) }
            let maskBytes = withUnsafeBytes(of: mask) { Array($0) }
            if zip(zip(remoteBytes, localBytes), maskBytes).allSatisfy({ pair, maskByte in
                (pair.0 & maskByte) == (pair.1 & maskByte)
            }) {
                return true
            }
        }
        return false
    }

    private static func activeInterfaces() -> UnsafeMutablePointer<ifaddrs>? {
        var interfaces: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&interfaces) == 0 else {
            return nil
        }
        return interfaces
    }

    private static func isActiveNonLoopback(_ interface: UnsafeMutablePointer<ifaddrs>) -> Bool {
        let flags = interface.pointee.ifa_flags
        return (flags & UInt32(IFF_UP)) != 0 && (flags & UInt32(IFF_LOOPBACK)) == 0
    }
}

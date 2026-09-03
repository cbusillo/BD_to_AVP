import Darwin
import Foundation
import Network

enum RelayNetworkServerError: Error, Equatable, Sendable {
    case unavailablePairingContext
    case invalidBonjourMetadata
}

final class RelayNetworkServer: @unchecked Sendable {
    private let host: RelayHost
    private let listener: NWListener
    private let queue: DispatchQueue

    private init(host: RelayHost, listener: NWListener, queue: DispatchQueue) {
        self.host = host
        self.listener = listener
        self.queue = queue
    }

    static func start(
        host: RelayHost,
        serviceName: String = Host.current().localizedName ?? "BD to AVP",
        queue: DispatchQueue = DispatchQueue(label: "com.shinycomputers.bd-to-avp.relay", qos: .userInitiated)
    ) async throws -> RelayNetworkServer {
        guard let advertisement = await host.advertisedBonjourService() else {
            throw RelayNetworkServerError.unavailablePairingContext
        }
        guard advertisement.serviceType == RelayHostConfiguration.bonjourServiceType,
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
        let server = RelayNetworkServer(host: host, listener: listener, queue: queue)
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
        return server
    }

    func stopForAppQuit() {
        listener.cancel()
        Task {
            await host.stopForAppQuit()
        }
    }

    func cancel() {
        listener.cancel()
        Task {
            await host.cancel()
        }
    }

    private func accept(_ connection: NWConnection) {
        let peer = RelayNetworkPeerClassifier.classify(connection.endpoint)
        guard peer == .localNetwork else {
            connection.cancel()
            return
        }
        connection.start(queue: queue)
        receiveRequest(on: connection, accumulated: Data(), peer: peer)
    }

    private func receiveRequest(on connection: NWConnection, accumulated: Data, peer: RelayHostPeer) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1_024) { [weak self] content, _, _, error in
            guard let self else {
                connection.cancel()
                return
            }
            guard error == nil else {
                connection.cancel()
                return
            }
            let nextData = accumulated + (content ?? Data())
            Task {
                if await self.host.needsMoreRequestBytes(nextData) {
                    self.receiveRequest(on: connection, accumulated: nextData, peer: peer)
                    return
                }
                let response = await self.host.handle(nextData, peer: peer)
                connection.send(content: response.serialized(), completion: .contentProcessed { _ in
                    connection.cancel()
                })
            }
        }
    }

    private func handleNetworkLoss() {
        Task {
            await host.networkLost()
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

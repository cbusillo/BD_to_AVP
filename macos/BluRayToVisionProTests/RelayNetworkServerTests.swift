import Foundation
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class RelayNetworkServerTests: XCTestCase {
    func testResourceCoordinatorEnforcesSixteenConnectionCap() async {
        let cancellations = RelayNetworkServerCancellationRecorder()
        let resources = RelayNetworkServerResources(
            queue: DispatchQueue(label: "com.shinycomputers.bd-to-avp.relay-tests.cap"),
            listenerCancellation: { cancellations.recordListenerCancellation() },
            requestTimeout: 60
        )

        let admitted = await withTaskGroup(of: Bool.self, returning: [Bool].self) { group in
            for _ in 0..<17 {
                group.addTask {
                    await resources.register(cancellation: { cancellations.recordConnectionCancellation() }) != nil
                }
            }
            var results: [Bool] = []
            for await result in group {
                results.append(result)
            }
            return results
        }

        XCTAssertEqual(admitted.filter { $0 }.count, 16)
        let activeConnectionCount = await resources.activeConnectionCount()
        XCTAssertEqual(activeConnectionCount, 16)
        XCTAssertEqual(cancellations.connectionCancellationCount, 1)

        await resources.cancelNetworkResources()

        XCTAssertEqual(cancellations.listenerCancellationCount, 1)
        XCTAssertEqual(cancellations.connectionCancellationCount, 17)
    }

    func testResourceCoordinatorCancelsSlowClientAtTimeout() async {
        let cancellations = RelayNetworkServerCancellationRecorder()
        let resources = RelayNetworkServerResources(
            queue: DispatchQueue(label: "com.shinycomputers.bd-to-avp.relay-tests.timeout"),
            listenerCancellation: { cancellations.recordListenerCancellation() },
            requestTimeout: 0.01
        )

        let identifier = await resources.register(cancellation: { cancellations.recordConnectionCancellation() })
        XCTAssertNotNil(identifier)
        let didCancelSlowClient = await waitUntil {
            let activeConnectionCount = await resources.activeConnectionCount()
            return cancellations.connectionCancellationCount == 1 && activeConnectionCount == 0
        }
        XCTAssertTrue(didCancelSlowClient)
        XCTAssertEqual(cancellations.listenerCancellationCount, 0)
    }

    func testLifecycleTeardownCancelsMonitorListenerAndConnectionsOnce() async {
        let cancellations = RelayNetworkServerCancellationRecorder()
        let resources = RelayNetworkServerResources(
            queue: DispatchQueue(label: "com.shinycomputers.bd-to-avp.relay-tests.lifecycle"),
            listenerCancellation: { cancellations.recordListenerCancellation() },
            requestTimeout: 60
        )
        let monitor: Task<Void, Never> = Task {
            _ = try? await Task.sleep(for: .seconds(60))
        }

        await resources.setLifecycleMonitor(monitor)
        let identifier = await resources.register(cancellation: { cancellations.recordConnectionCancellation() })
        XCTAssertNotNil(identifier)
        await resources.cancelNetworkResources()

        XCTAssertTrue(monitor.isCancelled)
        let resourcesAreCancelled = await resources.isCancelled()
        XCTAssertTrue(resourcesAreCancelled)
        XCTAssertEqual(cancellations.listenerCancellationCount, 1)
        XCTAssertEqual(cancellations.connectionCancellationCount, 1)
    }

    func testConcurrentStopAndNetworkLossAreIdempotent() async throws {
        let fixture = try makeHostFixture()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let server = try await RelayNetworkServer.start(
            host: fixture.host,
            serviceName: "RelayNetworkServerTests",
            lifecyclePollInterval: .seconds(60)
        )

        await withTaskGroup(of: Void.self) { group in
            group.addTask { await server.stop() }
            group.addTask { await server.cancel() }
            group.addTask { await server.stopForAppQuit() }
            group.addTask { await server.networkLost() }
        }

        let resourcesAreCancelled = await server.networkResourcesAreCancelled()
        XCTAssertTrue(resourcesAreCancelled)
        let lifecycle = await fixture.host.currentLifecycle()
        XCTAssertTrue([.cancelled, .expired, .stopped].contains(lifecycle))
    }

    func testLifecycleExpiryTearsDownNetworkResources() async throws {
        let now = RelayNetworkServerTestClock(Date(timeIntervalSince1970: 1_700_000_000))
        let fixture = try makeHostFixture(challengeTTL: 1, now: { now.value() })
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let server = try await RelayNetworkServer.start(
            host: fixture.host,
            serviceName: "RelayNetworkServerExpiryTests",
            lifecyclePollInterval: .milliseconds(1)
        )

        now.set(Date(timeIntervalSince1970: 1_700_000_002))

        let didTearDown = await waitUntil { await server.networkResourcesAreCancelled() }
        XCTAssertTrue(didTearDown)
        let lifecycle = await fixture.host.currentLifecycle()
        XCTAssertEqual(lifecycle, .expired)
    }

    private func waitUntil(
        timeoutIterations: Int = 100,
        condition: @escaping @Sendable () async -> Bool
    ) async -> Bool {
        for _ in 0..<timeoutIterations {
            if await condition() {
                return true
            }
            try? await Task.sleep(for: .milliseconds(10))
        }
        return false
    }

    private func makeHostFixture(
        challengeTTL: TimeInterval = 120,
        now: @escaping @Sendable () -> Date = { Date() }
    ) throws -> RelayNetworkServerHostFixture {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let pairingContext = try RelayServerPairingContext(
            pairingCode: try RelayPairingCode("2345-6789-ABCD-EFGH"),
            now: now(),
            challengeTTL: challengeTTL
        )
        let host = try RelayHost(
            pairingContext: pairingContext,
            configuration: try RelayHostConfiguration(fixtureDirectory: directory),
            now: now
        )
        return RelayNetworkServerHostFixture(host: host, directory: directory)
    }
}

private struct RelayNetworkServerHostFixture {
    let host: RelayHost
    let directory: URL
}

private final class RelayNetworkServerCancellationRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var listenerCancellations = 0
    private var connectionCancellations = 0

    var listenerCancellationCount: Int {
        lock.withLock { listenerCancellations }
    }

    var connectionCancellationCount: Int {
        lock.withLock { connectionCancellations }
    }

    func recordListenerCancellation() {
        lock.withLock {
            listenerCancellations += 1
        }
    }

    func recordConnectionCancellation() {
        lock.withLock {
            connectionCancellations += 1
        }
    }
}

private final class RelayNetworkServerTestClock: @unchecked Sendable {
    private let lock = NSLock()
    private var now: Date

    init(_ now: Date) {
        self.now = now
    }

    func value() -> Date {
        lock.withLock { now }
    }

    func set(_ now: Date) {
        lock.withLock {
            self.now = now
        }
    }
}

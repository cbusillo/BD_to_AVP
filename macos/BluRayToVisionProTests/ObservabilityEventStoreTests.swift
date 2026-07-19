import Darwin
import Foundation
import XCTest
@testable import BluRayToVisionPro

final class ObservabilityEventStoreTests: XCTestCase {
    func testAsyncFlushPersistsPendingEvent() async throws {
        let createdURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: createdURL, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: createdURL) }
        let rootURL = canonicalTemporaryURL(createdURL)
        let directoryURL = rootURL.appendingPathComponent("Observability", isDirectory: true)
        let store = makeStore(directoryURL: directoryURL)

        store.append(try makeEvent(sequence: 1))
        await store.flush()

        XCTAssertEqual(store.snapshot().writtenEvents, 1)
        XCTAssertTrue(
            FileManager.default.fileExists(
                atPath: directoryURL.appendingPathComponent("events.jsonl").path
            )
        )
    }

    func testCanonicalEventPersistsAsOwnerOnlyJSONL() throws {
        try withTemporaryDirectory { rootURL in
            let directoryURL = rootURL.appendingPathComponent("Observability", isDirectory: true)
            let store = makeStore(directoryURL: directoryURL)
            let event = try makeEvent(sequence: 1)

            store.append(event)
            store.flushForTesting()

            let eventURL = directoryURL.appendingPathComponent("events.jsonl")
            let line = try Data(contentsOf: eventURL).dropLast()
            XCTAssertEqual(try JSONDecoder().decode(ObservabilityEvent.self, from: line), event)
            XCTAssertEqual(try permissions(at: directoryURL), 0o700)
            XCTAssertEqual(try permissions(at: eventURL), 0o600)
            XCTAssertEqual(
                try permissions(at: directoryURL.appendingPathComponent(".events.jsonl.lock")),
                0o600
            )
            let snapshot = store.snapshot()
            XCTAssertEqual(snapshot.writtenEvents, 1)
            XCTAssertEqual(snapshot.droppedEvents, 0)
            XCTAssertEqual(snapshot.failureCount, 0)
        }
    }

    func testRotationRespectsPerFileAndTotalLimitsAndRetainsNewestEvent() throws {
        try withTemporaryDirectory { rootURL in
            let directoryURL = rootURL.appendingPathComponent("Observability", isDirectory: true)
            let maximumFileBytes = 4_096
            let maximumTotalBytes = 8_192
            let store = makeStore(
                directoryURL: directoryURL,
                maximumFileBytes: maximumFileBytes,
                maximumTotalBytes: maximumTotalBytes,
                maximumPendingBytes: 32_768
            )

            for sequence in 0 ..< 12 {
                store.append(try makeEvent(sequence: Int64(sequence)))
            }
            store.flushForTesting()

            let segmentURLs = try FileManager.default.contentsOfDirectory(
                at: directoryURL,
                includingPropertiesForKeys: nil
            )
            .filter { $0.lastPathComponent.hasPrefix("events") && $0.pathExtension == "jsonl" }
            let sizes = try segmentURLs.map {
                try XCTUnwrap(
                    FileManager.default.attributesOfItem(atPath: $0.path)[.size] as? NSNumber
                ).intValue
            }
            XCTAssertFalse(sizes.isEmpty)
            XCTAssertTrue(sizes.allSatisfy { $0 <= maximumFileBytes })
            XCTAssertLessThanOrEqual(sizes.reduce(0, +), maximumTotalBytes)

            let currentData = try Data(
                contentsOf: directoryURL.appendingPathComponent("events.jsonl")
            )
            let currentEvents = try decodeLines(currentData)
            XCTAssertTrue(currentEvents.contains { $0.sequence == 11 })
        }
    }

    func testSymlinkedEventFileIsRejectedWithoutTouchingTarget() throws {
        try withTemporaryDirectory { rootURL in
            let directoryURL = rootURL.appendingPathComponent("Observability", isDirectory: true)
            try FileManager.default.createDirectory(
                at: directoryURL,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let targetURL = rootURL.appendingPathComponent("target.txt")
            try Data("unchanged".utf8).write(to: targetURL)
            try FileManager.default.createSymbolicLink(
                at: directoryURL.appendingPathComponent("events.jsonl"),
                withDestinationURL: targetURL
            )
            let store = makeStore(directoryURL: directoryURL)

            store.append(try makeEvent(sequence: 1))
            store.flushForTesting()

            XCTAssertEqual(try String(contentsOf: targetURL, encoding: .utf8), "unchanged")
            XCTAssertEqual(store.snapshot().writtenEvents, 0)
            XCTAssertEqual(store.snapshot().droppedEvents, 1)
            XCTAssertEqual(store.snapshot().failureCount, 1)
        }
    }

    func testHardLinkedEventFileIsRejectedWithoutTouchingTarget() throws {
        try withTemporaryDirectory { rootURL in
            let directoryURL = rootURL.appendingPathComponent("Observability", isDirectory: true)
            try FileManager.default.createDirectory(
                at: directoryURL,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let targetURL = rootURL.appendingPathComponent("target.txt")
            try Data("unchanged".utf8).write(to: targetURL)
            let eventURL = directoryURL.appendingPathComponent("events.jsonl")
            XCTAssertEqual(link(targetURL.path, eventURL.path), 0)
            let store = makeStore(directoryURL: directoryURL)

            store.append(try makeEvent(sequence: 1))
            store.flushForTesting()

            XCTAssertEqual(try String(contentsOf: targetURL, encoding: .utf8), "unchanged")
            XCTAssertEqual(store.snapshot().writtenEvents, 0)
            XCTAssertEqual(store.snapshot().failureCount, 1)
        }
    }

    func testFIFOEventEntryIsRejectedWithoutBlockingDrain() throws {
        try withTemporaryDirectory { rootURL in
            let directoryURL = rootURL.appendingPathComponent("Observability", isDirectory: true)
            try FileManager.default.createDirectory(
                at: directoryURL,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let eventURL = directoryURL.appendingPathComponent("events.jsonl")
            XCTAssertEqual(mkfifo(eventURL.path, mode_t(0o600)), 0)
            let store = makeStore(directoryURL: directoryURL)

            store.append(try makeEvent(sequence: 1))
            store.flushForTesting()

            XCTAssertEqual(store.snapshot().writtenEvents, 0)
            XCTAssertEqual(store.snapshot().failureCount, 1)
        }
    }

    func testPendingQueueRemainsBoundedWhileWriterIsBlocked() throws {
        let writer = BlockingObservabilityWriter()
        let event = try makeEvent(sequence: 1)
        let encodedBytes = try JSONEncoder().encode(event).count + 1
        let maximumPendingBytes = encodedBytes * 2
        let store = ObservabilityEventStore(
            configuration: .init(
                directoryURL: URL(fileURLWithPath: "/unused"),
                maximumFileBytes: encodedBytes * 2,
                maximumTotalBytes: encodedBytes * 4,
                maximumPendingBytes: maximumPendingBytes
            ),
            writerFactory: { writer }
        )

        store.append(event)
        XCTAssertEqual(writer.started.wait(timeout: .now() + 2), .success)
        for sequence in 2 ... 20 {
            store.append(try makeEvent(sequence: Int64(sequence)))
        }

        let blockedSnapshot = store.snapshot()
        XCTAssertLessThanOrEqual(blockedSnapshot.pendingBytes, maximumPendingBytes)
        XCTAssertGreaterThan(blockedSnapshot.droppedEvents, 0)

        writer.release.signal()
        store.flushForTesting()
        XCTAssertEqual(store.snapshot().failureCount, 0)
    }

    func testExistingPartialTailIsTruncatedBeforeAppending() throws {
        try withTemporaryDirectory { rootURL in
            let directoryURL = rootURL.appendingPathComponent("Observability", isDirectory: true)
            try FileManager.default.createDirectory(
                at: directoryURL,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let firstEvent = try makeEvent(sequence: 1)
            let secondEvent = try makeEvent(sequence: 2)
            var existing = try JSONEncoder().encode(firstEvent)
            existing.append(0x0A)
            existing.append(Data(#"{"partial":"tail""#.utf8))
            try existing.write(to: directoryURL.appendingPathComponent("events.jsonl"))
            let store = makeStore(directoryURL: directoryURL)

            store.append(secondEvent)
            store.flushForTesting()

            XCTAssertEqual(
                store.snapshot().failureCount,
                0,
                store.failureDescriptionForTesting() ?? "unknown store failure"
            )
            let eventData = try Data(
                contentsOf: directoryURL.appendingPathComponent("events.jsonl")
            )
            XCTAssertFalse(String(decoding: eventData, as: UTF8.self).contains("partial"))
            let events = try decodeLines(eventData)
            XCTAssertEqual(events.map(\.sequence), [1, 2])
        }
    }

    func testExistingExtendedACLsAreRemoved() throws {
        try withTemporaryDirectory { rootURL in
            let directoryURL = rootURL.appendingPathComponent("Observability", isDirectory: true)
            try FileManager.default.createDirectory(
                at: directoryURL,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let eventURL = directoryURL.appendingPathComponent("events.jsonl")
            try Data().write(to: eventURL)
            try addEveryoneReadACL(to: directoryURL)
            try addEveryoneReadACL(to: eventURL)
            XCTAssertGreaterThan(try extendedACLCount(at: directoryURL), 0)
            XCTAssertGreaterThan(try extendedACLCount(at: eventURL), 0)
            let store = makeStore(directoryURL: directoryURL)

            store.append(try makeEvent(sequence: 1))
            store.flushForTesting()

            XCTAssertEqual(try extendedACLCount(at: directoryURL), 0)
            XCTAssertEqual(try extendedACLCount(at: eventURL), 0)
        }
    }

    func testLowerRetentionLimitPrunesOutOfRangeSegments() throws {
        try withTemporaryDirectory { rootURL in
            let directoryURL = rootURL.appendingPathComponent("Observability", isDirectory: true)
            let initialStore = makeStore(
                directoryURL: directoryURL,
                maximumFileBytes: 4_096,
                maximumTotalBytes: 12_288,
                maximumPendingBytes: 32_768
            )
            for sequence in 0 ..< 12 {
                initialStore.append(try makeEvent(sequence: Int64(sequence)))
            }
            initialStore.flushForTesting()
            XCTAssertTrue(
                FileManager.default.fileExists(
                    atPath: directoryURL.appendingPathComponent("events.1.jsonl").path
                )
            )

            let reducedStore = makeStore(
                directoryURL: directoryURL,
                maximumFileBytes: 4_096,
                maximumTotalBytes: 4_096,
                maximumPendingBytes: 8_192
            )
            reducedStore.append(try makeEvent(sequence: 100))
            reducedStore.flushForTesting()
            XCTAssertEqual(
                reducedStore.snapshot().failureCount,
                0,
                reducedStore.failureDescriptionForTesting() ?? "unknown store failure"
            )

            let eventFiles = try FileManager.default.contentsOfDirectory(
                at: directoryURL,
                includingPropertiesForKeys: nil
            )
            .map(\.lastPathComponent)
            .filter { $0.hasPrefix("events") && $0.hasSuffix(".jsonl") }
            XCTAssertEqual(eventFiles, ["events.jsonl"])
        }
    }

    func testRotationPreservesOlderSegmentAcrossExistingGap() throws {
        try withTemporaryDirectory { rootURL in
            let directoryURL = rootURL.appendingPathComponent("Observability", isDirectory: true)
            try FileManager.default.createDirectory(
                at: directoryURL,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let currentEvent = try makeEvent(sequence: 10)
            let olderEvent = try makeEvent(sequence: 2)
            let oldestEvent = try makeEvent(sequence: 1)
            var currentLine = try JSONEncoder().encode(currentEvent)
            currentLine.append(0x0A)
            var olderLine = try JSONEncoder().encode(olderEvent)
            olderLine.append(0x0A)
            var oldestLine = try JSONEncoder().encode(oldestEvent)
            oldestLine.append(0x0A)
            try currentLine.write(to: directoryURL.appendingPathComponent("events.jsonl"))
            try olderLine.write(to: directoryURL.appendingPathComponent("events.2.jsonl"))
            try oldestLine.write(to: directoryURL.appendingPathComponent("events.3.jsonl"))
            let maximumFileBytes = currentLine.count + 16
            let store = makeStore(
                directoryURL: directoryURL,
                maximumFileBytes: maximumFileBytes,
                maximumTotalBytes: maximumFileBytes * 4,
                maximumPendingBytes: maximumFileBytes * 2
            )

            store.append(try makeEvent(sequence: 11))
            store.flushForTesting()

            let olderEvents = try decodeLines(
                Data(contentsOf: directoryURL.appendingPathComponent("events.2.jsonl"))
            )
            let oldestEvents = try decodeLines(
                Data(contentsOf: directoryURL.appendingPathComponent("events.3.jsonl"))
            )
            XCTAssertEqual(olderEvents.map(\.sequence), [2])
            XCTAssertEqual(oldestEvents.map(\.sequence), [1])
            XCTAssertEqual(store.snapshot().failureCount, 0)
        }
    }

    func testRotationRemovesHardLinkedSegmentWithoutTouchingTarget() throws {
        try withTemporaryDirectory { rootURL in
            let directoryURL = rootURL.appendingPathComponent("Observability", isDirectory: true)
            try FileManager.default.createDirectory(
                at: directoryURL,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let currentEvent = try makeEvent(sequence: 10)
            var currentLine = try JSONEncoder().encode(currentEvent)
            currentLine.append(0x0A)
            try currentLine.write(to: directoryURL.appendingPathComponent("events.jsonl"))
            let targetURL = rootURL.appendingPathComponent("target.txt")
            try Data("unchanged".utf8).write(to: targetURL)
            XCTAssertEqual(
                link(
                    targetURL.path,
                    directoryURL.appendingPathComponent("events.1.jsonl").path
                ),
                0
            )
            let maximumFileBytes = currentLine.count + 16
            let store = makeStore(
                directoryURL: directoryURL,
                maximumFileBytes: maximumFileBytes,
                maximumTotalBytes: maximumFileBytes * 3,
                maximumPendingBytes: maximumFileBytes * 2
            )

            store.append(try makeEvent(sequence: 11))
            store.flushForTesting()

            XCTAssertEqual(try String(contentsOf: targetURL, encoding: .utf8), "unchanged")
            let rotatedEvents = try decodeLines(
                Data(contentsOf: directoryURL.appendingPathComponent("events.1.jsonl"))
            )
            XCTAssertEqual(rotatedEvents.map(\.sequence), [10])
            XCTAssertEqual(store.snapshot().failureCount, 0)
        }
    }

    private func makeStore(
        directoryURL: URL,
        maximumFileBytes: Int = 256 * 1_024,
        maximumTotalBytes: Int = 512 * 1_024,
        maximumPendingBytes: Int = 256 * 1_024
    ) -> ObservabilityEventStore {
        ObservabilityEventStore(
            configuration: .init(
                directoryURL: directoryURL,
                maximumFileBytes: maximumFileBytes,
                maximumTotalBytes: maximumTotalBytes,
                maximumPendingBytes: maximumPendingBytes
            )
        )
    }

    private func makeEvent(sequence: Int64) throws -> ObservabilityEvent {
        var fixture = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: sharedFixtureData()) as? [String: Any]
        )
        fixture["sequence"] = sequence
        return try JSONDecoder().decode(
            ObservabilityEvent.self,
            from: JSONSerialization.data(withJSONObject: fixture, options: [.sortedKeys])
        )
    }

    private func decodeLines(_ data: Data) throws -> [ObservabilityEvent] {
        try data.split(separator: 0x0A).map {
            try JSONDecoder().decode(ObservabilityEvent.self, from: Data($0))
        }
    }

    private func sharedFixtureData() throws -> Data {
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/observability_event_v1.json")
        return try XCTUnwrap(FileManager.default.contents(atPath: fixtureURL.path))
    }

    private func permissions(at url: URL) throws -> Int {
        let value = try XCTUnwrap(
            FileManager.default.attributesOfItem(atPath: url.path)[.posixPermissions] as? NSNumber
        )
        return value.intValue & 0o777
    }

    private func addEveryoneReadACL(to url: URL) throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/chmod")
        process.arguments = [
            "+a",
            "everyone allow read,readattr,readextattr,readsecurity",
            url.path,
        ]
        try process.run()
        process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, 0)
    }

    private func extendedACLCount(at url: URL) throws -> Int {
        let descriptor = open(url.path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)
        guard descriptor >= 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        defer { close(descriptor) }
        guard let accessControlList = acl_get_fd_np(descriptor, ACL_TYPE_EXTENDED) else {
            if errno == ENOENT {
                return 0
            }
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        defer { acl_free(UnsafeMutableRawPointer(accessControlList)) }
        var count = 0
        var entry: acl_entry_t?
        var entryIdentifier = ACL_FIRST_ENTRY.rawValue
        while acl_get_entry(accessControlList, entryIdentifier, &entry) == 0 {
            count += 1
            entryIdentifier = ACL_NEXT_ENTRY.rawValue
        }
        return count
    }

    private func withTemporaryDirectory(_ body: (URL) throws -> Void) throws {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: url) }
        try body(canonicalTemporaryURL(url))
    }

    private func canonicalTemporaryURL(_ url: URL) -> URL {
        if url.path.hasPrefix("/private/var/") {
            return url
        }
        if url.path.hasPrefix("/var/") {
            return URL(fileURLWithPath: "/private\(url.path)", isDirectory: true)
        }
        return url.resolvingSymlinksInPath()
    }
}

private final class BlockingObservabilityWriter: ObservabilityEventWriting {
    let started = DispatchSemaphore(value: 0)
    let release = DispatchSemaphore(value: 0)

    private let lock = NSLock()
    private var hasBlocked = false

    func append(_: Data) throws {
        let shouldBlock = lock.withLock { () -> Bool in
            if hasBlocked {
                return false
            }
            hasBlocked = true
            return true
        }
        if shouldBlock {
            started.signal()
            release.wait()
        }
    }
}

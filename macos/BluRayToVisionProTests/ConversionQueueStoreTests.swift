import Foundation
import XCTest
@testable import BluRayToVisionPro

final class ConversionQueueStoreTests: XCTestCase {
    func testRawDocumentRoundTripPreservesEveryStatusAndOmitsDerivedAvailability() throws {
        let items = DurableQueueItemState.allCases.enumerated().map { offset, state in
            makeItem(
                ordinal: offset,
                sourcePath: "/tmp/raw-source-\(offset).mkv",
                state: state,
                decision: state == .attention
                    ? DurableQueueDecision(
                        identifier: "mkv_creation_decision_required",
                        prompt: "Choose how to continue.",
                        choices: ["retry_continue_on_error"]
                    )
                    : nil,
                result: state == .completed ? DurableQueueResult(outputPath: "/tmp/raw-output.mov") : nil
            )
        }
        let document = ConversionQueueDocument(items: items)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        let data = try encoder.encode(document)
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        decoder.userInfo[.requiresVideoQualityIntent] = true

        XCTAssertEqual(try decoder.decode(ConversionQueueDocument.self, from: data), document)
        let json = String(decoding: data, as: UTF8.self)
        XCTAssertFalse(json.contains("unavailable"))
        XCTAssertFalse(json.contains("videoRoute"))
    }

    @MainActor
    func testDocumentRoundTripPreservesCanonicalItemsAndFrozenSettings() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let groupID = UUID(uuidString: "A24749CB-D59A-4E55-BBF4-E6E0D92D92B2")!
        var options = ConversionOptions()
        options.encoding = EncodingOptions(
            videoQuality: .custom(
                mvHEVC: MVHEVCOptions(generatedMergeQuality: 83),
                av1CRF: 27,
                upscaleQuality: 83
            ),
            mvHEVC: MVHEVCOptions(generatedMergeQuality: 83),
            upscaleQuality: 83
        )
        options.job.removeOriginalAfterSuccess = false
        var firstItem = makeItem(
                id: UUID(uuidString: "551519B2-7C66-4E8F-AF04-519127511D78")!,
                ordinal: 0,
                groupID: groupID,
                origin: .sourceFolder,
                sourcePath: "/Volumes/Rips/first.iso",
                options: options,
                state: .waiting
            )
        firstItem.intent.sourceFolderDiscTitleSelection = .all3DVideos
        firstItem.intent.sourceFolderTitleIndex = 1
        let items = [
            firstItem,
            makeItem(
                id: UUID(uuidString: "47B17E9C-26D9-4554-8994-CB744D97B377")!,
                ordinal: 1,
                groupID: groupID,
                origin: .sourceFolder,
                sourcePath: "/Volumes/Rips/second.mkv",
                options: options,
                state: .failed,
                failure: DurableQueueFailure(
                    code: "worker_failed",
                    message: "Conversion failed.",
                    details: "Fixture failure",
                    retryable: true
                )
            ),
        ]
        let store = ConversionQueueStore(fileURL: fileURL)

        try await store.replaceItems(items)
        let restored = ConversionQueueStore(fileURL: fileURL)

        XCTAssertEqual(restored.items, items)
        XCTAssertEqual(restored.items.map(\.ordinal), [0, 1])
        XCTAssertEqual(restored.items.map(\.groupID), [groupID, groupID])
        XCTAssertEqual(restored.items.first?.intent.options, options)
        XCTAssertEqual(restored.items.first?.intent.sourceFolderDiscTitleSelection, .all3DVideos)
        XCTAssertEqual(restored.items.first?.intent.sourceFolderTitleIndex, 1)
        XCTAssertFalse(try XCTUnwrap(restored.items.first).intent.options.job.removeOriginalAfterSuccess)
    }

    @MainActor
    func testEphemeralStatesRestoreAsInterruptedAndPersistClosedAttempts() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let states = DurableQueueItemState.allCases
        var items = states.enumerated().map { offset, state in
            makeItem(
                ordinal: offset,
                sourcePath: "/tmp/source-\(offset).mkv",
                state: state,
                decision: state == .attention
                    ? DurableQueueDecision(
                        identifier: "subtitle_decision_required",
                        prompt: "Retry without subtitles?",
                        choices: ["retry_without_subtitles"]
                    )
                    : nil,
                failure: state == .failed
                    ? DurableQueueFailure(
                        code: "fixture_failure",
                        message: "Fixture failure",
                        details: nil,
                        retryable: true
                    )
                    : nil,
                result: state == .completed ? DurableQueueResult(outputPath: "/tmp/output.mov") : nil,
                routeQualityConflict: state == .needsChoice
                    ? DurableRouteQualityConflict(conflict: makeRouteQualityConflict())
                    : nil
            )
        }
        for index in items.indices where [.inspecting, .processing, .stopping].contains(items[index].state) {
            items[index].attempts = [DurableQueueAttempt(startedAt: Date(timeIntervalSince1970: 1))]
        }
        let store = ConversionQueueStore(fileURL: fileURL)
        try await store.replaceItems(items)

        let restored = ConversionQueueStore(fileURL: fileURL).items

        for (original, item) in zip(states, restored) {
            let expected: DurableQueueItemState = switch original {
            case .inspecting, .processing, .stopping:
                .interrupted
            default:
                original
            }
            XCTAssertEqual(item.state, expected)
        }
        let attention = try XCTUnwrap(restored.first(where: { $0.state == .attention }))
        XCTAssertTrue(try XCTUnwrap(attention.decision).staleAfterRestore)
        XCTAssertTrue(restored
            .filter { $0.state == .interrupted }
            .allSatisfy { $0.attempts.allSatisfy { $0.endedAt != nil } })

        let reopened = ConversionQueueStore(fileURL: fileURL).items
        XCTAssertEqual(reopened.map(\.state), restored.map(\.state))
        XCTAssertTrue(reopened
            .filter { $0.state == .interrupted }
            .allSatisfy { $0.attempts.allSatisfy { $0.endedAt != nil } })
    }

    @MainActor
    func testUnknownAttentionDecisionRestoresAsInterrupted() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let item = makeItem(
            ordinal: 0,
            state: .attention,
            decision: DurableQueueDecision(
                identifier: "future_decision",
                prompt: "Choose a future option.",
                choices: ["future_choice"]
            )
        )
        let store = ConversionQueueStore(fileURL: fileURL)
        try await store.replaceItems([item])

        let restored = try XCTUnwrap(ConversionQueueStore(fileURL: fileURL).items.first)

        XCTAssertEqual(restored.state, .interrupted)
        XCTAssertNil(restored.decision)
    }

    @MainActor
    func testMissingVideoQualityFailsClosedAndPreservesUnreadableDocument() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let sourceStore = ConversionQueueStore(fileURL: fileURL)
        try await sourceStore.replaceItems([makeItem(ordinal: 0)])
        var object = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: Data(contentsOf: fileURL)) as? [String: Any]
        )
        var items = try XCTUnwrap(object["items"] as? [[String: Any]])
        var intent = try XCTUnwrap(items[0]["intent"] as? [String: Any])
        var options = try XCTUnwrap(intent["options"] as? [String: Any])
        var encoding = try XCTUnwrap(options["encoding"] as? [String: Any])
        encoding.removeValue(forKey: "videoQuality")
        options["encoding"] = encoding
        intent["options"] = options
        items[0]["intent"] = intent
        object["items"] = items
        let invalidData = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        try invalidData.write(to: fileURL)

        let restored = ConversionQueueStore(fileURL: fileURL)

        XCTAssertTrue(restored.items.isEmpty)
        XCTAssertNotNil(restored.loadErrorMessage)
        XCTAssertFalse(restored.writesBlocked)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fileURL.path))
        XCTAssertEqual(try Data(contentsOf: fileURL.appendingPathExtension("corrupt")), invalidData)
    }

    @MainActor
    func testLoadingValidDocumentPerformsNoWrites() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let sourceStore = ConversionQueueStore(fileURL: fileURL)
        try await sourceStore.replaceItems([makeItem(ordinal: 0)])
        let counter = LockedCounter()

        let restored = ConversionQueueStore(
            fileURL: fileURL,
            dataWriter: { _, _ in counter.increment() }
        )

        XCTAssertEqual(restored.items.count, 1)
        XCTAssertEqual(counter.value, 0)
    }

    @MainActor
    func testUnsupportedNewerVersionBlocksWritesWithoutMovingOriginal() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let data = Data(#"{"items":[],"version":99}"#.utf8)
        try data.write(to: fileURL)

        let store = ConversionQueueStore(fileURL: fileURL)

        XCTAssertTrue(store.writesBlocked)
        XCTAssertEqual(try Data(contentsOf: fileURL), data)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fileURL.appendingPathExtension("corrupt").path))
        await XCTAssertThrowsErrorAsync(try await store.replaceItems([])) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .recoveryRequired)
        }
    }

    @MainActor
    func testOlderUnsupportedVersionBlocksWritesWithoutMovingOriginal() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let data = Data(#"{"items":[],"version":0}"#.utf8)
        try data.write(to: fileURL)

        let store = ConversionQueueStore(fileURL: fileURL)

        XCTAssertTrue(store.writesBlocked)
        XCTAssertEqual(try Data(contentsOf: fileURL), data)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fileURL.appendingPathExtension("corrupt").path))
        await XCTAssertThrowsErrorAsync(try await store.replaceItems([])) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .recoveryRequired)
        }
    }

    @MainActor
    func testTransientReadFailureBlocksWritesWithoutMovingOriginal() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let data = Data(#"{"items":[],"version":1}"#.utf8)
        try data.write(to: fileURL)

        let store = ConversionQueueStore(
            fileURL: fileURL,
            dataReader: { _ in throw QueueStoreTestError.readFailed }
        )

        XCTAssertTrue(store.writesBlocked)
        XCTAssertEqual(try Data(contentsOf: fileURL), data)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fileURL.appendingPathExtension("corrupt").path))
    }

    @MainActor
    func testInvalidOrdinalAndRouteQualityStateCombinationsAreRejected() async throws {
        let store = ConversionQueueStore.inMemory()
        let misordered = makeItem(ordinal: 1)
        let failedWithoutEvidence = makeItem(ordinal: 0, state: .failed)
        let waitingWithConflict = makeItem(
            ordinal: 0,
            routeQualityConflict: DurableRouteQualityConflict(conflict: makeRouteQualityConflict())
        )
        let needsChoiceWithoutConflict = makeItem(ordinal: 0, state: .needsChoice)

        await XCTAssertThrowsErrorAsync(try await store.replaceItems([misordered])) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .invalidDocument)
        }
        await XCTAssertThrowsErrorAsync(try await store.replaceItems([failedWithoutEvidence])) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .invalidDocument)
        }
        await XCTAssertThrowsErrorAsync(try await store.replaceItems([waitingWithConflict])) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .invalidDocument)
        }
        await XCTAssertThrowsErrorAsync(try await store.replaceItems([needsChoiceWithoutConflict])) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .invalidDocument)
        }
    }

    @MainActor
    func testFailedCorruptFilePreservationBlocksWritesAndKeepsOriginal() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let invalidData = Data("not-json".utf8)
        try invalidData.write(to: fileURL)

        let store = ConversionQueueStore(
            fileURL: fileURL,
            dataMover: { _, _ in throw QueueStoreTestError.moveFailed }
        )

        XCTAssertTrue(store.writesBlocked)
        XCTAssertEqual(try Data(contentsOf: fileURL), invalidData)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fileURL.appendingPathExtension("corrupt").path))
        await XCTAssertThrowsErrorAsync(try await store.replaceItems([])) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .recoveryRequired)
        }
    }

    @MainActor
    func testFailedWriteLeavesPreviousDocumentByteIdentical() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let sourceStore = ConversionQueueStore(fileURL: fileURL)
        try await sourceStore.replaceItems([makeItem(ordinal: 0)])
        let originalData = try Data(contentsOf: fileURL)
        let failingStore = ConversionQueueStore(
            fileURL: fileURL,
            dataWriter: { _, _ in throw QueueStoreTestError.writeFailed }
        )

        await XCTAssertThrowsErrorAsync(
            try await failingStore.replaceItems([
                makeItem(ordinal: 0),
                makeItem(ordinal: 1, sourcePath: "/tmp/second.mkv"),
            ])
        ) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .writeFailed)
        }

        XCTAssertEqual(try Data(contentsOf: fileURL), originalData)
        XCTAssertEqual(failingStore.items.count, 1)
    }

    @MainActor
    func testOverlappingWritesConvergeToNewestGeneration() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let firstWriteStarted = expectation(description: "first write started")
        let releaseFirstWrite = DispatchSemaphore(value: 0)
        let writeCounter = LockedCounter()
        let store = ConversionQueueStore(
            fileURL: fileURL,
            dataWriter: { data, url in
                if writeCounter.incrementAndReturn() == 1 {
                    firstWriteStarted.fulfill()
                    releaseFirstWrite.wait()
                }
                try data.write(to: url, options: .atomic)
            }
        )
        let firstItems = [makeItem(ordinal: 0)]
        let secondItems = [
            makeItem(ordinal: 0),
            makeItem(ordinal: 1, sourcePath: "/tmp/newest.mkv"),
        ]

        let first = Task { @MainActor in try await store.replaceItems(firstItems) }
        await fulfillment(of: [firstWriteStarted], timeout: 2)
        let second = Task { @MainActor in try await store.replaceItems(secondItems) }
        releaseFirstWrite.signal()
        try await first.value
        try await second.value

        XCTAssertEqual(store.items, secondItems)
        XCTAssertEqual(ConversionQueueStore(fileURL: fileURL).items, secondItems)
    }

    @MainActor
    func testOverlappingMutationsApplyInOrderWithoutLosingEitherChange() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let firstWriteStarted = expectation(description: "first mutation write started")
        let releaseFirstWrite = DispatchSemaphore(value: 0)
        let writeCounter = LockedCounter()
        let store = ConversionQueueStore(
            fileURL: fileURL,
            dataWriter: { data, url in
                if writeCounter.incrementAndReturn() == 1 {
                    firstWriteStarted.fulfill()
                    releaseFirstWrite.wait()
                }
                try data.write(to: url, options: .atomic)
            }
        )

        let first = Task { @MainActor in
            try await store.mutateItems { items in
                items.append(self.makeItem(ordinal: 0, sourcePath: "/tmp/first.mkv"))
            }
        }
        await fulfillment(of: [firstWriteStarted], timeout: 2)
        let second = Task { @MainActor in
            try await store.mutateItems { items in
                items.append(self.makeItem(ordinal: 1, sourcePath: "/tmp/second.mkv"))
            }
        }
        releaseFirstWrite.signal()
        try await first.value
        try await second.value

        XCTAssertEqual(store.items.map(\.intent.source.path), ["/tmp/first.mkv", "/tmp/second.mkv"])
        XCTAssertEqual(ConversionQueueStore(fileURL: fileURL).items, store.items)
    }

    @MainActor
    func testPersistentQueueMoveBeforeAndMoveNextPreserveDenseOrdinals() async throws {
        let store = ConversionQueueStore.inMemory()
        let completed = makeItem(
            ordinal: 0,
            sourcePath: "/tmp/completed.mkv",
            state: .completed,
            result: DurableQueueResult(outputPath: "/tmp/completed.mov")
        )
        let first = makeItem(ordinal: 1, sourcePath: "/tmp/first.mkv")
        let second = makeItem(ordinal: 2, sourcePath: "/tmp/second.mkv")
        let third = makeItem(ordinal: 3, sourcePath: "/tmp/third.mkv")
        try await store.replaceItems([completed, first, second, third])

        try await store.moveWaitingItem(third.id, before: second.id)
        XCTAssertEqual(store.items.map(\.id), [completed.id, first.id, third.id, second.id])

        try await store.moveWaitingItemNext(second.id)
        XCTAssertEqual(store.items.map(\.id), [completed.id, second.id, first.id, third.id])
        XCTAssertEqual(store.items.map(\.ordinal), Array(store.items.indices))
    }

    @MainActor
    func testPersistentQueueAppendAndMoveAfterPreserveExistingRows() async throws {
        let store = ConversionQueueStore.inMemory()
        let completed = makeItem(
            ordinal: 0,
            sourcePath: "/tmp/completed.mkv",
            state: .completed,
            result: DurableQueueResult(outputPath: "/tmp/completed.mov")
        )
        let first = makeItem(ordinal: 0, sourcePath: "/tmp/first.mkv")
        let second = makeItem(ordinal: 1, sourcePath: "/tmp/second.mkv")
        try await store.replaceItems([completed])

        try await store.appendAdmittedItems([first, second])
        XCTAssertEqual(store.items.map(\.id), [completed.id, first.id, second.id])
        XCTAssertEqual(store.items.map(\.ordinal), [0, 1, 2])

        try await store.moveWaitingItem(first.id, after: second.id)
        XCTAssertEqual(store.items.map(\.id), [completed.id, second.id, first.id])
        XCTAssertEqual(store.items.map(\.ordinal), [0, 1, 2])
    }

    @MainActor
    func testPersistentQueueRemovalAllowsRecoverableTerminalRows() async throws {
        let store = ConversionQueueStore.inMemory()
        let failed = makeItem(
            ordinal: 0,
            sourcePath: "/tmp/failed.mkv",
            state: .failed,
            failure: DurableQueueFailure(code: "temporary", message: "Temporary", details: nil, retryable: true)
        )
        try await store.replaceItems([failed])

        let token = try await store.removeRemovableItems([failed.id])
        XCTAssertTrue(store.items.isEmpty)
        try await store.restoreRemovedItems(token)
        XCTAssertEqual(store.items, [failed])
    }

    @MainActor
    func testPersistentQueueRemovalTokenRestoresOriginalOrderAndIntents() async throws {
        let store = ConversionQueueStore.inMemory()
        let items = (0 ..< 4).map { offset in
            makeItem(ordinal: offset, sourcePath: "/tmp/source-\(offset).mkv")
        }
        try await store.replaceItems(items)

        let token = try await store.removeWaitingItems([items[1].id, items[3].id])
        XCTAssertEqual(store.items.map(\.id), [items[0].id, items[2].id])
        XCTAssertEqual(store.items.map(\.ordinal), [0, 1])

        try await store.restoreRemovedItems(token)
        XCTAssertEqual(store.items, items)
    }

    @MainActor
    func testPersistentQueueRemovalTokenRejectsRestoreAfterInterveningMutation() async throws {
        let store = ConversionQueueStore.inMemory()
        let items = (0 ..< 3).map { offset in
            makeItem(ordinal: offset, sourcePath: "/tmp/source-\(offset).mkv")
        }
        try await store.replaceItems(items)

        let token = try await store.removeWaitingItems([items[1].id])
        try await store.moveWaitingItemNext(items[2].id)

        await XCTAssertThrowsErrorAsync(try await store.restoreRemovedItems(token)) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .staleRemovalToken)
        }
        XCTAssertEqual(store.items.map(\.id), [items[2].id, items[0].id])
    }

    @MainActor
    func testPersistentQueueMutationsRejectNonWaitingItems() async throws {
        let store = ConversionQueueStore.inMemory()
        let waiting = makeItem(ordinal: 0, sourcePath: "/tmp/waiting.mkv")
        let completed = makeItem(
            ordinal: 1,
            sourcePath: "/tmp/completed.mkv",
            state: .completed,
            result: DurableQueueResult(outputPath: "/tmp/completed.mov")
        )
        try await store.replaceItems([waiting, completed])

        await XCTAssertThrowsErrorAsync(try await store.moveWaitingItemNext(completed.id)) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .invalidDocument)
        }
        await XCTAssertThrowsErrorAsync(try await store.removeWaitingItems([completed.id])) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .invalidDocument)
        }
        await XCTAssertThrowsErrorAsync(
            try await store.updateWaitingItemIntent(completed.id, intent: completed.intent)
        ) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .invalidDocument)
        }
        XCTAssertEqual(store.items, [waiting, completed])
    }

    @MainActor
    func testPersistentQueueIntentUpdateAndClearCompletedPersistCanonicalState() async throws {
        let store = ConversionQueueStore.inMemory()
        let waiting = makeItem(ordinal: 0, sourcePath: "/tmp/waiting.mkv")
        let completed = makeItem(
            ordinal: 1,
            sourcePath: "/tmp/completed.mkv",
            state: .completed,
            result: DurableQueueResult(outputPath: "/tmp/completed.mov")
        )
        try await store.replaceItems([waiting, completed])
        var updatedIntent = waiting.intent
        updatedIntent.destinationPath = "/tmp/New Output"

        try await store.updateWaitingItemIntent(waiting.id, intent: updatedIntent)
        let token = try await store.clearCompletedItems()

        XCTAssertEqual(store.items.count, 1)
        XCTAssertEqual(store.items[0].intent.destinationPath, "/tmp/New Output")
        XCTAssertEqual(store.items[0].ordinal, 0)
        XCTAssertEqual(token.items, [completed])
    }

    @MainActor
    func testClearCompletedPreservesCompletedSiblingsInActionableGroup() async throws {
        let store = ConversionQueueStore.inMemory()
        let groupID = UUID()
        let completed = makeItem(
            ordinal: 0,
            groupID: groupID,
            origin: .sourceFolder,
            sourcePath: "/tmp/completed.mkv",
            state: .completed,
            result: DurableQueueResult(outputPath: "/tmp/completed.mov")
        )
        let failed = makeItem(
            ordinal: 1,
            groupID: groupID,
            origin: .sourceFolder,
            sourcePath: "/tmp/failed.mkv",
            state: .failed,
            failure: DurableQueueFailure(
                code: "fixture_failure",
                message: "Fixture failure",
                details: nil,
                retryable: true
            )
        )
        let standaloneCompleted = makeItem(
            ordinal: 2,
            sourcePath: "/tmp/standalone.mkv",
            state: .completed,
            result: DurableQueueResult(outputPath: "/tmp/standalone.mov")
        )
        try await store.replaceItems([completed, failed, standaloneCompleted])

        let token = try await store.clearCompletedItems()

        XCTAssertEqual(store.items.map(\.id), [completed.id, failed.id])
        XCTAssertEqual(store.items.map(\.ordinal), [0, 1])
        XCTAssertEqual(token.items, [standaloneCompleted])
    }

    @MainActor
    func testClearCompletedRemovesCompletedSiblingFromFullyTerminalGroup() async throws {
        let store = ConversionQueueStore.inMemory()
        let groupID = UUID()
        let completed = makeItem(
            ordinal: 0,
            groupID: groupID,
            origin: .sourceFolder,
            sourcePath: "/tmp/completed.mkv",
            state: .completed,
            result: DurableQueueResult(outputPath: "/tmp/completed.mov")
        )
        let stopped = makeItem(
            ordinal: 1,
            groupID: groupID,
            origin: .sourceFolder,
            sourcePath: "/tmp/stopped.mkv",
            state: .stopped
        )
        try await store.replaceItems([completed, stopped])

        let token = try await store.clearCompletedItems()

        XCTAssertEqual(store.items.map(\.id), [stopped.id])
        XCTAssertEqual(store.items.map(\.ordinal), [0])
        XCTAssertEqual(token.items, [completed])
    }

    @MainActor
    func testClearCompletedRemovalTokenRestoresCompletedItems() async throws {
        let store = ConversionQueueStore.inMemory()
        let waiting = makeItem(ordinal: 0, sourcePath: "/tmp/waiting.mkv")
        let completed = makeItem(
            ordinal: 1,
            sourcePath: "/tmp/completed.mkv",
            state: .completed,
            result: DurableQueueResult(outputPath: "/tmp/completed.mov")
        )
        try await store.replaceItems([waiting, completed])

        let token = try await store.clearCompletedItems()
        try await store.restoreRemovedItems(token)

        XCTAssertEqual(store.items, [waiting, completed])
    }

    @MainActor
    func testPersistentQueueMutationsPreserveFinalMultiTitleSourceRemoval() async throws {
        let store = ConversionQueueStore.inMemory()
        let groupID = UUID()
        var options = ConversionOptions()
        options.job.removeOriginalAfterSuccess = true
        let first = makeItem(
            ordinal: 0,
            groupID: groupID,
            origin: .multiTitle,
            sourcePath: "/tmp/feature.iso",
            options: options
        )
        let second = makeItem(
            ordinal: 1,
            groupID: groupID,
            origin: .multiTitle,
            sourcePath: "/tmp/feature.iso",
            options: options
        )
        let third = makeItem(
            ordinal: 2,
            groupID: groupID,
            origin: .multiTitle,
            sourcePath: "/tmp/feature.iso",
            options: options
        )
        try await store.replaceItems([first, second, third])

        try await store.moveWaitingItem(third.id, before: second.id)
        XCTAssertEqual(
            store.items.map { $0.intent.options.job.removeOriginalAfterSuccess },
            [false, false, true]
        )

        let token = try await store.removeWaitingItems([second.id])
        XCTAssertEqual(
            store.items.map { $0.intent.options.job.removeOriginalAfterSuccess },
            [false, true]
        )

        try await store.restoreRemovedItems(token)
        XCTAssertEqual(
            store.items.map { $0.intent.options.job.removeOriginalAfterSuccess },
            [false, false, true]
        )
    }

    @MainActor
    func testPersistentQueueCrossGroupMovePreservesEachSourceRemovalOwner() async throws {
        let store = ConversionQueueStore.inMemory()
        let firstGroupID = UUID()
        let secondGroupID = UUID()
        var options = ConversionOptions()
        options.job.removeOriginalAfterSuccess = true
        let firstGroup = (0 ..< 2).map { offset in
            makeItem(
                ordinal: offset,
                groupID: firstGroupID,
                origin: .multiTitle,
                sourcePath: "/tmp/first.iso",
                options: options
            )
        }
        let secondGroup = (0 ..< 2).map { offset in
            makeItem(
                ordinal: 2 + offset,
                groupID: secondGroupID,
                origin: .multiTitle,
                sourcePath: "/tmp/second.iso",
                options: options
            )
        }
        try await store.replaceItems(firstGroup + secondGroup)

        try await store.moveWaitingItem(firstGroup[0].id, before: secondGroup[1].id)

        for groupID in [firstGroupID, secondGroupID] {
            let groupItems = store.items.filter { $0.groupID == groupID }
            XCTAssertEqual(
                groupItems.filter { $0.intent.options.job.removeOriginalAfterSuccess }.map(\.id),
                [try XCTUnwrap(groupItems.last?.id)]
            )
        }
        XCTAssertEqual(store.items.map(\.ordinal), Array(store.items.indices))
    }

    @MainActor
    func testPersistentQueueSameGroupPreservesSourceRemovalOwnerPerSource() async throws {
        let store = ConversionQueueStore.inMemory()
        let groupID = UUID()
        var options = ConversionOptions()
        options.job.removeOriginalAfterSuccess = true
        let firstSource = (0 ..< 2).map { offset in
            makeItem(
                ordinal: offset,
                groupID: groupID,
                origin: .multiTitle,
                sourcePath: "/tmp/first.iso",
                options: options
            )
        }
        let secondSource = (0 ..< 2).map { offset in
            makeItem(
                ordinal: 2 + offset,
                groupID: groupID,
                origin: .multiTitle,
                sourcePath: "/tmp/second.iso",
                options: options
            )
        }
        try await store.replaceItems(firstSource + secondSource)

        try await store.moveWaitingItem(firstSource[0].id, before: secondSource[1].id)

        for sourcePath in ["/tmp/first.iso", "/tmp/second.iso"] {
            let sourceItems = store.items.filter { $0.intent.source.path == sourcePath }
            XCTAssertEqual(
                sourceItems.filter { $0.intent.options.job.removeOriginalAfterSuccess }.map(\.id),
                [try XCTUnwrap(sourceItems.last?.id)]
            )
        }
    }

    @MainActor
    func testPersistentQueueRestoreRejectsTokenWhoseItemAlreadyExists() async throws {
        let store = ConversionQueueStore.inMemory()
        let item = makeItem(ordinal: 0)
        try await store.replaceItems([item])
        let token = try await store.removeWaitingItems([item.id])
        try await store.restoreRemovedItems(token)

        await XCTAssertThrowsErrorAsync(try await store.restoreRemovedItems(token)) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .staleRemovalToken)
        }
        XCTAssertEqual(store.items, [item])
    }

    @MainActor
    func testPersistentQueueMutationsFailClosedWhenWritesAreBlocked() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let originalData = Data("{\"items\":[],\"version\":\(ConversionQueueDocument.currentVersion + 1)}".utf8)
        try originalData.write(to: fileURL)
        let store = ConversionQueueStore(fileURL: fileURL)

        await XCTAssertThrowsErrorAsync(try await store.moveWaitingItemNext(UUID())) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .invalidDocument)
        }
        await XCTAssertThrowsErrorAsync(try await store.removeWaitingItems([UUID()])) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .invalidDocument)
        }
        await XCTAssertThrowsErrorAsync(try await store.clearCompletedItems()) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .recoveryRequired)
        }
        XCTAssertEqual(try Data(contentsOf: fileURL), originalData)
        XCTAssertTrue(store.items.isEmpty)
    }

    @MainActor
    func testResolutionTraceMigratesFromV1AndSurvivesRestoration() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let trace = DurableQueueResolutionTrace(
            conflictID: "generated_route_requirement|direct_mv_hevc|generated_mv_hevc|reusable_intermediates_requested|keep_requested_workflow:v1",
            resolutionID: "keep_requested_workflow:v1",
            qualityOutcome: "Balanced quality",
            fileOutcome: "Reusable files retained"
        )
        let item = makeItem(
            ordinal: 0,
            state: .needsChoice,
            routeQualityConflict: DurableRouteQualityConflict(conflict: makeRouteQualityConflict())
        )
        let v1Data = try JSONEncoder().encode(ConversionQueueDocument(version: 1, items: [item]))
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        try v1Data.write(to: fileURL)

        let store = ConversionQueueStore(fileURL: fileURL)
        try await store.resolveHeldItems(
            [item.id],
            intents: [item.id: item.intent],
            traces: [item.id: trace]
        )
        let restored = ConversionQueueStore(fileURL: fileURL)

        XCTAssertEqual(restored.document.version, ConversionQueueDocument.currentVersion)
        XCTAssertEqual(restored.items.first?.resolutionTrace, trace)
    }

    @MainActor
    func testResolvingHeldItemsPersistsEachItemTrace() async throws {
        let store = ConversionQueueStore.inMemory()
        let conflict = DurableRouteQualityConflict(conflict: makeRouteQualityConflict())
        let first = makeItem(ordinal: 0, state: .needsChoice, routeQualityConflict: conflict)
        let second = makeItem(ordinal: 1, state: .needsChoice, routeQualityConflict: conflict)
        let firstTrace = DurableQueueResolutionTrace(
            conflictID: "first",
            resolutionID: "first-resolution",
            qualityOutcome: "Balanced quality",
            fileOutcome: "Reusable files removed"
        )
        let secondTrace = DurableQueueResolutionTrace(
            conflictID: "second",
            resolutionID: "second-resolution",
            qualityOutcome: "Maximum quality",
            fileOutcome: "Reusable files kept"
        )
        try await store.replaceItems([first, second])

        try await store.resolveHeldItems(
            [first.id, second.id],
            intents: [first.id: first.intent, second.id: second.intent],
            traces: [first.id: firstTrace, second.id: secondTrace]
        )

        XCTAssertEqual(store.items.map(\.state), [.waiting, .waiting])
        XCTAssertEqual(store.items.map(\.resolutionTrace), [firstTrace, secondTrace])
    }

    @MainActor
    func testCompactionDropsOldestTerminalGroupsAndRenumbersOrdinals() async throws {
        let store = ConversionQueueStore.inMemory()
        let groupIDs = (0 ..< 7).map { _ in UUID() }
        let items = groupIDs.enumerated().flatMap { groupOffset, groupID in
            (0 ..< 2).map { itemOffset in
                makeItem(
                    ordinal: groupOffset * 2 + itemOffset,
                    groupID: groupID,
                    sourcePath: "/tmp/group-\(groupOffset)-item-\(itemOffset).mkv",
                    state: itemOffset == 0 ? .completed : .stopped,
                    result: itemOffset == 0
                        ? DurableQueueResult(outputPath: "/tmp/group-\(groupOffset).mov")
                        : nil
                )
            }
        }

        try await store.replaceItems(items)

        XCTAssertEqual(Set(store.items.compactMap(\.groupID)), Set(groupIDs.suffix(5)))
        XCTAssertEqual(store.items.map(\.ordinal), Array(store.items.indices))
        XCTAssertEqual(
            store.items.map(\.intent.source.path),
            items.filter { Set(groupIDs.suffix(5)).contains($0.groupID) }.map(\.intent.source.path)
        )
    }

    @MainActor
    func testCompactionPreservesWholeGroupsWithActionableMembers() async throws {
        let store = ConversionQueueStore.inMemory()
        let protectedStates: [(DurableQueueItemState, DurableQueueDecision?, DurableQueueFailure?)] = [
            (.waiting, nil, nil),
            (.inspecting, nil, nil),
            (.processing, nil, nil),
            (.stopping, nil, nil),
            (.interrupted, nil, nil),
            (
                .attention,
                DurableQueueDecision(
                    identifier: "subtitle_decision_required",
                    prompt: "Retry without subtitles?",
                    choices: ["retry_without_subtitles"]
                ),
                nil
            ),
            (
                .failed,
                nil,
                DurableQueueFailure(
                    code: "fixture_failure",
                    message: "Fixture failure",
                    details: nil,
                    retryable: true
                )
            ),
            (.notStarted, nil, nil),
        ]
        var items: [DurableConversionQueueItem] = []
        var protectedGroupIDs = Set<UUID>()
        for (groupOffset, stateFixture) in protectedStates.enumerated() {
            let groupID = UUID()
            protectedGroupIDs.insert(groupID)
            items.append(makeItem(
                ordinal: items.count,
                groupID: groupID,
                sourcePath: "/tmp/protected-\(groupOffset)-completed.mkv",
                state: .completed,
                result: DurableQueueResult(outputPath: "/tmp/protected-\(groupOffset).mov")
            ))
            items.append(makeItem(
                ordinal: items.count,
                groupID: groupID,
                sourcePath: "/tmp/protected-\(groupOffset)-actionable.mkv",
                state: stateFixture.0,
                decision: stateFixture.1,
                failure: stateFixture.2
            ))
        }
        for terminalGroupOffset in 0 ..< 7 {
            let groupID = UUID()
            items.append(makeItem(
                ordinal: items.count,
                groupID: groupID,
                sourcePath: "/tmp/terminal-\(terminalGroupOffset).mkv",
                state: .completed,
                result: DurableQueueResult(outputPath: "/tmp/terminal-\(terminalGroupOffset).mov")
            ))
        }

        try await store.replaceItems(items)

        for groupID in protectedGroupIDs {
            XCTAssertEqual(store.items.filter { $0.groupID == groupID }.count, 2)
        }
        XCTAssertEqual(store.items.count, protectedStates.count * 2 + ConversionQueueStore.maxRetainedTerminalUnits)
    }

    @MainActor
    func testCompactionTreatsUngroupedItemsAsIndependentRetentionUnits() async throws {
        let store = ConversionQueueStore.inMemory()
        let items = (0 ..< 8).map { offset in
            makeItem(
                ordinal: offset,
                sourcePath: "/tmp/ungrouped-\(offset).mkv",
                state: .completed,
                result: DurableQueueResult(outputPath: "/tmp/ungrouped-\(offset).mov")
            )
        }

        try await store.replaceItems(items)

        XCTAssertEqual(store.items.map(\.id), items.suffix(5).map(\.id))
        XCTAssertEqual(store.items.map(\.ordinal), Array(store.items.indices))
    }

    @MainActor
    func testCompactionEnforcesSoftItemCeilingByDroppingWholeTerminalGroups() async throws {
        let store = ConversionQueueStore.inMemory()
        let groupIDs = (0 ..< 5).map { _ in UUID() }
        let items = groupIDs.enumerated().flatMap { groupOffset, groupID in
            (0 ..< 80).map { itemOffset in
                makeItem(
                    ordinal: groupOffset * 80 + itemOffset,
                    groupID: groupID,
                    sourcePath: "/tmp/large-\(groupOffset)-\(itemOffset).mkv",
                    state: .completed,
                    result: DurableQueueResult(outputPath: "/tmp/large-\(groupOffset)-\(itemOffset).mov")
                )
            }
        }

        try await store.replaceItems(items)

        XCTAssertEqual(store.items.count, 160)
        XCTAssertEqual(Set(store.items.compactMap(\.groupID)), Set(groupIDs.suffix(2)))
        XCTAssertEqual(store.items.map(\.ordinal), Array(store.items.indices))
    }

    @MainActor
    func testCompactionKeepsNewestTerminalGroupWhenItExceedsSoftItemCeiling() async throws {
        let store = ConversionQueueStore.inMemory()
        let groupID = UUID()
        let items = (0 ..< 250).map { offset in
            makeItem(
                ordinal: offset,
                groupID: groupID,
                sourcePath: "/tmp/oversized-\(offset).mkv",
                state: .completed,
                result: DurableQueueResult(outputPath: "/tmp/oversized-\(offset).mov")
            )
        }

        try await store.replaceItems(items)

        XCTAssertEqual(store.items.count, 250)
        XCTAssertEqual(Set(store.items.compactMap(\.groupID)), [groupID])
    }

    @MainActor
    func testCompactionKeepsProtectedGroupWhenItExceedsSoftItemCeiling() async throws {
        let store = ConversionQueueStore.inMemory()
        let groupID = UUID()
        let items = (0 ..< 250).map { offset in
            makeItem(
                ordinal: offset,
                groupID: groupID,
                sourcePath: "/tmp/protected-oversized-\(offset).mkv",
                state: offset == 249 ? .interrupted : .completed,
                result: offset == 249
                    ? nil
                    : DurableQueueResult(outputPath: "/tmp/protected-oversized-\(offset).mov")
            )
        }

        try await store.replaceItems(items)

        XCTAssertEqual(store.items.count, 250)
        XCTAssertEqual(Set(store.items.compactMap(\.groupID)), [groupID])
        XCTAssertEqual(store.items.last?.state, .interrupted)
    }

    @MainActor
    func testOversizedDocumentLoadsWithoutWritingAndCompactsOnFirstMutation() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let items = (0 ..< 8).map { offset in
            makeItem(
                ordinal: offset,
                sourcePath: "/tmp/legacy-\(offset).mkv",
                state: .completed,
                result: DurableQueueResult(outputPath: "/tmp/legacy-\(offset).mov")
            )
        }
        try queueData(items: items).write(to: fileURL)
        let writeCounter = LockedCounter()
        let store = ConversionQueueStore(
            fileURL: fileURL,
            dataWriter: { data, url in
                writeCounter.increment()
                try data.write(to: url, options: .atomic)
            }
        )

        XCTAssertEqual(store.items.count, 8)
        XCTAssertEqual(writeCounter.value, 0)

        try await store.mutateItems { _ in }

        XCTAssertEqual(store.items.map(\.id), items.suffix(5).map(\.id))
        XCTAssertEqual(writeCounter.value, 1)
        XCTAssertEqual(ConversionQueueStore(fileURL: fileURL).items, store.items)
    }

    @MainActor
    func testNoOpMutationStillPersistsOneGenerationWhenCompactionDoesNothing() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let writeCounter = LockedCounter()
        let store = ConversionQueueStore(
            fileURL: fileURL,
            dataWriter: { data, url in
                writeCounter.increment()
                try data.write(to: url, options: .atomic)
            }
        )
        let item = makeItem(ordinal: 0)

        try await store.replaceItems([item])
        try await store.mutateItems { _ in }

        XCTAssertEqual(writeCounter.value, 2)
        XCTAssertEqual(store.items, [item])
    }

    @MainActor
    func testFailedWriteAfterCompactionLeavesPreviousDocumentUnchanged() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let items = (0 ..< 8).map { offset in
            makeItem(
                ordinal: offset,
                sourcePath: "/tmp/failing-\(offset).mkv",
                state: .completed,
                result: DurableQueueResult(outputPath: "/tmp/failing-\(offset).mov")
            )
        }
        let originalData = try queueData(items: items)
        try originalData.write(to: fileURL)
        let store = ConversionQueueStore(
            fileURL: fileURL,
            dataWriter: { _, _ in throw QueueStoreTestError.writeFailed }
        )

        await XCTAssertThrowsErrorAsync(try await store.mutateItems { _ in }) { error in
            XCTAssertEqual(error as? ConversionQueueStoreError, .writeFailed)
        }

        XCTAssertEqual(store.items, items)
        XCTAssertEqual(try Data(contentsOf: fileURL), originalData)
    }

    @MainActor
    func testSaveLoadSaveProducesStableBytes() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let items = [makeItem(ordinal: 0)]
        let store = ConversionQueueStore(fileURL: fileURL)
        try await store.replaceItems(items)
        let firstData = try Data(contentsOf: fileURL)
        let restored = ConversionQueueStore(fileURL: fileURL)

        try await restored.replaceItems(restored.items)

        XCTAssertEqual(try Data(contentsOf: fileURL), firstData)
    }

    @MainActor
    func testInjectingRestoredStoreDoesNotLaunchWorker() async throws {
        let directoryURL = temporaryDirectoryURL()
        defer { try? FileManager.default.removeItem(at: directoryURL) }
        let fileURL = directoryURL.appendingPathComponent("queue.json")
        let sourceStore = ConversionQueueStore(fileURL: fileURL)
        try await sourceStore.replaceItems([makeItem(ordinal: 0, state: .processing)])
        let restored = ConversionQueueStore(fileURL: fileURL)
        let counter = LockedCounter()

        let viewModel = ConversionViewModel(
            clientFactory: {
                counter.increment()
                throw QueueStoreTestError.workerLaunched
            },
            durableQueueStore: restored
        )

        XCTAssertEqual(counter.value, 0)
        XCTAssertEqual(viewModel.restoredDurableQueueItems.first?.state, .interrupted)
        XCTAssertNil(viewModel.durableQueueLoadErrorMessage)
        XCTAssertFalse(viewModel.durableQueueWritesBlocked)
    }

    func testStablePreviewIdentifierDoesNotDependOnArrayPosition() {
        let draft = makeDraft(sourcePath: "/tmp/movie.mkv", titleID: "42")
        let first = ConversionQueueItem.stablePreviewID(for: draft)
        let second = ConversionQueueItem.stablePreviewID(for: draft)
        let other = ConversionQueueItem.stablePreviewID(
            for: makeDraft(sourcePath: "/tmp/movie.mkv", titleID: "43")
        )

        XCTAssertEqual(first, second)
        XCTAssertNotEqual(first, other)
    }

    func testPersistentQueueProjectionProvidesSourceFirstIdentity() throws {
        let selectedTitle = SourceTitle(
            id: "title-1",
            name: "Main Movie",
            outputName: "Main Movie",
            durationSeconds: 7_200,
            resolution: "1920x1080",
            frameRate: "24000/1001",
            mainFeature: true
        )
        let item = DurableConversionQueueItem(
            ordinal: 0,
            origin: .multiTitle,
            intent: DurableQueueItemIntent(
                source: DurableQueueSource(
                    kind: ConversionSourceKind.physicalDisc.rawValue,
                    path: "/Volumes/Avatar 3D",
                    displayName: "Avatar 3D",
                    workerSourcePath: "/Volumes/Avatar 3D",
                    mediaIdentifier: "disk4s1"
                ),
                profile: DurableQueueProfile(
                    id: BuiltInProfile.balanced.id,
                    name: BuiltInProfile.balanced.name,
                    kind: .builtIn
                ),
                destinationPath: "/Movies",
                options: ConversionOptions(),
                selectedTitle: selectedTitle
            ),
            state: .waiting
        )

        let projection = try PersistentQueueItem(item: item)

        XCTAssertEqual(projection.sourceIdentity, "Avatar 3D")
        XCTAssertEqual(projection.selectedTitleIdentity, "Main Movie")
        XCTAssertEqual(projection.sourceKindName, "3D Blu-ray Disc")
        XCTAssertEqual(projection.sourceLocation, "/Volumes/Avatar 3D")
    }

    private func makeItem(
        id: UUID = UUID(),
        ordinal: Int,
        groupID: UUID? = nil,
        origin: DurableQueueItemOrigin = .singleSource,
        sourcePath: String = "/tmp/source.mkv",
        options: ConversionOptions = ConversionOptions(),
        state: DurableQueueItemState = .waiting,
        decision: DurableQueueDecision? = nil,
        failure: DurableQueueFailure? = nil,
        result: DurableQueueResult? = nil,
        routeQualityConflict: DurableRouteQualityConflict? = nil
    ) -> DurableConversionQueueItem {
        DurableConversionQueueItem(
            id: id,
            ordinal: ordinal,
            groupID: groupID,
            origin: origin,
            intent: DurableQueueItemIntent(
                source: DurableQueueSource(
                    kind: ConversionSourceKind.matroska.rawValue,
                    path: sourcePath,
                    displayName: URL(fileURLWithPath: sourcePath).lastPathComponent,
                    workerSourcePath: sourcePath
                ),
                profile: DurableQueueProfile(
                    id: BuiltInProfile.balanced.id,
                    name: BuiltInProfile.balanced.name,
                    kind: .builtIn
                ),
                destinationPath: "/tmp/Output",
                options: options
            ),
            state: state,
            decision: decision,
            failure: failure,
            result: result,
            routeQualityConflict: routeQualityConflict
        )
    }

    private func makeRouteQualityConflict() -> RouteQualityConflict {
        var options = ConversionOptions()
        try? options.encoding.selectQualityStep(.maximumDetail)
        guard case let .conflict(conflict) = RouteQualityEngine.propose(
            options: options,
            edit: .reusableIntermediates(true)
        ) else {
            fatalError("Expected reusable-file conflict")
        }
        return conflict
    }

    private func makeDraft(sourcePath: String, titleID: String) -> ConversionDraft {
        ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: sourcePath)),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/tmp/Output"),
            options: ConversionOptions(),
            selectedTitle: SourceTitle(
                id: titleID,
                name: "Title \(titleID)",
                outputName: "title-\(titleID)",
                durationSeconds: 100,
                resolution: "1920x1080",
                frameRate: "24000/1001",
                mainFeature: titleID == "42"
            )
        )
    }

    private func temporaryDirectoryURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("ConversionQueueStoreTests.\(UUID().uuidString)", isDirectory: true)
    }

    private func queueData(items: [DurableConversionQueueItem]) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        return try encoder.encode(ConversionQueueDocument(items: items))
    }
}

private final class LockedCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var storage = 0

    var value: Int {
        lock.withLock { storage }
    }

    func increment() {
        lock.withLock { storage += 1 }
    }

    func incrementAndReturn() -> Int {
        lock.withLock {
            storage += 1
            return storage
        }
    }
}

private enum QueueStoreTestError: Error {
    case readFailed
    case writeFailed
    case moveFailed
    case workerLaunched
}

private func XCTAssertThrowsErrorAsync<T>(
    _ expression: @autoclosure () async throws -> T,
    _ errorHandler: (Error) -> Void = { _ in }
) async {
    do {
        _ = try await expression()
        XCTFail("Expected expression to throw")
    } catch {
        errorHandler(error)
    }
}

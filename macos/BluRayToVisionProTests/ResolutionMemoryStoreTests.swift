import XCTest
@testable import BluRayToVisionPro

final class ResolutionMemoryStoreTests: XCTestCase {
    @MainActor
    func testScopePrecedenceAndMappingVersionStaleness() throws {
        let store = ResolutionMemoryStore.inMemory()
        try store.store(resolutionID: "global", for: "conflict", scope: .global, mappingVersion: 1)
        try store.store(resolutionID: "source", for: "conflict", scope: .sourceKind(.matroska), mappingVersion: 1)
        try store.store(resolutionID: "profile", for: "conflict", scope: .profile("custom.one"), mappingVersion: 1)

        let suggestion = try XCTUnwrap(store.suggestion(
            conflictID: "conflict",
            profileID: "custom.one",
            sourceKind: .matroska,
            mappingVersion: 2
        ))
        XCTAssertEqual(suggestion.entry.resolutionID, "profile")
        XCTAssertTrue(suggestion.isStale)
        XCTAssertEqual(
            suggestion.staleExplanation,
            "Quality options changed after an update. Choose a current option before applying this suggestion."
        )
    }

    @MainActor
    func testStableConflictIdentityKeepsAnOlderMappingVisibleAsStale() throws {
        var options = ConversionOptions()
        try options.encoding.selectQualityStep(.maximumDetail)
        guard case let .conflict(conflict) = RouteQualityEngine.propose(
            options: options,
            edit: .reusableIntermediates(true)
        ) else { return XCTFail("Expected conflict") }
        let store = ResolutionMemoryStore.inMemory()
        let resolutionID = try XCTUnwrap(conflict.resolutions.first(where: { $0.isAvailable })?.id)
        try store.store(
            resolutionID: resolutionID,
            for: conflict.stableID,
            scope: .profile(BuiltInProfile.balanced.id),
            mappingVersion: conflict.mappingVersion - 1
        )

        let suggestion = try XCTUnwrap(store.suggestion(
            conflictID: conflict.stableID,
            profileID: BuiltInProfile.balanced.id,
            sourceKind: .matroska,
            mappingVersion: conflict.mappingVersion
        ))
        XCTAssertTrue(suggestion.isStale)
    }

    @MainActor
    func testChangeForgetAndProfileDeletionCleanup() throws {
        let store = ResolutionMemoryStore.inMemory()
        try store.store(resolutionID: "first", for: "conflict", scope: .profile("custom.one"), mappingVersion: 1)
        try store.store(resolutionID: "changed", for: "conflict", scope: .profile("custom.one"), mappingVersion: 1)
        XCTAssertEqual(store.entries.map(\.resolutionID), ["changed"])

        try store.removeProfileMemories(profileID: "custom.one")
        XCTAssertTrue(store.entries.isEmpty)
        try store.store(resolutionID: "again", for: "conflict", scope: .global, mappingVersion: 1)
        try store.forget(conflictID: "conflict", scope: .global)
        XCTAssertTrue(store.entries.isEmpty)
    }

    @MainActor
    func testSuggestionsExpireAfterFourteenDays() throws {
        let storedAt = Date(timeIntervalSince1970: 1_700_000_000)
        var currentDate = storedAt
        let store = ResolutionMemoryStore.inMemory(now: { currentDate })
        try store.store(resolutionID: "choice", for: "conflict", scope: .global, mappingVersion: 1)

        currentDate = storedAt.addingTimeInterval(ResolutionMemoryStore.suggestionLifetime)
        XCTAssertNotNil(store.suggestion(
            conflictID: "conflict",
            profileID: "builtin.balanced",
            sourceKind: .matroska,
            mappingVersion: 1
        ))

        currentDate.addTimeInterval(1)
        XCTAssertNil(store.suggestion(
            conflictID: "conflict",
            profileID: "builtin.balanced",
            sourceKind: .matroska,
            mappingVersion: 1
        ))
        XCTAssertEqual(store.entries.count, 1)
    }

    @MainActor
    func testVersionOneDocumentMigratesWithFreshTimestamp() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let fileURL = directory.appendingPathComponent("resolution-memory.json")
        let data = Data("""
        {
          "version": 1,
          "entries": [
            {
              "conflictID": "conflict",
              "resolutionID": "choice",
              "scope": { "kind": "global" },
              "mappingVersion": 1
            }
          ]
        }
        """.utf8)
        try data.write(to: fileURL)
        let migrationDate = Date(timeIntervalSince1970: 1_700_000_000)

        let store = ResolutionMemoryStore(fileURL: fileURL, now: { migrationDate })

        XCTAssertEqual(store.entries.first?.storedAt, migrationDate)
        let migrated = try JSONDecoder().decode(
            ResolutionMemoryDocument.self,
            from: Data(contentsOf: fileURL)
        )
        XCTAssertEqual(migrated.version, ResolutionMemoryDocument.currentVersion)
        XCTAssertEqual(migrated.entries.first?.storedAt, migrationDate)
    }

    @MainActor
    func testCorruptDocumentIsPreserved() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let fileURL = directory.appendingPathComponent("resolution-memory.json")
        let data = Data("not json".utf8)
        try data.write(to: fileURL)

        let store = ResolutionMemoryStore(fileURL: fileURL)

        XCTAssertTrue(store.entries.isEmpty)
        XCTAssertNotNil(store.loadErrorMessage)
        XCTAssertEqual(try Data(contentsOf: fileURL.appendingPathExtension("corrupt")), data)
    }
}

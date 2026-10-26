import XCTest
@testable import BDToAVPPlayer

final class ResumeStoreTests: XCTestCase {
    func testResumeRoundTripPersistsThroughApplicationSupportStyleJSON() throws {
        let storageURL = temporaryURL()
        let store = ResumeStore(storageURL: storageURL)
        try store.setResumeTime(123.5, for: "movie-1")

        let reloaded = ResumeStore(storageURL: storageURL)

        XCTAssertEqual(reloaded.resumeTime(for: "movie-1"), 123.5)
        XCTAssertLessThanOrEqual(try Data(contentsOf: storageURL).count, 64 * 1024)
    }

    func testPositionSavedUnderAnOldKeyMovesToTheNewKeyAndPersists() throws {
        let storageURL = temporaryURL()
        let store = ResumeStore(storageURL: storageURL)
        try store.setResumeTime(42, for: "shared:old")

        try store.adoptPosition(from: "shared:old", to: "shared-file:new")

        let reloaded = ResumeStore(storageURL: storageURL)
        XCTAssertEqual(reloaded.resumeTime(for: "shared-file:new"), 42)
        XCTAssertNil(reloaded.resumeTime(for: "shared:old"))
    }

    func testAdoptingAnOldPositionNeverReplacesANewerOne() throws {
        let store = ResumeStore(storageURL: temporaryURL())
        try store.setResumeTime(42, for: "shared:old")
        try store.setResumeTime(90, for: "shared-file:new")

        try store.adoptPosition(from: "shared:old", to: "shared-file:new")

        XCTAssertEqual(store.resumeTime(for: "shared-file:new"), 90)
    }

    func testABlurayTitleInALocationResumesWhereItStoppedAfterARelaunch() throws {
        let storageURL = temporaryURL()
        let location = UUID()
        let itemID = LocationLibraryModel.itemID(locationID: location, relativePath: "Testers/Ocean Dreams 3D (2013).iso")
        try ResumeStore(storageURL: storageURL).setResumeTime(4_210.5, for: itemID)

        // The relaunched app finds the same item under the same key and starts the title there.
        let relaunchedID = LocationLibraryModel.itemID(locationID: location, relativePath: "Testers/Ocean Dreams 3D (2013).iso")
        let saved = ResumeStore(storageURL: storageURL).resumeTime(for: relaunchedID)

        XCTAssertEqual(ResumeWritePolicy.blurayStartSeconds(savedPosition: saved, duration: 5_642.8), 4_210.5)
        XCTAssertNil(ResumeStore(storageURL: storageURL).resumeTime(
            for: LocationLibraryModel.itemID(locationID: location, relativePath: "Testers/Fast X.iso")
        ))
        XCTAssertEqual(ResumeWritePolicy.blurayStartSeconds(savedPosition: nil, duration: 5_642.8), 0)
        XCTAssertEqual(ResumeWritePolicy.blurayStartSeconds(savedPosition: 9_000, duration: 5_642.8), 5_642.8)
    }

    func testMissingResumeReturnsNil() {
        let store = ResumeStore(storageURL: temporaryURL())

        XCTAssertNil(store.resumeTime(for: "missing"))
    }

    func testCorruptResumeDataIsIgnored() throws {
        let storageURL = temporaryURL()
        let directory = storageURL.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try Data("not json".utf8).write(to: storageURL)

        let store = ResumeStore(storageURL: storageURL)

        XCTAssertNil(store.resumeTime(for: "movie-1"))
    }

    func testTooManyEntriesAreBoundedToConfiguredCapacity() throws {
        let storageURL = temporaryURL()
        var timestamp = Date(timeIntervalSince1970: 0)
        let store = ResumeStore(storageURL: storageURL, maxEntries: 2) {
            defer { timestamp.addTimeInterval(1) }
            return timestamp
        }
        try store.setResumeTime(1, for: "first")
        try store.setResumeTime(2, for: "second")
        try store.setResumeTime(3, for: "third")

        let reloaded = ResumeStore(storageURL: storageURL, maxEntries: 2)

        XCTAssertNil(reloaded.resumeTime(for: "first"))
        XCTAssertEqual(reloaded.resumeTime(for: "second"), 2)
        XCTAssertEqual(reloaded.resumeTime(for: "third"), 3)
    }

    private func temporaryURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("BDToAVPPlayerTests")
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("json")
    }
}

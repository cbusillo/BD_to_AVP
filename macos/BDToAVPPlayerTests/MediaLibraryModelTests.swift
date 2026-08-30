import XCTest
@testable import BDToAVPPlayer

final class MediaLibraryModelTests: XCTestCase {
    func testDuplicateIDsProduceOneConsistentItemAcrossProjections() {
        let first = MediaItem(
            id: "movie-1",
            title: "First",
            fileName: "first.mov",
            sourceURL: URL(fileURLWithPath: "/tmp/first.mov"),
            format: .mvHEVC
        )
        let replacement = MediaItem(
            id: "movie-1",
            title: "Replacement",
            fileName: "replacement.mov",
            sourceURL: URL(fileURLWithPath: "/tmp/replacement.mov"),
            format: .sideBySide
        )
        let second = MediaItem(url: URL(fileURLWithPath: "/tmp/second.mov"), format: .overUnder)
        let model = MediaLibraryModel(items: [first, replacement, second])

        XCTAssertEqual(model.items.count, 2)
        XCTAssertEqual(model.posters, model.files)
        XCTAssertEqual(model.posters.map(\.id), ["movie-1", second.id])
        XCTAssertEqual(model.posters.first?.title, "First")

        model.upsert(replacement)

        XCTAssertEqual(model.items.count, 2)
        XCTAssertEqual(model.posters, model.files)
        XCTAssertEqual(model.files.first?.title, "Replacement")
    }
}

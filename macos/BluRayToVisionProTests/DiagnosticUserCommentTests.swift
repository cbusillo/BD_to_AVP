import Foundation
import XCTest
@testable import BluRayToVisionPro

final class DiagnosticUserCommentTests: XCTestCase {
    func testEmptyInputReturnsNil() {
        XCTAssertNil(DiagnosticUserComment.normalize(""))
    }

    func testWhitespaceOnlyInputReturnsNil() {
        XCTAssertNil(DiagnosticUserComment.normalize("   \n\t  \r\n "))
    }

    func testNormalizesLineEndingsToLineFeed() {
        let comment = DiagnosticUserComment.normalize("first\r\nsecond\rthird")
        XCTAssertEqual(comment?.text, "first\nsecond\nthird")
        XCTAssertEqual(comment?.truncated, false)
    }

    func testStripsControlAndFormatCharactersButKeepsTabsAndNewlines() {
        let raw = "line\u{0000}one\u{0007}\ttwo\u{007F}\u{0085}\u{200B}\u{202E}\nthree"
        let comment = DiagnosticUserComment.normalize(raw)
        XCTAssertEqual(comment?.text, "lineone\ttwo\nthree")
    }

    func testTrimsLeadingAndTrailingWhitespace() {
        let comment = DiagnosticUserComment.normalize("\n\n  hello world  \n")
        XCTAssertEqual(comment?.text, "hello world")
    }

    func testOversizedCharacterCountIsBoundedAndFlagged() {
        let raw = String(repeating: "a", count: DiagnosticUserComment.maximumCharacterCount + 500)
        let comment = DiagnosticUserComment.normalize(raw)
        XCTAssertEqual(comment?.text.count, DiagnosticUserComment.maximumCharacterCount)
        XCTAssertEqual(comment?.truncated, true)
    }

    func testOversizedByteCountIsBoundedWithoutSplittingScalars() throws {
        let multiScalarCharacter = "e\u{0301}\u{0302}\u{0303}\u{0304}"
        let raw = String(repeating: multiScalarCharacter, count: 1_200)
        XCTAssertLessThan(raw.count, DiagnosticUserComment.maximumCharacterCount)
        XCTAssertGreaterThan(raw.utf8.count, DiagnosticUserComment.maximumByteCount)
        let comment = try XCTUnwrap(DiagnosticUserComment.normalize(raw))
        XCTAssertLessThanOrEqual(comment.text.utf8.count, DiagnosticUserComment.maximumByteCount)
        XCTAssertTrue(comment.truncated)
        // Valid UTF-8 round-trip proves no scalar was split.
        XCTAssertEqual(String(decoding: Array(comment.text.utf8), as: UTF8.self), comment.text)
    }
}

import Foundation
import XCTest
@testable import BluRayToVisionPro

final class JSONLFramerTests: XCTestCase {
    func testFramesEventsAcrossArbitraryChunks() throws {
        var framer = JSONLFramer()

        XCTAssertEqual(try framer.append(Data("{\"one\":1".utf8)), [])
        let lines = try framer.append(Data("}\n{\"two\":2}\r\n".utf8))

        XCTAssertEqual(lines.map { String(decoding: $0, as: UTF8.self) }, ["{\"one\":1}", "{\"two\":2}"])
        XCTAssertNoThrow(try framer.finish())
    }

    func testRejectsIncompleteFinalEvent() throws {
        var framer = JSONLFramer()
        _ = try framer.append(Data("{\"partial\":".utf8))

        XCTAssertThrowsError(try framer.finish()) { error in
            XCTAssertEqual(error as? JSONLFramingError, .incompleteLine)
        }
    }

    func testRejectsOversizedEvent() {
        var framer = JSONLFramer()
        let oversized = Data(repeating: 0x61, count: JSONLFramer.maximumLineBytes + 1)

        XCTAssertThrowsError(try framer.append(oversized)) { error in
            XCTAssertEqual(error as? JSONLFramingError, .lineTooLarge)
        }
    }
}

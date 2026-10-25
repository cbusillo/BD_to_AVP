import XCTest
@testable import BDToAVPPlayer

final class Bluray3DSourceOpenerTests: XCTestCase {
    func testPickedURLsResolveToSourceKinds() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let disc = root.appendingPathComponent("Movie", isDirectory: true)
        try FileManager.default.createDirectory(at: disc.appendingPathComponent("BDMV"), withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: disc.appendingPathComponent("BDMV/index.bdmv").path, contents: Data())
        let iso = root.appendingPathComponent("Movie.ISO")
        let mkv = root.appendingPathComponent("Movie.mkv")
        let mov = root.appendingPathComponent("Movie.mov")
        for file in [iso, mkv, mov] { FileManager.default.createFile(atPath: file.path, contents: Data()) }

        XCTAssertEqual(Bluray3DSourceOpener.kind(of: disc), .discFolder(root: disc))
        XCTAssertEqual(Bluray3DSourceOpener.kind(of: disc.appendingPathComponent("BDMV")), .discFolder(root: disc))
        XCTAssertEqual(Bluray3DSourceOpener.kind(of: iso), .discImage)
        XCTAssertEqual(Bluray3DSourceOpener.kind(of: mkv), .matroska)
        XCTAssertNil(Bluray3DSourceOpener.kind(of: mov))
        XCTAssertNil(Bluray3DSourceOpener.kind(of: root))
    }

    func testRealMatroskaFileDeliversStereoFrames() throws {
        try RealBluraySource.read(environment: "BD_TO_AVP_MVC_MKV") { try Bluray3DSourceOpener.open($0) }
    }
}

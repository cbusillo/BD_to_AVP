import Foundation
import XCTest
@testable import BluRayToVisionPro

final class LanguageCatalogTests: XCTestCase {
    func testBundledCatalogContainsAllSelectableLanguages() {
        let catalog = LanguageCatalog.shared

        XCTAssertEqual(catalog.languages.count, 414)
        XCTAssertEqual(catalog.language(matching: "eng")?.name, "English")
        XCTAssertNil(catalog.language(matching: "und"))
        XCTAssertTrue(catalog.commonLanguages.contains { $0.code == "nld" })
    }

    func testCatalogNormalizesLegacyAndIETFAliases() {
        let catalog = LanguageCatalog.shared
        let expected = [
            "nl": "nld",
            "dut": "nld",
            "nld": "nld",
            "ger": "deu",
            "fre": "fra",
            "chi": "zho",
            "pt-BR": "por",
            "zh_Hant": "zho",
        ]

        for (supplied, canonical) in expected {
            XCTAssertEqual(catalog.language(matching: supplied)?.code, canonical, supplied)
        }
    }

    func testSearchFindsDutchByNameAndEveryCode() {
        let catalog = LanguageCatalog.shared

        for query in ["Dutch", "dutch", "nl", "nld", "dut"] {
            XCTAssertEqual(catalog.search(query).first?.code, "nld", query)
        }
        XCTAssertTrue(catalog.search("not-a-language").isEmpty)
    }

    func testMediaLanguageCodableCanonicalizesLegacyAliasesForAudioAndSubtitles() throws {
        let decoded = try JSONDecoder().decode(MediaLanguage.self, from: Data(#""dut""#.utf8))
        let encoded = try JSONEncoder().encode(decoded)

        XCTAssertEqual(decoded, .dutch)
        XCTAssertEqual(String(decoding: encoded, as: UTF8.self), #""nld""#)
    }

    func testCatalogRejectsUnknownAndMalformedSelectableCodes() {
        let catalog = LanguageCatalog.shared

        for code in ["und", "xyz", "en-", "en-not", "zh__Hant"] {
            XCTAssertNil(catalog.language(matching: code), code)
        }
    }

    func testCatalogRejectsDuplicateCodes() {
        let data = Data(
            #"{"schema_version":1,"languages":[{"code":"eng","name":"English","alpha2":"en","bibliographic":"eng"},{"code":"eng","name":"Duplicate","alpha2":null,"bibliographic":"eng"}]}"#.utf8
        )

        XCTAssertThrowsError(try LanguageCatalog(data: data))
    }
}

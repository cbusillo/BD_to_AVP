import Foundation
import XCTest
@testable import BluRayToVisionPro

final class GitHubIssueDraftTests: XCTestCase {
    private func sampleDraft(
        supportCode: String = "BDAVP-0123456789ABCDEF",
        description: String? = "playback failed after stage two"
    ) -> GitHubIssueDraft {
        GitHubIssueDraft(
            supportCode: supportCode,
            appVersion: "1.2.3",
            appBuild: "456",
            capturedStage: "converting",
            redactedDescription: description
        )
    }

    func testURLTargetsOnlyThePublicRepositoryNewIssueEndpoint() throws {
        let url = try XCTUnwrap(sampleDraft().url())
        let components = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false))
        XCTAssertEqual(components.scheme, "https")
        XCTAssertEqual(components.host, "github.com")
        XCTAssertEqual(components.path, "/cbusillo/BD_to_AVP/issues/new")
    }

    func testQueryContainsOnlyTitleAndBodyParameters() throws {
        let url = try XCTUnwrap(sampleDraft().url())
        let components = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false))
        let names = Set((components.percentEncodedQueryItems ?? []).map(\.name))
        XCTAssertEqual(names, ["title", "body"])
    }

    func testBodyContainsAllowlistedFieldsOnly() {
        let draft = sampleDraft()
        let body = draft.body
        XCTAssertTrue(body.contains(draft.supportCode))
        XCTAssertTrue(body.contains("1.2.3"))
        XCTAssertTrue(body.contains("456"))
        XCTAssertTrue(body.contains("converting"))
        XCTAssertTrue(body.contains("playback failed after stage two"))
        XCTAssertTrue(body.lowercased().contains("public"))
    }

    func testDraftNeverExposesSecretBearingFieldNames() throws {
        let draft = sampleDraft()
        let url = try XCTUnwrap(draft.url())
        let haystack = (url.absoluteString + draft.title + draft.body).lowercased()
        for forbidden in ["report_id", "report-id", "reportid", "bearer", "object_key", "objectkey", "token=", "authorization", "/var/folders", "/users/"] {
            XCTAssertFalse(haystack.contains(forbidden), "URL/body must not contain \(forbidden)")
        }
    }

    func testSpecialCharactersInDescriptionCannotInjectExtraParameters() throws {
        // A description crafted to look like extra query parameters must be encoded.
        let draft = sampleDraft(description: "a&title=evil&body=evil#frag ?x=y")
        let url = try XCTUnwrap(draft.url())
        let components = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false))
        XCTAssertEqual((components.percentEncodedQueryItems ?? []).count, 2)
        XCTAssertFalse(url.absoluteString.contains("title=evil"))
        XCTAssertFalse(url.absoluteString.contains("body=evil"))
        XCTAssertNil(url.fragment)
    }

    func testDecodedQueryPreservesExactlyOneSafeTitleAndBody() throws {
        let draft = sampleDraft(description: "a&title=evil\nsecond line")
        let url = try XCTUnwrap(draft.url())
        let components = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false))
        let items = try XCTUnwrap(components.queryItems)
        XCTAssertEqual(items.count, 2)
        XCTAssertEqual(items.first { $0.name == "title" }?.value, draft.title)
        XCTAssertEqual(items.first { $0.name == "body" }?.value, draft.body)
    }

    func testTitleAndBodyAreBounded() {
        let draft = sampleDraft(description: String(repeating: "z", count: 20_000))
        XCTAssertLessThanOrEqual(draft.title.count, GitHubIssueDraft.maximumTitleCharacterCount)
        XCTAssertLessThanOrEqual(draft.body.count, GitHubIssueDraft.maximumBodyCharacterCount)
    }

    func testMissingDescriptionStillProducesSafeURL() throws {
        let draft = sampleDraft(description: nil)
        let url = try XCTUnwrap(draft.url())
        XCTAssertEqual(url.host, "github.com")
        XCTAssertTrue(draft.body.contains(draft.supportCode))
    }
}

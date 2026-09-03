import XCTest
@testable import BDToAVPPlayer

@MainActor
final class RelayPairingEntryModelTests: XCTestCase {
    func testBeginningSubmissionClearsSensitiveInputBeforeNetworkAttempt() {
        let model = RelayPairingEntryModel()
        model.pairingCode = "2345-6789-ABCD-EFGH"

        XCTAssertEqual(model.beginSubmission(), "2345-6789-ABCD-EFGH")
        XCTAssertEqual(model.pairingCode, "")
        XCTAssertTrue(model.isSubmitting)

        model.finishSubmission()

        XCTAssertFalse(model.isSubmitting)
    }

    func testBlankSubmissionIsClearedWithoutStartingAttempt() {
        let model = RelayPairingEntryModel()
        model.pairingCode = "   "

        XCTAssertNil(model.beginSubmission())
        XCTAssertEqual(model.pairingCode, "")
        XCTAssertFalse(model.isSubmitting)
    }
}

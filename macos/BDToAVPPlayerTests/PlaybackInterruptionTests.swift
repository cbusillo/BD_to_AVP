import AVFoundation
import XCTest
@testable import BDToAVPPlayer

/// Closing and reopening the app interrupts playback with AVFoundation's -11847; the player
/// rebuilds instead of showing it (#1165).
final class PlaybackInterruptionTests: XCTestCase {
    /// The error the headset showed after the app was closed and reopened during playback.
    static let operationInterrupted = NSError(
        domain: AVFoundationErrorDomain,
        code: -11847,
        userInfo: [
            NSLocalizedDescriptionKey: "Operation Interrupted",
            NSLocalizedRecoverySuggestionErrorKey: "Stop other operations and try again.",
        ]
    )

    func testSystemInterruptionsAreRecoverable() {
        XCTAssertTrue(PlaybackInterruptionPolicy.isInterruption(Self.operationInterrupted))
        let mediaServicesReset = NSError(domain: AVFoundationErrorDomain, code: -11819)
        XCTAssertTrue(PlaybackInterruptionPolicy.isInterruption(mediaServicesReset))
        let wrapped = NSError(
            domain: AVFoundationErrorDomain,
            code: -11800,
            userInfo: [NSUnderlyingErrorKey: Self.operationInterrupted]
        )
        XCTAssertTrue(PlaybackInterruptionPolicy.isInterruption(wrapped))
    }

    func testRealFailuresAreNotInterruptions() {
        XCTAssertFalse(PlaybackInterruptionPolicy.isInterruption(nil))
        XCTAssertFalse(PlaybackInterruptionPolicy.isInterruption(NSError(domain: AVFoundationErrorDomain, code: -11828)))
        XCTAssertFalse(PlaybackInterruptionPolicy.isInterruption(POSIXError(.ENOTCONN)))
        XCTAssertFalse(PlaybackInterruptionPolicy.isInterruption(NSError(domain: NSOSStatusErrorDomain, code: -11847)))
    }

    func testRecoveryIsSpacedOutAndGivesUpAfterRepeatedInterruptions() {
        var budget = PlaybackRecoveryBudget()
        XCTAssertEqual(budget.decision(now: 100), .recover)
        XCTAssertEqual(budget.decision(now: 100.25), .wait(PlaybackRecoveryBudget.spacing - 0.25))
        for attempt in 1 ..< PlaybackRecoveryBudget.attemptLimit {
            XCTAssertEqual(budget.decision(now: 100 + Double(attempt) * PlaybackRecoveryBudget.spacing), .recover)
        }
        XCTAssertEqual(budget.decision(now: 110), .giveUp)
        // Interruptions far apart, such as on each return to the app, always recover.
        XCTAssertEqual(budget.decision(now: 100 + PlaybackRecoveryBudget.window + 10), .recover)
        budget.reset()
        XCTAssertEqual(budget.attempts, [])
    }

    func testSystemErrorDetailsNeverReachTheWearer() {
        let message = PlaybackErrorText.message(for: Self.operationInterrupted)
        XCTAssertEqual(message, PlaybackErrorText.couldNotPlay)
        for raw in ["AVFoundationErrorDomain", "-11847", "Code", "UserInfo", "Stop other operations"] {
            XCTAssertFalse(message.contains(raw), raw)
        }
        let undecodable = NSError(
            domain: AVFoundationErrorDomain,
            code: -11829,
            userInfo: [NSUnderlyingErrorKey: NSError(domain: NSOSStatusErrorDomain, code: -12848)]
        )
        XCTAssertEqual(PlaybackErrorText.message(for: undecodable), PlaybackErrorText.unreadable)
        XCTAssertEqual(PlaybackErrorText.message(for: POSIXError(.ENOTCONN)), PlaybackErrorText.sourceStoppedResponding)
    }

    func testTheAppsOwnPlainErrorsAreShownAsWritten() {
        let error = ShareConnectionMonitor.ReadError.stoppedAnswering
        XCTAssertEqual(PlaybackErrorText.message(for: error), error.errorDescription)
        XCTAssertEqual(
            PlaybackErrorText.message(for: Bluray3DSourceError.readFailed),
            Bluray3DSourceError.readFailed.errorDescription
        )
    }
}

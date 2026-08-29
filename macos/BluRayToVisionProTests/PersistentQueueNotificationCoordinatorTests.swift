import Foundation
import XCTest
@testable import BluRayToVisionPro

@MainActor
final class PersistentQueueNotificationCoordinatorTests: XCTestCase {
    func testInitialRestoredTerminalStateIsBaselineOnly() async throws {
        let delivery = CapturingQueueNotificationDelivery()
        let settings = makeSettings(finish: true, attention: true)
        let failed = try makeItem(ordinal: 0, state: .failed, sourcePath: "/Private/Restored.mkv")
        let coordinator = PersistentQueueNotificationCoordinator(
            settings: settings,
            delivery: delivery,
            initialItems: [failed],
            initialRunState: .idle
        )

        coordinator.observe(items: [failed], runState: .idle)
        await Task.yield()

        XCTAssertEqual(delivery.deliveredRequests.count, 0)
    }

    func testSendsOneAttentionAlertPerRunAndIgnoresProjectionRefreshes() async throws {
        let delivery = CapturingQueueNotificationDelivery()
        let settings = makeSettings(finish: false, attention: true)
        let coordinator = PersistentQueueNotificationCoordinator(settings: settings, delivery: delivery)
        let waiting = try makeItem(ordinal: 0, state: .waiting)
        let failed = try waiting.replacingStatus(.failed)

        coordinator.observe(items: [waiting], runState: .running)
        coordinator.observe(items: [failed], runState: .running)
        coordinator.observe(items: [failed], runState: .running)
        await Task.yield()

        XCTAssertEqual(delivery.deliveredRequests.map(\.title), ["Queue Needs Attention"])
        XCTAssertEqual(delivery.deliveredRequests.first?.threadIdentifier, "queue.attention")
        XCTAssertEqual(delivery.deliveredRequests.first?.body, "1 queued item needs attention.")
    }

    func testCompletionUsesFinalDurableStatesForParticipatingItems() async throws {
        let delivery = CapturingQueueNotificationDelivery()
        let settings = makeSettings(finish: true, attention: false)
        let coordinator = PersistentQueueNotificationCoordinator(settings: settings, delivery: delivery)
        let first = try makeItem(ordinal: 0, state: .waiting)
        let second = try makeItem(ordinal: 1, state: .waiting)
        let stopped = try makeItem(ordinal: 2, state: .notStarted)
        let completed = try first.replacingStatus(.completed)
        let failed = try second.replacingStatus(.failed)

        coordinator.observe(items: [first, second, stopped], runState: .running)
        coordinator.observe(items: [completed, failed, stopped], runState: .idle)
        await Task.yield()

        XCTAssertEqual(delivery.deliveredRequests.map(\.title), ["Queue Finished"])
        XCTAssertEqual(delivery.deliveredRequests.first?.threadIdentifier, "queue.completion")
        XCTAssertEqual(delivery.deliveredRequests.first?.body, "Completed: 1. Failed: 1. Needs action: 0.")
    }

    func testPauseStatesPreserveSingleNotificationSession() async throws {
        let delivery = CapturingQueueNotificationDelivery()
        let settings = makeSettings(finish: true, attention: true)
        let coordinator = PersistentQueueNotificationCoordinator(settings: settings, delivery: delivery)
        let item = try makeItem(ordinal: 0, state: .waiting)
        let completed = try item.replacingStatus(.completed)

        coordinator.observe(items: [item], runState: .running)
        coordinator.observe(items: [item], runState: .pauseAfterCurrent)
        coordinator.observe(items: [item], runState: .paused)
        coordinator.observe(items: [item], runState: .running)
        coordinator.observe(items: [completed], runState: .idle)
        await Task.yield()

        XCTAssertEqual(delivery.deliveredRequests.map(\.title), ["Queue Finished"])
        XCTAssertEqual(delivery.deliveredRequests.first?.body, "Completed: 1. Failed: 0. Needs action: 0.")
    }

    func testDisabledPreferencesRequestAuthorizationOnlyWhenEnabled() async throws {
        let delivery = CapturingQueueNotificationDelivery()
        let settings = makeSettings(finish: false, attention: false)
        let coordinator = PersistentQueueNotificationCoordinator(settings: settings, delivery: delivery)
        await Task.yield()
        XCTAssertEqual(delivery.authorizationRequestCount, 0)

        let authorizationRequested = expectation(description: "authorization requested")
        delivery.authorizationRequested = authorizationRequested
        settings.notifyWhenQueueFinishes = true
        coordinator.refreshAuthorizationPreference()
        await fulfillment(of: [authorizationRequested], timeout: 1)
        XCTAssertEqual(delivery.authorizationRequestCount, 1)

        settings.notifyWhenQueueNeedsAttention = true
        coordinator.refreshAuthorizationPreference()
        await Task.yield()
        XCTAssertEqual(delivery.authorizationRequestCount, 1)
        _ = coordinator
    }

    func testNotificationCopyIsPrivacySafe() async throws {
        let delivery = CapturingQueueNotificationDelivery()
        let settings = makeSettings(finish: true, attention: true)
        let coordinator = PersistentQueueNotificationCoordinator(settings: settings, delivery: delivery)
        let waiting = try makeItem(ordinal: 0, state: .waiting, sourcePath: "/Volumes/Private Movie/Secret Feature.mkv")
        let failed = try waiting.replacingStatus(.failed, message: "ffmpeg failed at /tmp/private-output.mov")

        coordinator.observe(items: [waiting], runState: .running)
        coordinator.observe(items: [failed], runState: .idle)
        await Task.yield()

        let copy = delivery.deliveredRequests.flatMap { [$0.title, $0.body, $0.threadIdentifier, $0.identifier] }.joined(separator: " ")
        XCTAssertFalse(copy.contains("Secret Feature"))
        XCTAssertFalse(copy.contains("/Volumes"))
        XCTAssertFalse(copy.contains("/tmp"))
        XCTAssertFalse(copy.localizedCaseInsensitiveContains("ffmpeg"))
    }

    private func makeSettings(finish: Bool, attention: Bool) -> AppSettings {
        let suiteName = "PersistentQueueNotificationCoordinatorTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defaults.removePersistentDomain(forName: suiteName)
        defaults.set(finish, forKey: "native.notifyWhenQueueFinishes")
        defaults.set(attention, forKey: "native.notifyWhenQueueNeedsAttention")
        return AppSettings(defaults: defaults, homeDirectoryURL: URL(fileURLWithPath: "/Users/example"))
    }

    private func makeItem(
        ordinal: Int,
        state: DurableQueueItemState,
        sourcePath: String? = nil,
        message: String = "Temporary failure"
    ) throws -> PersistentQueueItem {
        let path = sourcePath ?? "/Sources/Feature-\(ordinal).mkv"
        let draft = ConversionDraft(
            source: ConversionSource(kind: .matroska, url: URL(fileURLWithPath: path)),
            sourceDetails: nil,
            profile: BuiltInProfile.balanced.profile,
            destinationURL: URL(fileURLWithPath: "/Movies"),
            options: ConversionOptions()
        )
        return try PersistentQueueItem(item: DurableConversionQueueItem(
            ordinal: ordinal,
            origin: .singleSource,
            intent: DurableQueueItemIntent(draft: draft),
            state: state,
            decision: state == .attention ? DurableQueueDecision(
                identifier: "source_missing",
                prompt: "Reconnect the source.",
                choices: [WorkerRecoveryChoice.retryContinueOnError.rawValue]
            ) : nil,
            failure: state == .failed ? DurableQueueFailure(
                code: "temporary",
                message: message,
                details: nil,
                retryable: true
            ) : nil,
            result: state == .completed ? DurableQueueResult(outputPath: "/Movies/Feature-\(ordinal).mov") : nil
        ))
    }
}

@MainActor
private final class CapturingQueueNotificationDelivery: QueueNotificationDelivering {
    private(set) var authorizationRequestCount = 0
    private(set) var deliveredRequests: [QueueNotificationRequest] = []
    var authorizationRequested: XCTestExpectation?

    func requestAuthorization() async -> Bool {
        authorizationRequestCount += 1
        authorizationRequested?.fulfill()
        return true
    }

    func deliver(_ request: QueueNotificationRequest) async {
        deliveredRequests.append(request)
    }
}

private extension PersistentQueueItem {
    func replacingStatus(_ state: DurableQueueItemState, message: String = "Temporary failure") throws -> PersistentQueueItem {
        try PersistentQueueItem(item: DurableConversionQueueItem(
            id: id,
            ordinal: ordinal,
            groupID: groupID,
            origin: origin,
            intent: DurableQueueItemIntent(draft: draft),
            inspection: draft.sourceDetails,
            state: state,
            attempts: [],
            decision: state == .attention ? DurableQueueDecision(
                identifier: "source_missing",
                prompt: "Reconnect the source.",
                choices: [WorkerRecoveryChoice.retryContinueOnError.rawValue]
            ) : nil,
            failure: state == .failed ? DurableQueueFailure(
                code: "temporary",
                message: message,
                details: nil,
                retryable: true
            ) : nil,
            result: state == .completed ? DurableQueueResult(outputPath: "/Movies/Feature.mov") : nil,
            resolutionTrace: resolutionTrace
        ))
    }
}

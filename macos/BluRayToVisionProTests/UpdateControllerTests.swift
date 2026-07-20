import Foundation
import XCTest
@testable import BluRayToVisionPro

final class UpdateControllerTests: XCTestCase {
    func testRoutesExposeExactHierarchicalSparkleChannels() {
        XCTAssertEqual(UpdateChannelPreference.allCases.map(\.name), ["Stable", "RC", "Beta", "Alpha"])
        XCTAssertEqual(UpdateChannelPreference.stable.sparkleChannels, [])
        XCTAssertEqual(UpdateChannelPreference.releaseCandidate.sparkleChannels, ["rc"])
        XCTAssertEqual(UpdateChannelPreference.beta.sparkleChannels, ["beta", "rc"])
        XCTAssertEqual(UpdateChannelPreference.alpha.sparkleChannels, ["alpha", "beta", "rc"])
        XCTAssertEqual(UpdateChannelPreference.storedValue(nil), .stable)
        XCTAssertEqual(UpdateChannelPreference.storedValue("stable"), .stable)
        XCTAssertEqual(UpdateChannelPreference.storedValue("rc"), .releaseCandidate)
        XCTAssertEqual(UpdateChannelPreference.storedValue("releaseCandidate"), .releaseCandidate)
        XCTAssertEqual(UpdateChannelPreference.storedValue("nightly"), .stable)
        XCTAssertTrue(UpdateChannelPreference.noDowngradeExplanation.contains("future newer builds"))
        XCTAssertTrue(UpdateChannelPreference.noDowngradeExplanation.contains("never downgrades"))
    }

    func testOnlyBetaAndAlphaRoutesIncludeTheBetaChannel() {
        let routesIncludingBeta = UpdateChannelPreference.allCases.filter { preference in
            preference.sparkleChannels.contains("beta")
        }

        XCTAssertEqual(routesIncludingBeta, [.beta, .alpha])
    }

    @MainActor
    func testDebugBundleRemainsInManualUpdateMode() {
        XCTAssertEqual(UpdateEnvironment().mode, .manual)
    }

    @MainActor
    func testDirectEnvironmentRequiresCompleteFailClosedMetadata() {
        XCTAssertEqual(directEnvironment().mode, .sparkle)
        XCTAssertEqual(directEnvironment(feedURL: "http://example.com/appcast.xml").mode, .manual)
        XCTAssertEqual(directEnvironment(publicKey: nil).mode, .manual)
        XCTAssertEqual(directEnvironment(allowsAutomaticUpdates: true).mode, .manual)
        XCTAssertEqual(directEnvironment(verifiesBeforeExtraction: false).mode, .manual)
        XCTAssertEqual(directEnvironment(automaticChecksSettingPresent: true).mode, .manual)
        XCTAssertEqual(directEnvironment(distributionChannel: "app-store").mode, .appStore)
        XCTAssertEqual(directEnvironment(startupSuppressed: true).mode, .disabled)
    }

    @MainActor
    func testControllerSynchronizesSparklePreferencesAndChannel() {
        let suiteName = "UpdateControllerTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let backend = FakeUpdateBackend(automaticallyChecksForUpdates: false, canCheckForUpdates: true)
        let controller = UpdateController(
            environment: directEnvironment(),
            defaults: defaults,
            backendFactory: { _, _ in backend }
        )

        controller.startIfNeeded()

        XCTAssertTrue(controller.supportsAutomaticChecks)
        XCTAssertTrue(controller.canPerformUpdateAction)
        XCTAssertFalse(controller.automaticallyChecksForUpdates)
        XCTAssertEqual(controller.updateChannel, .stable)

        controller.automaticallyChecksForUpdates = true
        controller.updateChannel = .alpha
        controller.performUpdateAction()

        XCTAssertTrue(backend.automaticallyChecksForUpdates)
        XCTAssertEqual(backend.updateChannel, .alpha)
        XCTAssertEqual(backend.channelChangeCount, 1)
        XCTAssertEqual(defaults.string(forKey: UpdateController.channelDefaultsKey), "alpha")
        XCTAssertEqual(backend.checkCount, 1)
    }

    @MainActor
    func testControllerRefreshesWhenSparkleStateChanges() {
        let backend = FakeUpdateBackend(automaticallyChecksForUpdates: false, canCheckForUpdates: true)
        let controller = UpdateController(
            environment: directEnvironment(),
            defaults: isolatedDefaults(),
            backendFactory: { _, _ in backend }
        )
        controller.startIfNeeded()

        backend.automaticallyChecksForUpdates = true
        backend.canCheckForUpdates = false
        backend.stateDidChange?()

        XCTAssertTrue(controller.automaticallyChecksForUpdates)
        XCTAssertFalse(controller.canCheckForUpdates)
        XCTAssertFalse(controller.canPerformUpdateAction)
    }

    @MainActor
    func testManualModeUsesReleasePageWithoutCreatingSparkle() {
        var openedURL: URL?
        var factoryCalled = false
        let controller = UpdateController(
            environment: directEnvironment(publicKey: nil),
            defaults: isolatedDefaults(),
            openURL: { url in
                openedURL = url
                return true
            },
            backendFactory: { _, _ in
                factoryCalled = true
                return FakeUpdateBackend()
            }
        )

        controller.startIfNeeded()
        controller.performUpdateAction()

        XCTAssertEqual(controller.mode, .manual)
        XCTAssertFalse(controller.supportsAutomaticChecks)
        XCTAssertTrue(controller.canPerformUpdateAction)
        XCTAssertFalse(factoryCalled)
        XCTAssertEqual(openedURL, UpdateController.releasesURL)
    }

    @MainActor
    func testLegacyReleaseCandidatePreferenceMigratesToProductionKey() {
        let suiteName = "UpdateControllerTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }
        defaults.set("releaseCandidate", forKey: UpdateController.legacyChannelDefaultsKey)

        let controller = UpdateController(
            environment: directEnvironment(publicKey: nil),
            defaults: defaults
        )

        XCTAssertEqual(controller.updateChannel, .releaseCandidate)
        XCTAssertEqual(defaults.string(forKey: UpdateController.channelDefaultsKey), "rc")
        XCTAssertNil(defaults.string(forKey: UpdateController.legacyChannelDefaultsKey))
    }

    @MainActor
    func testCurrentReleaseCandidateAliasMigratesToCanonicalValue() {
        let suiteName = "UpdateControllerTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }
        defaults.set("releaseCandidate", forKey: UpdateController.channelDefaultsKey)

        let controller = UpdateController(
            environment: directEnvironment(publicKey: nil),
            defaults: defaults
        )

        XCTAssertEqual(controller.updateChannel, .releaseCandidate)
        XCTAssertEqual(defaults.string(forKey: UpdateController.channelDefaultsKey), "rc")
    }

    @MainActor
    func testRoutePreferencePersistsAcrossControllerRelaunch() {
        let suiteName = "UpdateControllerTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let firstController = UpdateController(
            environment: directEnvironment(publicKey: nil),
            defaults: defaults
        )
        firstController.updateChannel = .beta

        let relaunchedController = UpdateController(
            environment: directEnvironment(publicKey: nil),
            defaults: defaults
        )

        XCTAssertEqual(relaunchedController.updateChannel, .beta)
    }

    @MainActor
    func testPersistedStableAndRCStayUnchangedUntilExplicitRouteSelection() {
        let cases: [(rawValue: String, preference: UpdateChannelPreference)] = [
            ("stable", .stable),
            ("rc", .releaseCandidate),
        ]

        for testCase in cases {
            let defaults = isolatedDefaults()
            let backend = FakeUpdateBackend()
            var initializedChannel: UpdateChannelPreference?
            defaults.set(testCase.rawValue, forKey: UpdateController.channelDefaultsKey)

            let controller = UpdateController(
                environment: directEnvironment(),
                defaults: defaults,
                backendFactory: { channel, _ in
                    initializedChannel = channel
                    return backend
                }
            )
            controller.startIfNeeded()

            XCTAssertEqual(controller.updateChannel, testCase.preference)
            XCTAssertEqual(initializedChannel, testCase.preference)
            XCTAssertEqual(defaults.string(forKey: UpdateController.channelDefaultsKey), testCase.rawValue)
            XCTAssertEqual(backend.channelChangeCount, 0)

            let relaunchedController = UpdateController(
                environment: directEnvironment(publicKey: nil),
                defaults: defaults
            )
            XCTAssertEqual(relaunchedController.updateChannel, testCase.preference)

            controller.updateChannel = .beta

            XCTAssertEqual(defaults.string(forKey: UpdateController.channelDefaultsKey), "beta")
            XCTAssertEqual(backend.channelChangeCount, 1)
        }
    }

    @MainActor
    func testInvalidStoredChannelFallsBackToStable() {
        let suiteName = "UpdateControllerTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }
        defaults.set("nightly", forKey: UpdateController.channelDefaultsKey)

        let controller = UpdateController(
            environment: directEnvironment(publicKey: nil),
            defaults: defaults
        )

        XCTAssertEqual(controller.updateChannel, .stable)
        XCTAssertEqual(defaults.string(forKey: UpdateController.channelDefaultsKey), "stable")
    }

    @MainActor
    func testSparkleInitializationFailureFallsBackToManualUpdates() {
        let controller = UpdateController(
            environment: directEnvironment(),
            defaults: isolatedDefaults(),
            backendFactory: { _, _ in throw TestError.initializationFailed }
        )

        controller.startIfNeeded()

        XCTAssertEqual(controller.mode, .manual)
        XCTAssertNotNil(controller.initializationError)
        XCTAssertTrue(controller.canPerformUpdateAction)
    }

    @MainActor
    private func isolatedDefaults() -> UserDefaults {
        let suiteName = "UpdateControllerTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        addTeardownBlock {
            defaults.removePersistentDomain(forName: suiteName)
        }
        return defaults
    }

    private func directEnvironment(
        distributionChannel: String? = "direct",
        feedURL: String? = "https://example.com/appcast.xml",
        publicKey: String? = "public-key",
        allowsAutomaticUpdates: Bool? = false,
        verifiesBeforeExtraction: Bool? = true,
        automaticChecksSettingPresent: Bool = false,
        startupSuppressed: Bool = false
    ) -> UpdateEnvironment {
        UpdateEnvironment(
            distributionChannel: distributionChannel,
            feedURL: feedURL,
            publicKey: publicKey,
            allowsAutomaticUpdates: allowsAutomaticUpdates,
            verifiesBeforeExtraction: verifiesBeforeExtraction,
            automaticChecksSettingPresent: automaticChecksSettingPresent,
            startupSuppressed: startupSuppressed
        )
    }
}

@MainActor
private final class FakeUpdateBackend: UpdateBackend {
    var stateDidChange: (() -> Void)?
    var automaticallyChecksForUpdates: Bool
    var canCheckForUpdates: Bool
    var updateChannel = UpdateChannelPreference.stable {
        didSet {
            if updateChannel != oldValue {
                channelChangeCount += 1
            }
        }
    }
    private(set) var checkCount = 0
    private(set) var channelChangeCount = 0

    init(
        automaticallyChecksForUpdates: Bool = false,
        canCheckForUpdates: Bool = true
    ) {
        self.automaticallyChecksForUpdates = automaticallyChecksForUpdates
        self.canCheckForUpdates = canCheckForUpdates
    }

    func checkForUpdates() {
        checkCount += 1
    }
}

private enum TestError: Error {
    case initializationFailed
}

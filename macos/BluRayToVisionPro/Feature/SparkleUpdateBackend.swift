import Foundation
import Sparkle

@MainActor
final class SparkleUpdateBackend: NSObject, UpdateBackend, SPUUpdaterDelegate {
    var stateDidChange: (() -> Void)?
    var updateChannel: UpdateChannelPreference {
        didSet {
            guard updateChannel != oldValue else {
                return
            }
            controller.updater.resetUpdateCycleAfterShortDelay()
        }
    }

    var automaticallyChecksForUpdates: Bool {
        get { controller.updater.automaticallyChecksForUpdates }
        set { controller.updater.automaticallyChecksForUpdates = newValue }
    }

    var canCheckForUpdates: Bool {
        controller.updater.canCheckForUpdates
    }

    private var controller: SPUStandardUpdaterController!
    private weak var installPostponer: (any UpdateInstallPostponing)?
    private var observations: [NSKeyValueObservation] = []

    init(
        updateChannel: UpdateChannelPreference,
        installPostponer: (any UpdateInstallPostponing)?
    ) {
        self.updateChannel = updateChannel
        self.installPostponer = installPostponer
        super.init()
        controller = SPUStandardUpdaterController(
            startingUpdater: true,
            updaterDelegate: self,
            userDriverDelegate: nil
        )
        observations = [
            controller.updater.observe(\.automaticallyChecksForUpdates, options: [.new]) { [weak self] _, _ in
                Task { @MainActor in
                    self?.stateDidChange?()
                }
            },
            controller.updater.observe(\.canCheckForUpdates, options: [.new]) { [weak self] _, _ in
                Task { @MainActor in
                    self?.stateDidChange?()
                }
            },
        ]
    }

    func checkForUpdates() {
        controller.checkForUpdates(nil)
    }

    func allowedChannels(for updater: SPUUpdater) -> Set<String> {
        updateChannel.sparkleChannels
    }

    func updater(
        _ updater: SPUUpdater,
        shouldPostponeRelaunchForUpdate item: SUAppcastItem,
        untilInvokingBlock installHandler: @escaping () -> Void
    ) -> Bool {
        installPostponer?.postponeInstallUntilIdle(installHandler) ?? false
    }
}

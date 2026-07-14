import AppKit
import Foundation

enum UpdateChannelPreference: String, CaseIterable, Identifiable {
    case stable
    case releaseCandidate = "rc"

    var id: String { rawValue }

    var name: String {
        switch self {
        case .stable:
            "Stable"
        case .releaseCandidate:
            "Release Candidates"
        }
    }

    var sparkleChannels: Set<String> {
        switch self {
        case .stable:
            []
        case .releaseCandidate:
            ["rc"]
        }
    }

    static func storedValue(_ rawValue: String?) -> UpdateChannelPreference {
        if rawValue == "releaseCandidate" {
            return .releaseCandidate
        }
        return UpdateChannelPreference(rawValue: rawValue ?? "") ?? .stable
    }
}

enum UpdateMode: Equatable {
    case sparkle
    case manual
    case appStore
    case disabled
}

struct UpdateEnvironment: Equatable {
    let distributionChannel: String?
    let feedURL: String?
    let publicKey: String?
    let allowsAutomaticUpdates: Bool?
    let verifiesBeforeExtraction: Bool?
    let automaticChecksSettingPresent: Bool
    let startupSuppressed: Bool

    init(
        distributionChannel: String?,
        feedURL: String?,
        publicKey: String?,
        allowsAutomaticUpdates: Bool?,
        verifiesBeforeExtraction: Bool?,
        automaticChecksSettingPresent: Bool,
        startupSuppressed: Bool = false
    ) {
        self.distributionChannel = distributionChannel
        self.feedURL = feedURL
        self.publicKey = publicKey
        self.allowsAutomaticUpdates = allowsAutomaticUpdates
        self.verifiesBeforeExtraction = verifiesBeforeExtraction
        self.automaticChecksSettingPresent = automaticChecksSettingPresent
        self.startupSuppressed = startupSuppressed
    }

    init(
        bundle: Bundle = .main,
        arguments: [String] = ProcessInfo.processInfo.arguments,
        processEnvironment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        func stringValue(_ key: String) -> String? {
            guard let value = bundle.object(forInfoDictionaryKey: key) else {
                return nil
            }
            let text = String(describing: value).trimmingCharacters(in: .whitespacesAndNewlines)
            return text.isEmpty ? nil : text
        }

        func boolValue(_ key: String) -> Bool? {
            guard let value = bundle.object(forInfoDictionaryKey: key) else {
                return nil
            }
            if let value = value as? Bool {
                return value
            }
            if let value = value as? NSNumber {
                return value.boolValue
            }
            return nil
        }

        distributionChannel = stringValue("BDToAVPDistributionChannel")
        feedURL = stringValue("SUFeedURL")
        publicKey = stringValue("SUPublicEDKey")
        allowsAutomaticUpdates = boolValue("SUAllowsAutomaticUpdates")
        verifiesBeforeExtraction = boolValue("SUVerifyUpdateBeforeExtraction")
        automaticChecksSettingPresent = bundle.object(forInfoDictionaryKey: "SUEnableAutomaticChecks") != nil
        startupSuppressed = AppDelegate.isStartupSmoke(arguments: arguments)
            || processEnvironment["XCODE_RUNNING_FOR_PREVIEWS"] == "1"
    }

    var mode: UpdateMode {
        if startupSuppressed {
            return .disabled
        }
        if distributionChannel == "app-store" {
            return .appStore
        }
        guard distributionChannel == "direct",
              let feedURL,
              let url = URL(string: feedURL),
              url.scheme == "https",
              url.host != nil,
              let publicKey,
              !publicKey.isEmpty,
              allowsAutomaticUpdates == false,
              verifiesBeforeExtraction == true,
              !automaticChecksSettingPresent
        else {
            return .manual
        }
        return .sparkle
    }
}

@MainActor
protocol UpdateInstallPostponing: AnyObject {
    func postponeInstallUntilIdle(_ installHandler: @escaping () -> Void) -> Bool
}

@MainActor
protocol UpdateBackend: AnyObject {
    var stateDidChange: (() -> Void)? { get set }
    var automaticallyChecksForUpdates: Bool { get set }
    var canCheckForUpdates: Bool { get }
    var updateChannel: UpdateChannelPreference { get set }

    func checkForUpdates()
}

@MainActor
final class UpdateController: ObservableObject {
    static let channelDefaultsKey = "BDToAVPUpdateChannel"
    static let legacyChannelDefaultsKey = "native.updateChannel"
    static let releasesURL = URL(string: "https://github.com/cbusillo/BD_to_AVP/releases")!

    typealias BackendFactory = @MainActor (
        UpdateChannelPreference,
        (any UpdateInstallPostponing)?
    ) throws -> any UpdateBackend

    @Published private(set) var mode: UpdateMode
    @Published var automaticallyChecksForUpdates: Bool {
        didSet {
            guard !isSynchronizingBackend,
                  automaticallyChecksForUpdates != oldValue else {
                return
            }
            backend?.automaticallyChecksForUpdates = automaticallyChecksForUpdates
        }
    }

    @Published var updateChannel: UpdateChannelPreference {
        didSet {
            guard updateChannel != oldValue else {
                return
            }
            defaults.set(updateChannel.rawValue, forKey: Self.channelDefaultsKey)
            if !isSynchronizingBackend {
                backend?.updateChannel = updateChannel
            }
        }
    }

    @Published private(set) var canCheckForUpdates = false
    private(set) var initializationError: Error?

    private let defaults: UserDefaults
    private let backendFactory: BackendFactory
    private let openURL: (URL) -> Bool
    private weak var installPostponer: (any UpdateInstallPostponing)?
    private var backend: (any UpdateBackend)?
    private var didStart = false
    private var isSynchronizingBackend = false

    init(
        environment: UpdateEnvironment = UpdateEnvironment(),
        defaults: UserDefaults = .standard,
        installPostponer: (any UpdateInstallPostponing)? = nil,
        openURL: @escaping (URL) -> Bool = { NSWorkspace.shared.open($0) },
        backendFactory: @escaping BackendFactory = { channel, postponer in
            SparkleUpdateBackend(updateChannel: channel, installPostponer: postponer)
        }
    ) {
        self.defaults = defaults
        self.installPostponer = installPostponer
        self.openURL = openURL
        self.backendFactory = backendFactory
        mode = environment.mode
        automaticallyChecksForUpdates = false
        updateChannel = Self.readChannel(from: defaults)
        migrateLegacyChannelIfNeeded(defaults: defaults, channel: updateChannel)
    }

    var supportsAutomaticChecks: Bool {
        mode == .sparkle && backend != nil
    }

    var supportsChannels: Bool {
        supportsAutomaticChecks
    }

    var canPerformUpdateAction: Bool {
        switch mode {
        case .sparkle:
            canCheckForUpdates
        case .manual:
            true
        case .appStore, .disabled:
            false
        }
    }

    var updateActionTitle: String {
        mode == .sparkle ? "Check for Updates…" : "View Available Releases…"
    }

    var unavailableReason: String {
        switch mode {
        case .sparkle:
            ""
        case .manual:
            "Automatic update checks aren’t available in this build. Releases remain available for manual download."
        case .appStore:
            "Updates for this build are managed by the App Store."
        case .disabled:
            "Update checks are disabled for this launch."
        }
    }

    func startIfNeeded() {
        guard !didStart else {
            return
        }
        didStart = true
        guard mode == .sparkle else {
            return
        }

        do {
            let backend = try backendFactory(updateChannel, installPostponer)
            self.backend = backend
            backend.stateDidChange = { [weak self] in
                self?.refreshFromBackend()
            }
            refreshFromBackend()
        } catch {
            initializationError = error
            mode = .manual
            backend = nil
        }
    }

    func performUpdateAction() {
        switch mode {
        case .sparkle:
            guard canCheckForUpdates else {
                return
            }
            backend?.checkForUpdates()
        case .manual:
            _ = openURL(Self.releasesURL)
        case .appStore, .disabled:
            return
        }
    }

    private func refreshFromBackend() {
        guard let backend else {
            return
        }
        isSynchronizingBackend = true
        automaticallyChecksForUpdates = backend.automaticallyChecksForUpdates
        canCheckForUpdates = backend.canCheckForUpdates
        isSynchronizingBackend = false
    }

    private static func readChannel(from defaults: UserDefaults) -> UpdateChannelPreference {
        if let currentValue = defaults.string(forKey: channelDefaultsKey) {
            return .storedValue(currentValue)
        }
        return .storedValue(defaults.string(forKey: legacyChannelDefaultsKey))
    }

    private func migrateLegacyChannelIfNeeded(defaults: UserDefaults, channel: UpdateChannelPreference) {
        guard defaults.string(forKey: Self.channelDefaultsKey) == nil,
              defaults.string(forKey: Self.legacyChannelDefaultsKey) != nil else {
            return
        }
        defaults.set(channel.rawValue, forKey: Self.channelDefaultsKey)
        defaults.removeObject(forKey: Self.legacyChannelDefaultsKey)
    }
}

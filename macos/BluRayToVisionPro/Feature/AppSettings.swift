import Foundation

enum UpdateChannelPreference: String, CaseIterable, Identifiable {
    case stable
    case releaseCandidate

    var id: String { rawValue }

    var name: String {
        switch self {
        case .stable:
            "Stable"
        case .releaseCandidate:
            "Release Candidates"
        }
    }
}

@MainActor
final class AppSettings: ObservableObject {
    private enum Key {
        static let profile = "native.defaultProfile"
        static let destination = "native.defaultDestination"
        static let revealOutput = "native.revealOutput"
        static let playSound = "native.playSound"
        static let keepAwake = "native.keepAwake"
        static let automaticUpdates = "native.automaticUpdates"
        static let updateChannel = "native.updateChannel"
        static let showTechnicalDetails = "native.showTechnicalDetails"
        static let keepIntermediateFiles = "native.keepIntermediateFiles"
        static let useSoftwareEncoder = "native.useSoftwareEncoder"
    }

    private let defaults: UserDefaults
    private let fallbackDestinationURL: URL

    @Published var selectedProfileID: String {
        didSet { defaults.set(selectedProfileID, forKey: Key.profile) }
    }

    @Published var destinationURL: URL {
        didSet { defaults.set(destinationURL.path, forKey: Key.destination) }
    }

    @Published var revealOutput: Bool {
        didSet { defaults.set(revealOutput, forKey: Key.revealOutput) }
    }

    @Published var playSound: Bool {
        didSet { defaults.set(playSound, forKey: Key.playSound) }
    }

    @Published var keepAwake: Bool {
        didSet { defaults.set(keepAwake, forKey: Key.keepAwake) }
    }

    @Published var automaticallyChecksForUpdates: Bool {
        didSet { defaults.set(automaticallyChecksForUpdates, forKey: Key.automaticUpdates) }
    }

    @Published var updateChannel: UpdateChannelPreference {
        didSet { defaults.set(updateChannel.rawValue, forKey: Key.updateChannel) }
    }

    @Published var showTechnicalDetails: Bool {
        didSet { defaults.set(showTechnicalDetails, forKey: Key.showTechnicalDetails) }
    }

    @Published var keepIntermediateFiles: Bool {
        didSet { defaults.set(keepIntermediateFiles, forKey: Key.keepIntermediateFiles) }
    }

    @Published var useSoftwareEncoder: Bool {
        didSet { defaults.set(useSoftwareEncoder, forKey: Key.useSoftwareEncoder) }
    }

    init(
        defaults: UserDefaults = .standard,
        homeDirectoryURL: URL = FileManager.default.homeDirectoryForCurrentUser
    ) {
        self.defaults = defaults
        fallbackDestinationURL = homeDirectoryURL.appendingPathComponent("Movies", isDirectory: true)
        selectedProfileID = defaults.string(forKey: Key.profile) ?? BuiltInProfile.balanced.id
        destinationURL = URL(
            fileURLWithPath: defaults.string(forKey: Key.destination) ?? fallbackDestinationURL.path,
            isDirectory: true
        )
        revealOutput = defaults.object(forKey: Key.revealOutput) as? Bool ?? true
        playSound = defaults.object(forKey: Key.playSound) as? Bool ?? true
        keepAwake = defaults.object(forKey: Key.keepAwake) as? Bool ?? true
        automaticallyChecksForUpdates = defaults.object(forKey: Key.automaticUpdates) as? Bool ?? true
        updateChannel = UpdateChannelPreference(
            rawValue: defaults.string(forKey: Key.updateChannel) ?? UpdateChannelPreference.stable.rawValue
        ) ?? .stable
        showTechnicalDetails = defaults.object(forKey: Key.showTechnicalDetails) as? Bool ?? false
        keepIntermediateFiles = defaults.object(forKey: Key.keepIntermediateFiles) as? Bool ?? false
        useSoftwareEncoder = defaults.object(forKey: Key.useSoftwareEncoder) as? Bool ?? false
    }

    func resetAdvancedSettings() {
        showTechnicalDetails = false
        keepIntermediateFiles = false
        useSoftwareEncoder = false
    }

    func normalize(for capabilities: AppCapabilities) {
        if !capabilities.automaticUpdateChecksAvailable {
            automaticallyChecksForUpdates = false
        }
    }

    func resetDestination() {
        destinationURL = fallbackDestinationURL
    }
}

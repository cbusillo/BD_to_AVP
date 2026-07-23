import Foundation

@MainActor
final class AppSettings: ObservableObject {
    private enum Key {
        static let profile = "native.defaultProfile"
        static let destination = "native.defaultDestination"
        static let revealOutput = "native.revealOutput"
        static let playSound = "native.playSound"
        static let keepAwake = "native.keepAwake"
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

    @Published var showTechnicalDetails: Bool {
        didSet { defaults.set(showTechnicalDetails, forKey: Key.showTechnicalDetails) }
    }

    @Published var intermediatePolicy: IntermediatePolicy {
        didSet {
            defaults.set(
                intermediatePolicy.createsReusableArtifacts,
                forKey: Key.keepIntermediateFiles
            )
        }
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
        showTechnicalDetails = defaults.object(forKey: Key.showTechnicalDetails) as? Bool ?? false
        intermediatePolicy = IntermediatePolicy(
            legacyKeepStageFiles: defaults.object(forKey: Key.keepIntermediateFiles) as? Bool ?? false
        )
        useSoftwareEncoder = defaults.object(forKey: Key.useSoftwareEncoder) as? Bool ?? false
    }

    func resetAdvancedSettings() {
        showTechnicalDetails = false
        intermediatePolicy = .automatic
        useSoftwareEncoder = false
    }

    func resetDestination() {
        destinationURL = fallbackDestinationURL
    }
}

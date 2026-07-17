import Foundation
import SwiftUI

@MainActor
final class ProfileStore: ObservableObject {
    static let balancedProfileID = BuiltInProfile.balanced.id

    @Published private(set) var customProfiles: [EncodingProfile] = []
    @Published private(set) var loadErrorMessage: String?

    private let fileManager: FileManager
    private let fileURL: URL
    private let idGenerator: () -> UUID
    private let dataWriter: (Data, URL) throws -> Void
    private var writesBlocked = false

    init(
        fileURL: URL? = nil,
        fileManager: FileManager = .default,
        idGenerator: @escaping () -> UUID = UUID.init,
        dataWriter: @escaping (Data, URL) throws -> Void = { data, url in
            try data.write(to: url, options: .atomic)
        }
    ) {
        self.fileManager = fileManager
        self.fileURL = fileURL ?? Self.defaultFileURL(fileManager: fileManager)
        self.idGenerator = idGenerator
        self.dataWriter = dataWriter
        loadProfiles()
    }

    var profiles: [EncodingProfile] {
        BuiltInProfile.allCases.map(\.profile) + customProfiles
    }

    func profile(withID identifier: String) -> EncodingProfile {
        profiles.first(where: { $0.id == identifier }) ?? BuiltInProfile.balanced.profile
    }

    func normalizedProfileID(_ identifier: String) -> String {
        let migratedIdentifier = switch identifier {
        case "balanced":
            BuiltInProfile.balanced.id
        case "originalResolution":
            BuiltInProfile.originalResolution.id
        case "fourKUpscale":
            BuiltInProfile.fourKUpscale.id
        default:
            identifier
        }
        return profile(withID: migratedIdentifier).id
    }

    func createProfile(name: String, options: EncodingOptions) throws -> String {
        let normalizedName = try validatedName(name)
        let profile = EncodingProfile(
            id: "custom.\(idGenerator().uuidString.lowercased())",
            name: normalizedName,
            options: options,
            kind: .custom,
            systemImage: "slider.horizontal.3"
        )
        var updatedProfiles = customProfiles
        updatedProfiles.append(profile)
        try persist(updatedProfiles)
        customProfiles = updatedProfiles
        return profile.id
    }

    func duplicateProfile(_ identifier: String) throws -> String {
        let source = profile(withID: identifier)
        return try createProfile(
            name: suggestedDuplicateName(for: source.name),
            options: source.options
        )
    }

    func updateProfile(
        _ identifier: String,
        name: String,
        options: EncodingOptions
    ) throws {
        guard let index = customProfiles.firstIndex(where: { $0.id == identifier }) else {
            throw ProfileStoreError.builtInProfileIsReadOnly
        }
        let normalizedName = try validatedName(name, excluding: identifier)
        var updatedProfiles = customProfiles
        updatedProfiles[index].name = normalizedName
        updatedProfiles[index].options = options
        try persist(updatedProfiles)
        customProfiles = updatedProfiles
    }

    func deleteProfile(_ identifier: String) throws {
        guard customProfiles.contains(where: { $0.id == identifier }) else {
            throw ProfileStoreError.builtInProfileIsReadOnly
        }
        let updatedProfiles = customProfiles.filter { $0.id != identifier }
        try persist(updatedProfiles)
        customProfiles = updatedProfiles
    }

    func moveCustomProfiles(fromOffsets offsets: IndexSet, toOffset destination: Int) throws {
        var updatedProfiles = customProfiles
        updatedProfiles.move(fromOffsets: offsets, toOffset: destination)
        try persist(updatedProfiles)
        customProfiles = updatedProfiles
    }

    func suggestedDuplicateName(for sourceName: String) -> String {
        let baseName = "\(sourceName) Copy"
        if isNameAvailable(baseName) {
            return baseName
        }
        var suffix = 2
        while !isNameAvailable("\(baseName) \(suffix)") {
            suffix += 1
        }
        return "\(baseName) \(suffix)"
    }

    private func validatedName(_ name: String, excluding identifier: String? = nil) throws -> String {
        let normalizedName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalizedName.isEmpty else {
            throw ProfileStoreError.emptyName
        }
        guard isNameAvailable(normalizedName, excluding: identifier) else {
            throw ProfileStoreError.duplicateName(normalizedName)
        }
        return normalizedName
    }

    private func isNameAvailable(_ name: String, excluding identifier: String? = nil) -> Bool {
        !profiles.contains {
            $0.id != identifier && $0.name.localizedCaseInsensitiveCompare(name) == .orderedSame
        }
    }

    private func loadProfiles() {
        guard fileManager.fileExists(atPath: fileURL.path) else {
            return
        }
        do {
            let data = try Data(contentsOf: fileURL)
            let decoder = JSONDecoder()
            let version = try decoder.decode(ProfileDocumentVersion.self, from: data).version
            let storedProfiles: [StoredProfile]
            let needsMigration: Bool
            switch version {
            case 1:
                let legacyDocument = try decoder.decode(LegacyProfileDocumentV1.self, from: data)
                storedProfiles = try legacyDocument.profiles.map { try $0.migrated() }
                needsMigration = true
            case 2:
                let legacyDocument = try decoder.decode(LegacyProfileDocumentV2.self, from: data)
                storedProfiles = legacyDocument.profiles.map { $0.migrated() }
                needsMigration = true
            case ProfileDocument.currentVersion:
                storedProfiles = try decoder.decode(ProfileDocument.self, from: data).profiles
                needsMigration = false
            default:
                throw ProfileStoreError.unsupportedVersion(version)
            }
            let loadedProfiles = try restoreProfiles(storedProfiles)
            if needsMigration {
                do {
                    try persist(loadedProfiles)
                } catch {
                    writesBlocked = true
                    customProfiles = loadedProfiles
                    loadErrorMessage = "Custom profiles were loaded but could not be upgraded. Profile changes are disabled to protect the original library."
                    return
                }
            }
            customProfiles = loadedProfiles
        } catch {
            if let recoveryURL = preserveUnreadableFile() {
                loadErrorMessage = "Custom profiles could not be loaded. The original library was preserved as \(recoveryURL.lastPathComponent)."
            } else {
                writesBlocked = true
                loadErrorMessage = "Custom profiles could not be loaded or preserved. Profile changes are disabled to protect the original library."
            }
            customProfiles = []
        }
    }

    private func restoreProfiles(_ storedProfiles: [StoredProfile]) throws -> [EncodingProfile] {
        var identifiers = Set<String>()
        var names = Set<String>()
        return try storedProfiles.map { storedProfile in
            let identifier = "custom.\(storedProfile.id.uuidString.lowercased())"
            let normalizedName = storedProfile.name.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !normalizedName.isEmpty,
                  identifiers.insert(identifier).inserted,
                  names.insert(normalizedName.lowercased()).inserted
            else {
                throw ProfileStoreError.invalidDocument
            }
            return EncodingProfile(
                id: identifier,
                name: normalizedName,
                options: storedProfile.options,
                kind: .custom,
                systemImage: "slider.horizontal.3"
            )
        }
    }

    private func persist(_ profiles: [EncodingProfile]) throws {
        guard !writesBlocked else {
            throw ProfileStoreError.recoveryRequired
        }
        let directoryURL = fileURL.deletingLastPathComponent()
        try fileManager.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let storedProfiles = try profiles.map { profile -> StoredProfile in
            guard profile.isCustom,
                  let identifier = UUID(uuidString: profile.id.replacingOccurrences(of: "custom.", with: ""))
            else {
                throw ProfileStoreError.invalidDocument
            }
            return StoredProfile(id: identifier, name: profile.name, options: profile.options)
        }
        let document = ProfileDocument(version: ProfileDocument.currentVersion, profiles: storedProfiles)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(document)
        try dataWriter(data, fileURL)
        loadErrorMessage = nil
    }

    private func preserveUnreadableFile() -> URL? {
        var recoveryURL = fileURL.appendingPathExtension("corrupt")
        var suffix = 2
        while fileManager.fileExists(atPath: recoveryURL.path) {
            recoveryURL = fileURL.appendingPathExtension("corrupt-\(suffix)")
            suffix += 1
        }
        do {
            try fileManager.moveItem(at: fileURL, to: recoveryURL)
            return recoveryURL
        } catch {
            return nil
        }
    }

    private static func defaultFileURL(fileManager: FileManager) -> URL {
        let applicationSupportURL = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        return applicationSupportURL
            .appendingPathComponent("3D Blu-ray to Vision Pro", isDirectory: true)
            .appendingPathComponent("profiles.json")
    }
}

enum ProfileStoreError: LocalizedError, Equatable {
    case builtInProfileIsReadOnly
    case duplicateName(String)
    case emptyName
    case invalidDocument
    case recoveryRequired
    case unsupportedVersion(Int)

    var errorDescription: String? {
        switch self {
        case .builtInProfileIsReadOnly:
            "Built-in profiles are read-only. Duplicate the profile to customize it."
        case let .duplicateName(name):
            "A profile named “\(name)” already exists."
        case .emptyName:
            "Enter a name for this profile."
        case .invalidDocument:
            "The profile library contains invalid data."
        case .recoveryRequired:
            "Profile changes are disabled until the unreadable profile library can be preserved."
        case let .unsupportedVersion(version):
            "Profile library version \(version) is not supported."
        }
    }
}

private struct ProfileDocument: Codable {
    static let currentVersion = 3

    let version: Int
    let profiles: [StoredProfile]
}

private struct ProfileDocumentVersion: Decodable {
    let version: Int
}

private struct StoredProfile: Codable {
    let id: UUID
    let name: String
    let options: EncodingOptions
}

private struct LegacyProfileDocumentV2: Decodable {
    let version: Int
    let profiles: [LegacyStoredProfileV2]
}

private struct LegacyStoredProfileV2: Decodable {
    let id: UUID
    let name: String
    let options: LegacyEncodingOptionsV2

    func migrated() -> StoredProfile {
        StoredProfile(id: id, name: name, options: options.migrated())
    }
}

private struct LegacyEncodingOptionsV2: Decodable {
    let hevcQuality: Int
    let leftRightBitrate: Int
    let upscaleEnabled: Bool
    let upscaleQuality: Int
    let linkQuality: Bool
    let fieldOfView: Int
    let frameRateOverride: String
    let resolutionOverride: String
    let cropBlackBars: Bool
    let swapEyes: Bool
    let audioHandling: AudioHandling
    let audioBitrate: Int
    let subtitles: SubtitlePolicy

    func migrated() -> EncodingOptions {
        EncodingOptions(
            videoOutputMode: .mvHEVC,
            av1CRF: 32,
            hevcQuality: hevcQuality,
            leftRightBitrate: leftRightBitrate,
            upscaleEnabled: upscaleEnabled,
            upscaleQuality: upscaleQuality,
            linkQuality: linkQuality,
            fieldOfView: fieldOfView,
            frameRateOverride: frameRateOverride,
            resolutionOverride: resolutionOverride,
            cropBlackBars: cropBlackBars,
            swapEyes: swapEyes,
            audioHandling: audioHandling,
            audioBitrate: audioBitrate,
            subtitles: subtitles
        )
    }
}

private struct LegacyProfileDocumentV1: Decodable {
    let version: Int
    let profiles: [LegacyStoredProfileV1]
}

private struct LegacyStoredProfileV1: Decodable {
    let id: UUID
    let name: String
    let options: LegacyEncodingOptionsV1

    func migrated() throws -> StoredProfile {
        StoredProfile(id: id, name: name, options: try options.migrated())
    }
}

private struct LegacyEncodingOptionsV1: Decodable {
    let hevcQuality: Int
    let leftRightBitrate: Int
    let upscaleEnabled: Bool
    let upscaleQuality: Int
    let linkQuality: Bool
    let fieldOfView: Int
    let frameRateOverride: String
    let resolutionOverride: String
    let cropBlackBars: Bool
    let swapEyes: Bool
    let audioHandling: AudioHandling
    let audioBitrate: Int
    let language: String
    let includeSubtitles: Bool
    let keepExtraLanguages: Bool

    func migrated() throws -> EncodingOptions {
        guard let preferredLanguage = SubtitleLanguage(code: language) else {
            throw ProfileStoreError.invalidDocument
        }
        let mode: SubtitleMode = if !includeSubtitles {
            .off
        } else if keepExtraLanguages {
            .preferredPlusOthers
        } else {
            .preferredOnly
        }
        return EncodingOptions(
            hevcQuality: hevcQuality,
            leftRightBitrate: leftRightBitrate,
            upscaleEnabled: upscaleEnabled,
            upscaleQuality: upscaleQuality,
            linkQuality: linkQuality,
            fieldOfView: fieldOfView,
            frameRateOverride: frameRateOverride,
            resolutionOverride: resolutionOverride,
            cropBlackBars: cropBlackBars,
            swapEyes: swapEyes,
            audioHandling: audioHandling,
            audioBitrate: audioBitrate,
            subtitles: SubtitlePolicy(mode: mode, preferredLanguage: preferredLanguage)
        )
    }
}

import SwiftUI

struct SettingsView: View {
    @ObservedObject var settings: AppSettings
    @ObservedObject var profileStore: ProfileStore
    @ObservedObject var updater: UpdateController

    var body: some View {
        TabView {
            GeneralSettingsPane(settings: settings, profileStore: profileStore)
                .tabItem { Label("General", systemImage: "gearshape") }

            ProfilesSettingsPane(settings: settings, profileStore: profileStore)
                .tabItem { Label("Profiles", systemImage: "slider.horizontal.3") }

            UpdatesSettingsPane(updater: updater)
                .tabItem { Label("Updates", systemImage: "arrow.triangle.2.circlepath") }

            AdvancedSettingsPane(settings: settings)
                .tabItem { Label("Advanced", systemImage: "wrench.and.screwdriver") }
        }
        .padding(20)
        .frame(
            minWidth: 820,
            idealWidth: 900,
            maxWidth: .infinity,
            minHeight: 560,
            idealHeight: 680,
            maxHeight: .infinity
        )
    }
}

private struct GeneralSettingsPane: View {
    @ObservedObject var settings: AppSettings
    @ObservedObject var profileStore: ProfileStore

    var body: some View {
        Form {
            SettingsPaneHeader(
                title: "General",
                subtitle: "Choose the defaults used for new conversions."
            )

            Section("New Conversions") {
                Picker("Default profile", selection: $settings.selectedProfileID) {
                    ForEach(profileStore.profiles) { profile in
                        Text(profile.name).tag(profile.id)
                    }
                }

                LabeledContent("Default destination") {
                    HStack(spacing: 8) {
                        Text(settings.destinationURL.path)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .frame(maxWidth: 420, alignment: .trailing)
                        Button("Choose…") {
                            if let destination = DestinationPicker.chooseDestination(
                                startingAt: settings.destinationURL
                            ) {
                                settings.destinationURL = destination
                            }
                        }
                    }
                }
            }

            Section("When a Conversion Finishes") {
                Toggle("Reveal the output in Finder", isOn: $settings.revealOutput)
                Toggle("Play a sound", isOn: $settings.playSound)
                Toggle("Keep the Mac awake while converting", isOn: $settings.keepAwake)
            }
        }
        .formStyle(.grouped)
    }
}

private struct ProfilesSettingsPane: View {
    @ObservedObject var settings: AppSettings
    @ObservedObject var profileStore: ProfileStore

    @State private var selectedProfileID: String?
    @State private var editorRequest: ProfileEditorRequest?
    @State private var profileToDelete: EncodingProfile?
    @State private var errorMessage: String?
    @State private var selectedSection = EncodingOptionsSection.video

    private var selectedProfile: EncodingProfile? {
        guard let selectedProfileID else {
            return nil
        }
        return profileStore.profiles.first { $0.id == selectedProfileID }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            SettingsPaneHeader(
                title: "Profiles",
                subtitle: "Built-in profiles are templates. Duplicate one or create your own reusable media settings."
            )

            if let loadErrorMessage = profileStore.loadErrorMessage {
                Label(loadErrorMessage, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }

            HSplitView {
                profileList
                    .frame(minWidth: 220, idealWidth: 240, maxWidth: 280)

                profileDetail
                    .frame(minWidth: 540)
            }
        }
        .onAppear {
            selectedProfileID = profileStore.normalizedProfileID(
                selectedProfileID ?? settings.selectedProfileID
            )
        }
        .onChange(of: profileStore.customProfiles) { _, _ in
            guard let selectedProfileID else {
                return
            }
            let normalizedIdentifier = profileStore.normalizedProfileID(selectedProfileID)
            if normalizedIdentifier != selectedProfileID {
                self.selectedProfileID = normalizedIdentifier
            }
            settings.selectedProfileID = profileStore.normalizedProfileID(settings.selectedProfileID)
        }
        .sheet(item: $editorRequest) { request in
            ProfileEditorSheet(request: request) { mode, name, options in
                switch mode {
                case .create:
                    selectedProfileID = try profileStore.createProfile(name: name, options: options)
                case let .update(identifier):
                    try profileStore.updateProfile(identifier, name: name, options: options)
                    selectedProfileID = identifier
                }
            }
        }
        .alert(
            "Delete Profile?",
            isPresented: Binding(
                get: { profileToDelete != nil },
                set: { if !$0 { profileToDelete = nil } }
            ),
            presenting: profileToDelete
        ) { profile in
            Button("Delete", role: .destructive) {
                delete(profile)
            }
            Button("Cancel", role: .cancel) {}
        } message: { profile in
            Text("“\(profile.name)” will be removed. Existing conversion jobs keep their current settings.")
        }
        .alert(
            "Profile Could Not Be Updated",
            isPresented: Binding(
                get: { errorMessage != nil },
                set: { if !$0 { errorMessage = nil } }
            )
        ) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(errorMessage ?? "The profile could not be updated.")
        }
    }

    private var profileList: some View {
        VStack(spacing: 8) {
            List(selection: $selectedProfileID) {
                Section("Built-In") {
                    ForEach(profileStore.profiles.filter(\.isBuiltIn)) { profile in
                        ProfileListRow(profile: profile, isDefault: profile.id == settings.selectedProfileID)
                            .tag(profile.id)
                    }
                }

                Section("My Profiles") {
                    if profileStore.customProfiles.isEmpty {
                        Text("No custom profiles")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(profileStore.customProfiles) { profile in
                            ProfileListRow(profile: profile, isDefault: profile.id == settings.selectedProfileID)
                                .tag(profile.id)
                        }
                    }
                }
            }
            .listStyle(.sidebar)

            HStack(spacing: 6) {
                Button {
                    editorRequest = ProfileEditorRequest(
                        mode: .create,
                        name: "New Profile",
                        options: BuiltInProfile.balanced.options
                    )
                } label: {
                    Label("New Profile", systemImage: "plus")
                        .labelStyle(.iconOnly)
                }
                .help("Create a new profile")

                Button(action: duplicateSelectedProfile) {
                    Label("Duplicate Profile", systemImage: "plus.square.on.square")
                        .labelStyle(.iconOnly)
                }
                .help("Duplicate the selected profile")
                .disabled(selectedProfile == nil)

                Button(action: moveSelectedProfileUp) {
                    Label("Move Profile Up", systemImage: "arrow.up")
                        .labelStyle(.iconOnly)
                }
                .help("Move the selected custom profile up")
                .disabled(!canMoveSelectedProfileUp)

                Button(action: moveSelectedProfileDown) {
                    Label("Move Profile Down", systemImage: "arrow.down")
                        .labelStyle(.iconOnly)
                }
                .help("Move the selected custom profile down")
                .disabled(!canMoveSelectedProfileDown)

                Spacer()

                Button(role: .destructive) {
                    profileToDelete = selectedProfile?.isCustom == true ? selectedProfile : nil
                } label: {
                    Label("Delete Profile", systemImage: "minus")
                        .labelStyle(.iconOnly)
                }
                .help("Delete the selected custom profile")
                .disabled(selectedProfile?.isCustom != true)
            }
            .buttonStyle(.borderless)
            .padding(.horizontal, 8)
        }
    }

    @ViewBuilder
    private var profileDetail: some View {
        if let profile = selectedProfile {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .center, spacing: 10) {
                    Image(systemName: profile.systemImage)
                        .font(.title2)
                        .foregroundStyle(Color.accentColor)
                        .accessibilityHidden(true)

                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 8) {
                            Text(profile.name)
                                .font(.title2.weight(.semibold))
                            Text(profile.isBuiltIn ? "Built-In" : "Custom")
                                .font(.caption.weight(.medium))
                                .foregroundStyle(.secondary)
                                .padding(.horizontal, 7)
                                .padding(.vertical, 3)
                                .background(.quaternary, in: Capsule())
                        }
                        Text(profile.summary)
                            .foregroundStyle(.secondary)
                    }

                    Spacer()

                    if profile.id != settings.selectedProfileID {
                        Button("Make Default") {
                            settings.selectedProfileID = profile.id
                        }
                    } else {
                        Label("Default", systemImage: "checkmark.circle.fill")
                            .foregroundStyle(.green)
                    }

                    if profile.isCustom {
                        Button("Edit…") {
                            editorRequest = ProfileEditorRequest(
                                mode: .update(profile.id),
                                name: profile.name,
                                options: profile.options
                            )
                        }
                        .buttonStyle(.borderedProminent)
                    } else {
                        Button("Duplicate…", action: duplicateSelectedProfile)
                            .buttonStyle(.borderedProminent)
                    }
                }

                Picker("Profile settings", selection: $selectedSection) {
                    ForEach(EncodingOptionsSection.allCases) { section in
                        Text(section.title).tag(section)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()

                ProfileEncodingSummaryView(
                    options: profile.options,
                    section: selectedSection
                )
            }
            .padding(.leading, 12)
        } else {
            ContentUnavailableView(
                "Select a Profile",
                systemImage: "slider.horizontal.3",
                description: Text("Choose a built-in template or create a custom profile.")
            )
        }
    }

    private var selectedCustomProfileIndex: Int? {
        guard let selectedProfileID else {
            return nil
        }
        return profileStore.customProfiles.firstIndex { $0.id == selectedProfileID }
    }

    private var canMoveSelectedProfileUp: Bool {
        guard let selectedCustomProfileIndex else {
            return false
        }
        return selectedCustomProfileIndex > 0
    }

    private var canMoveSelectedProfileDown: Bool {
        guard let selectedCustomProfileIndex else {
            return false
        }
        return selectedCustomProfileIndex < profileStore.customProfiles.count - 1
    }

    private func duplicateSelectedProfile() {
        guard let profile = selectedProfile else {
            return
        }
        editorRequest = ProfileEditorRequest(
            mode: .create,
            name: profileStore.suggestedDuplicateName(for: profile.name),
            options: profile.options
        )
    }

    private func delete(_ profile: EncodingProfile) {
        do {
            try profileStore.deleteProfile(profile.id)
            if settings.selectedProfileID == profile.id {
                settings.selectedProfileID = ProfileStore.balancedProfileID
            }
            selectedProfileID = ProfileStore.balancedProfileID
        } catch {
            errorMessage = error.localizedDescription
        }
        profileToDelete = nil
    }

    private func moveSelectedProfileUp() {
        guard let index = selectedCustomProfileIndex else {
            return
        }
        do {
            try profileStore.moveCustomProfiles(
                fromOffsets: IndexSet(integer: index),
                toOffset: index - 1
            )
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func moveSelectedProfileDown() {
        guard let index = selectedCustomProfileIndex else {
            return
        }
        do {
            try profileStore.moveCustomProfiles(
                fromOffsets: IndexSet(integer: index),
                toOffset: index + 2
            )
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct ProfileEncodingSummaryView: View {
    let options: EncodingOptions
    let section: EncodingOptionsSection

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                switch section {
                case .video:
                    ProfileSummarySection(
                        title: "Spatial Video Encoding",
                        items: [
                            ProfileSummaryItem(title: "HEVC quality", value: "\(options.hevcQuality)"),
                            ProfileSummaryItem(title: "Left / right bitrate", value: "\(options.leftRightBitrate) Mbps"),
                            ProfileSummaryItem(title: "AI FX upscale to 2× resolution", value: enabledText(options.upscaleEnabled)),
                            ProfileSummaryItem(title: "Upscale quality", value: "\(options.upscaleQuality)"),
                            ProfileSummaryItem(title: "Link HEVC and upscale quality", value: enabledText(options.linkQuality)),
                        ]
                    )

                    ProfileSummarySection(
                        title: "Picture",
                        items: [
                            ProfileSummaryItem(title: "Field of view", value: "\(options.fieldOfView)°"),
                            ProfileSummaryItem(title: "Resolution override", value: fallbackText(options.resolutionOverride)),
                            ProfileSummaryItem(title: "Frame-rate override", value: fallbackText(options.frameRateOverride)),
                        ]
                    )

                    ProfileSummarySection(
                        title: "Stereo Corrections",
                        items: [
                            ProfileSummaryItem(title: "Crop black bars", value: enabledText(options.cropBlackBars)),
                            ProfileSummaryItem(title: "Swap left and right eyes", value: enabledText(options.swapEyes)),
                        ]
                    )
                case .audioAndSubtitles:
                    ProfileSummarySection(title: "Audio", items: audioItems)

                    ProfileSummarySection(
                        title: "Subtitles and Languages",
                        items: [
                            ProfileSummaryItem(title: "Preferred language", value: options.language.name),
                            ProfileSummaryItem(title: "Include subtitles", value: enabledText(options.includeSubtitles)),
                            ProfileSummaryItem(title: "Keep extra languages", value: enabledText(options.keepExtraLanguages)),
                        ]
                    )
                }
            }
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .padding(.trailing, 8)
        }
    }

    private var audioItems: [ProfileSummaryItem] {
        var items = [
            ProfileSummaryItem(title: "Audio handling", value: options.audioHandling.title),
        ]
        if options.audioHandling == .transcodeAAC {
            items.append(ProfileSummaryItem(title: "AAC bitrate", value: "\(options.audioBitrate) kbps"))
        }
        return items
    }

    private func enabledText(_ enabled: Bool) -> String {
        enabled ? "On" : "Off"
    }

    private func fallbackText(_ value: String) -> String {
        value.isEmpty ? "Use source" : value
    }
}

private struct ProfileSummarySection: View {
    let title: String
    let items: [ProfileSummaryItem]

    var body: some View {
        GroupBox {
            VStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                    if index > 0 {
                        Divider()
                    }
                    LabeledContent(item.title) {
                        Text(item.value)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.trailing)
                    }
                    .padding(.vertical, 8)
                }
            }
            .padding(.horizontal, 4)
        } label: {
            Text(title)
                .font(.headline)
        }
    }
}

private struct ProfileSummaryItem: Identifiable {
    let title: String
    let value: String

    var id: String { title }
}

private struct ProfileListRow: View {
    let profile: EncodingProfile
    let isDefault: Bool

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: profile.systemImage)
                .foregroundStyle(Color.accentColor)
                .accessibilityHidden(true)
            Text(profile.name)
                .lineLimit(1)
            Spacer()
            if isDefault {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                    .accessibilityLabel("Default profile")
            }
        }
    }
}

private struct ProfileEditorRequest: Identifiable {
    enum Mode {
        case create
        case update(String)
    }

    let id = UUID()
    let mode: Mode
    let name: String
    let options: EncodingOptions
}

private struct ProfileEditorSheet: View {
    @Environment(\.dismiss) private var dismiss
    let request: ProfileEditorRequest
    let save: (ProfileEditorRequest.Mode, String, EncodingOptions) throws -> Void

    @State private var name: String
    @State private var options: EncodingOptions
    @State private var selectedSection = EncodingOptionsSection.video
    @State private var errorMessage: String?

    init(
        request: ProfileEditorRequest,
        save: @escaping (ProfileEditorRequest.Mode, String, EncodingOptions) throws -> Void
    ) {
        self.request = request
        self.save = save
        _name = State(initialValue: request.name)
        _options = State(initialValue: request.options)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text(editorTitle)
                    .font(.title2.weight(.semibold))
                Text("Profiles save media-result settings only. Job, recovery, and destructive choices remain explicit per conversion.")
                    .foregroundStyle(.secondary)
            }

            TextField("Profile name", text: $name)
                .textFieldStyle(.roundedBorder)

            Picker("Profile settings", selection: $selectedSection) {
                ForEach(EncodingOptionsSection.allCases) { section in
                    Text(section.title).tag(section)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()

            EncodingOptionsEditor(options: $options, section: selectedSection)

            HStack {
                Spacer()
                Button("Cancel") {
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)

                Button("Save") {
                    do {
                        try save(request.mode, name, options)
                        dismiss()
                    } catch {
                        errorMessage = error.localizedDescription
                    }
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(24)
        .frame(width: 760, height: 620)
        .alert(
            "Profile Could Not Be Saved",
            isPresented: Binding(
                get: { errorMessage != nil },
                set: { if !$0 { errorMessage = nil } }
            )
        ) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(errorMessage ?? "The profile could not be saved.")
        }
    }

    private var editorTitle: String {
        switch request.mode {
        case .create:
            "New Profile"
        case .update:
            "Edit Profile"
        }
    }
}

private struct UpdatesSettingsPane: View {
    @ObservedObject var updater: UpdateController

    var body: some View {
        Form {
            SettingsPaneHeader(
                title: "Updates",
                subtitle: "Choose which releases the app should offer. Installation always requires your approval."
            )

            Section("Update Preferences") {
                Toggle("Automatically check for updates", isOn: $updater.automaticallyChecksForUpdates)
                    .disabled(!updater.supportsAutomaticChecks)

                Picker("Update channel", selection: $updater.updateChannel) {
                    ForEach(UpdateChannelPreference.allCases) { channel in
                        Text(channel.name).tag(channel)
                    }
                }
                .disabled(!updater.supportsChannels)

                if !updater.supportsAutomaticChecks {
                    Label(updater.unavailableReason, systemImage: "info.circle")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }

            Section {
                Button(updater.updateActionTitle) {
                    updater.performUpdateAction()
                }
                .disabled(!updater.canPerformUpdateAction)

                if updater.mode == .sparkle {
                    Link("View All Releases…", destination: UpdateController.releasesURL)
                }
            }
        }
        .formStyle(.grouped)
    }
}

private struct AdvancedSettingsPane: View {
    @ObservedObject var settings: AppSettings

    var body: some View {
        Form {
            SettingsPaneHeader(
                title: "Advanced",
                subtitle: "These options are intended for troubleshooting or specialized workflows."
            )

            Section("Defaults for New Jobs") {
                Toggle("Default to the software encoder", isOn: $settings.useSoftwareEncoder)
                Toggle("Keep durable stage files by default", isOn: $settings.keepIntermediateFiles)
            }

            Section("Diagnostics") {
                Toggle("Show technical details", isOn: $settings.showTechnicalDetails)
            }

            Section {
                Button("Restore Advanced Defaults") {
                    settings.resetAdvancedSettings()
                }
            }
        }
        .formStyle(.grouped)
    }
}

private struct SettingsPaneHeader: View {
    let title: String
    let subtitle: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.title2.weight(.semibold))
            Text(subtitle)
                .foregroundStyle(.secondary)
        }
        .padding(.bottom, 8)
    }
}

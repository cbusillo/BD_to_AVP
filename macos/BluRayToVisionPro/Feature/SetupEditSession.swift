import SwiftUI

struct SetupEditSheet: View {
    @Environment(\.dismiss) private var dismiss

    let initialProfile: EncodingProfile
    let initialOptions: ConversionOptions
    let fallbackPipelineDefaults: ProfilePipelineDefaults
    let sourceKind: ConversionSourceKind?
    let profiles: [EncodingProfile]
    @ObservedObject var profileStore: ProfileStore
    @ObservedObject var resolutionMemoryStore: ResolutionMemoryStore
    let applyToConversion: (String, ConversionOptions) -> Void
    let queueConflictForReview: ((String, ConversionOptions, RouteQualityConflict) -> Void)?

    @State private var session: SetupEditSession
    @State private var selectedTab = ConversionSetupTab.video
    @State private var pendingProfileID: String?
    @State private var isShowingDiscardConfirmation = false
    @State private var isShowingSaveAsNew = false
    @State private var newProfileName = ""
    @State private var errorMessage: String?
    @StateObject private var routeQualityState = RouteQualityResolutionState()

    init(
        initialProfile: EncodingProfile,
        initialOptions: ConversionOptions,
        fallbackPipelineDefaults: ProfilePipelineDefaults,
        sourceKind: ConversionSourceKind?,
        profiles: [EncodingProfile],
        profileStore: ProfileStore,
        resolutionMemoryStore: ResolutionMemoryStore,
        applyToConversion: @escaping (String, ConversionOptions) -> Void,
        queueConflictForReview: ((String, ConversionOptions, RouteQualityConflict) -> Void)? = nil
    ) {
        self.initialProfile = initialProfile
        self.initialOptions = initialOptions
        self.fallbackPipelineDefaults = fallbackPipelineDefaults
        self.sourceKind = sourceKind
        self.profiles = profiles
        self.profileStore = profileStore
        self.resolutionMemoryStore = resolutionMemoryStore
        self.applyToConversion = applyToConversion
        self.queueConflictForReview = queueConflictForReview
        _session = State(initialValue: SetupEditSession(profile: initialProfile, options: initialOptions))
    }

    private var selectedProfile: EncodingProfile {
        profiles.first(where: { $0.id == session.profileID }) ?? initialProfile
    }

    private var optionsBinding: Binding<ConversionOptions> {
        Binding(
            get: { session.draftOptions },
            set: { session.updateOptions($0) }
        )
    }

    private var selectedProfileBinding: Binding<String> {
        Binding(
            get: { session.profileID },
            set: { requestProfileChange($0) }
        )
    }

    var body: some View {
        VStack(spacing: 0) {
            ConversionSetupView(
                selectedProfileID: selectedProfileBinding,
                selectedTab: $selectedTab,
                options: optionsBinding,
                profiles: profiles,
                selectedProfile: selectedProfile,
                profileModified: session.isDirty,
                isLocked: false,
                sourceKind: sourceKind,
                routeQualityState: routeQualityState,
                resolutionMemoryStore: resolutionMemoryStore,
                isReady: false,
                openEditor: {},
                saveSelectedProfile: updateProfile,
                saveAsNewProfile: beginSaveAsNew,
                resetProfile: discardChanges,
                queueConflictForReview: queueConflictForReview == nil ? nil : enqueueHeldConflict
            )

            Divider()
            HStack(spacing: 10) {
                Button("Cancel") {
                    requestDismissal()
                }
                    .accessibilityIdentifier("setup-editor-cancel")

                Spacer()

                if session.canUpdateProfile {
                    Button("Update Profile", action: updateProfile)
                        .accessibilityIdentifier("setup-editor-update-profile")
                        .disabled(routeQualityState.blockReason != nil)
                }

                Button("Save as New Profile…", action: beginSaveAsNew)
                    .accessibilityIdentifier("setup-editor-save-as-new")
                    .disabled(routeQualityState.blockReason != nil)

                Button("Apply to This Conversion", action: apply)
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .accessibilityIdentifier("setup-editor-apply")
                    .disabled(routeQualityState.blockReason != nil)
            }
            .padding(16)
        }
        .frame(minWidth: 820, idealWidth: 920, minHeight: 680, idealHeight: 800)
        .onExitCommand(perform: requestDismissal)
        .alert(
            "Discard Unsaved Changes?",
            isPresented: $isShowingDiscardConfirmation
        ) {
            Button(pendingProfileID == nil ? "Discard Changes" : "Discard and Load", role: .destructive) {
                confirmDiscard()
            }
            Button("Cancel", role: .cancel) {
                pendingProfileID = nil
            }
        } message: {
            Text("Your unsaved settings will be discarded.")
        }
        .sheet(isPresented: $isShowingSaveAsNew) {
            ProfileNameSheet(name: $newProfileName) {
                saveAsNewProfile()
            }
        }
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

    private func requestDismissal() {
        if session.isDirty {
            isShowingDiscardConfirmation = true
        } else {
            dismiss()
        }
    }

    private func requestProfileChange(_ identifier: String) {
        guard identifier != session.profileID else {
            return
        }
        guard let profile = profiles.first(where: { $0.id == identifier }) else {
            return
        }
        if session.isDirty {
            pendingProfileID = profile.id
            isShowingDiscardConfirmation = true
            return
        }
        load(profile)
    }

    private func loadPendingProfile() {
        guard let identifier = pendingProfileID,
              let profile = profiles.first(where: { $0.id == identifier })
        else {
            pendingProfileID = nil
            return
        }
        load(profile)
        pendingProfileID = nil
    }

    private func confirmDiscard() {
        if pendingProfileID == nil {
            discardChanges()
            dismiss()
        } else {
            loadPendingProfile()
        }
    }

    private func load(_ profile: EncodingProfile) {
        var loadedOptions = session.draftOptions
        loadedOptions.encoding = profile.options
        loadedOptions.job.applyProfilePipelineDefaults(
            profile.pipelineDefaults ?? fallbackPipelineDefaults
        )
        routeQualityState.reset()
        session.load(profile: profile, options: loadedOptions)
    }

    private func discardChanges() {
        routeQualityState.reset()
        session.discard()
    }

    private func apply() {
        applyToConversion(session.profileID, session.draftOptions)
        dismiss()
    }

    private func enqueueHeldConflict(_ conflict: RouteQualityConflict) {
        guard let queueConflictForReview else { return }
        queueConflictForReview(session.profileID, session.draftOptions, conflict)
        dismiss()
    }

    private func updateProfile() {
        guard session.canUpdateProfile else {
            return
        }
        do {
            let values = session.profileWriteValues()
            try profileStore.updateProfile(
                session.profileID,
                name: values.name,
                options: values.options,
                pipelineDefaults: values.pipelineDefaults
            )
            applyToConversion(session.profileID, session.draftOptions)
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func beginSaveAsNew() {
        newProfileName = profileStore.suggestedDuplicateName(for: selectedProfile.name)
        isShowingSaveAsNew = true
    }

    private func saveAsNewProfile() {
        do {
            let values = session.profileWriteValues()
            let identifier = try profileStore.createProfile(
                name: newProfileName,
                options: values.options,
                pipelineDefaults: values.pipelineDefaults
            )
            isShowingSaveAsNew = false
            applyToConversion(identifier, session.draftOptions)
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct ProfileNameSheet: View {
    @Environment(\.dismiss) private var dismiss
    @Binding var name: String
    let save: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Save as New Profile")
                .font(.title2.weight(.semibold))
            Text(ProfilePersistenceCopy.summary)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            TextField("Profile name", text: $name)
                .textFieldStyle(.roundedBorder)
                .accessibilityIdentifier("setup-editor-new-profile-name")
            HStack {
                Spacer()
                Button("Cancel") {
                    dismiss()
                }
                Button("Save", action: save)
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(24)
        .frame(width: 440)
    }
}

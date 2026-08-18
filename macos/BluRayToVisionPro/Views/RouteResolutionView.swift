import SwiftUI

struct RouteQualityConflictView: View {
    let conflict: RouteQualityConflict
    let profile: EncodingProfile?
    let sourceKind: ConversionSourceKind?
    let memoryStore: ResolutionMemoryStore?
    let queueForReview: (() -> Void)?
    let resolve: (RouteQualityResolutionOption) -> Void
    @State private var selectedResolutionID: String?
    @State private var shouldSuggest = false
    @State private var scope: ResolutionMemoryScope
    @State private var memoryErrorMessage: String?

    init(
        conflict: RouteQualityConflict,
        profile: EncodingProfile? = nil,
        sourceKind: ConversionSourceKind? = nil,
        memoryStore: ResolutionMemoryStore? = nil,
        queueForReview: (() -> Void)? = nil,
        resolve: @escaping (RouteQualityResolutionOption) -> Void
    ) {
        self.conflict = conflict
        self.profile = profile
        self.sourceKind = sourceKind
        self.memoryStore = memoryStore
        self.queueForReview = queueForReview
        self.resolve = resolve
        let suggestion = profile.flatMap { profile in sourceKind.flatMap {
            memoryStore?.suggestion(
                conflictID: conflict.stableID,
                profileID: profile.id,
                sourceKind: $0,
                mappingVersion: conflict.mappingVersion
            )
        } }
        let suggestedResolutionID = suggestion?.isStale == false
            && conflict.resolutions.contains(where: {
                $0.id == suggestion?.entry.resolutionID && $0.isAvailable
            })
            ? suggestion?.entry.resolutionID
            : nil
        _selectedResolutionID = State(initialValue: suggestedResolutionID)
        _scope = State(initialValue: profile.map { .profile($0.id) } ?? .global)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Quality and file choices need a decision", systemImage: "arrow.triangle.branch")
                .font(.subheadline.weight(.semibold))
            Text(conflict.reason)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Picker("Resolution", selection: $selectedResolutionID) {
                Text("Choose a resolution").tag(String?.none)
                ForEach(conflict.resolutions.filter(\.isAvailable)) { option in
                    Text(option.title).tag(Optional(option.id))
                }
            }
            .pickerStyle(.radioGroup)
            ForEach(conflict.resolutions.filter(\.isAvailable)) { option in
                if selectedResolutionID == option.id {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(option.title)
                                .fontWeight(.semibold)
                            Spacer()
                            if option.choice == .keepRequestedWorkflow,
                               let mappedStep = option.mappedStep
                            {
                                Text(mappedStep.title)
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(.secondary)
                            }
                        }
                        Text(option.detail)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("route-quality-resolution-\(option.id)")
                }
            }
            if let suggestion, let memoryStore {
                Text(suggestion.staleExplanation ?? "Suggested from \(suggestion.entry.scope.id). You’ll still be asked, with this choice already filled in.")
                    .font(.caption)
                    .foregroundStyle(suggestion.isStale ? .orange : .secondary)
                Button("Forget suggestion") {
                    do {
                        try memoryStore.forget(conflictID: conflict.stableID, scope: suggestion.entry.scope)
                        selectedResolutionID = nil
                        memoryErrorMessage = nil
                    } catch {
                        memoryErrorMessage = error.localizedDescription
                    }
                }
                .buttonStyle(.borderless)
            }
            if let profile, let memoryStore {
                Toggle("Suggest this next time for \(profile.name)", isOn: $shouldSuggest)
            }
            if shouldSuggest, let profile {
                Picker("Suggestion scope", selection: $scope) {
                    Text(profile.name).tag(ResolutionMemoryScope.profile(profile.id))
                    if let sourceKind { Text(sourceKind.rawValue).tag(ResolutionMemoryScope.sourceKind(sourceKind)) }
                    Text("All sources").tag(ResolutionMemoryScope.global)
                }
            }
            if let memoryErrorMessage {
                Label(memoryErrorMessage, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                if let queueForReview {
                    Button("Add to Queue for Review", action: queueForReview)
                        .buttonStyle(.bordered)
                        .accessibilityIdentifier("route-quality-add-to-queue")
                    Spacer()
                }
                Button("Apply Choice") {
                    guard let option = conflict.resolutions.first(where: { $0.id == selectedResolutionID }) else { return }
                    if shouldSuggest, let memoryStore {
                        do {
                            try memoryStore.store(
                                resolutionID: option.id,
                                for: conflict.stableID,
                                scope: scope,
                                mappingVersion: conflict.mappingVersion
                            )
                            memoryErrorMessage = nil
                        } catch {
                            memoryErrorMessage = error.localizedDescription
                            return
                        }
                    }
                    resolve(option)
                }
                .buttonStyle(.borderedProminent)
                .disabled(selectedResolutionID == nil)
            }
        }
        .padding(12)
        .background(.orange.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
        .overlay {
            RoundedRectangle(cornerRadius: 10)
                .strokeBorder(.orange.opacity(0.35))
        }
    }

    private var suggestion: ResolutionMemorySuggestion? {
        guard let sourceKind, let profile, let memoryStore else { return nil }
        return memoryStore.suggestion(
            conflictID: conflict.stableID,
            profileID: profile.id,
            sourceKind: sourceKind,
            mappingVersion: conflict.mappingVersion
        )
    }
}

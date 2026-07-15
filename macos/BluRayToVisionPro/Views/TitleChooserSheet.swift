import SwiftUI

struct TitleChooserSheet: View {
    @Environment(\.dismiss) private var dismiss

    let titles: [SourceTitle]
    let applySelection: (Set<String>) -> Void
    @State private var selectedIDs: Set<String>

    init(
        titles: [SourceTitle],
        selectedIDs: Set<String>,
        applySelection: @escaping (Set<String>) -> Void
    ) {
        self.titles = titles
        self.applySelection = applySelection
        _selectedIDs = State(initialValue: selectedIDs)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Choose 3D Videos")
                    .font(.title2.weight(.semibold))
                Text("Select the videos to convert. They will be processed one at a time using the current settings.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            HStack(spacing: 12) {
                Button("Main Movie Only") {
                    selectedIDs = Set(titles.filter(\.mainFeature).map(\.id))
                }
                Button("Select All") {
                    selectedIDs = Set(titles.map(\.id))
                }
                Spacer()
                Text("\(selectedIDs.count) selected")
                    .font(.callout.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            List(titles) { title in
                Toggle(isOn: selectionBinding(for: title.id)) {
                    HStack(spacing: 12) {
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 7) {
                                Text(title.name)
                                    .fontWeight(.medium)
                                if title.mainFeature {
                                    Text("Main Movie")
                                        .font(.caption2.weight(.semibold))
                                        .foregroundStyle(Color.accentColor)
                                        .padding(.horizontal, 6)
                                        .padding(.vertical, 2)
                                        .background(Color.accentColor.opacity(0.12), in: Capsule())
                                }
                            }
                            Text(title.outputName)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                        Spacer()
                        VStack(alignment: .trailing, spacing: 3) {
                            Text(title.formattedDuration)
                                .font(.callout.monospacedDigit())
                            Text("\(title.resolution) · \(title.frameRate)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.vertical, 4)
                }
                .toggleStyle(.checkbox)
                .accessibilityLabel("\(title.name), \(title.formattedDuration), \(title.resolution)")
            }
            .frame(minHeight: 230)

            HStack {
                Spacer()
                Button("Cancel") {
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)

                Button("Use Selection") {
                    applySelection(selectedIDs)
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(selectedIDs.isEmpty)
            }
        }
        .padding(24)
        .frame(width: 600, height: 440)
    }

    private func selectionBinding(for identifier: String) -> Binding<Bool> {
        Binding(
            get: { selectedIDs.contains(identifier) },
            set: { selected in
                if selected {
                    selectedIDs.insert(identifier)
                } else {
                    selectedIDs.remove(identifier)
                }
            }
        )
    }
}

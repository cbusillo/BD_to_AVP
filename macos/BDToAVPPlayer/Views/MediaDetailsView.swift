import SwiftUI
import UniformTypeIdentifiers

struct MediaDetailsView: View {
    @ObservedObject var model: PlayerAppModel
    let itemID: String

    @Environment(\.dismiss) private var dismiss
    @State private var isLocatorPresented = false

    var body: some View {
        NavigationStack {
            Group {
                if let item = model.item(id: itemID) {
                    details(for: item)
                } else {
                    ContentUnavailableView("Movie unavailable", systemImage: "film")
                }
            }
            .navigationTitle("Details")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") {
                        model.closeDetails()
                        dismiss()
                    }
                }
            }
        }
        .fileImporter(
            isPresented: $isLocatorPresented,
            allowedContentTypes: [.movie],
            allowsMultipleSelection: false
        ) { result in
            guard case let .success(urls) = result, let url = urls.first else { return }
            Task { await model.locate(itemID: itemID, at: url) }
        }
    }

    @ViewBuilder
    private func details(for item: MediaItem) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                HStack(alignment: .top, spacing: 22) {
                    PosterPlaceholderView(fileName: item.fileName, format: item.format)
                        .frame(width: 170, height: 235)

                    VStack(alignment: .leading, spacing: 13) {
                        Text(item.title)
                            .font(.largeTitle.weight(.semibold))
                        FormatPill(format: item.format)
                        Text(item.fileName)
                            .font(.headline)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                        sourceStatusView(for: item)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }

                playbackSection(for: item)
                metadataSection(for: item)
            }
            .padding(34)
        }
    }

    @ViewBuilder
    private func playbackSection(for item: MediaItem) -> some View {
        let availability = model.playbackAvailability(for: item)
        VStack(alignment: .leading, spacing: 12) {
            Text("Playback")
                .font(.title2.weight(.semibold))

            switch availability {
            case .playable:
                Button {
                    model.requestPlayback(for: item.id)
                } label: {
                    Label("Play", systemImage: "play.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(Color(red: 0.82, green: 0.48, blue: 0.12))
                .accessibilityHint("Sends this movie to the playback integrator.")
            case let .planned(message), let .unavailable(message):
                Button {
                } label: {
                    Label(
                        item.format == .unsupported ? "Play unavailable" : "Play — planned",
                        systemImage: "play.slash"
                    )
                    .frame(maxWidth: .infinity)
                }
                .disabled(true)
                Text(message)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(22)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 24, style: .continuous)
                .stroke(.white.opacity(0.28), lineWidth: 1)
        }
    }

    private func sourceStatusView(for item: MediaItem) -> some View {
        let status = model.sourceStatuses[item.id] ?? .missing
        return VStack(alignment: .leading, spacing: 8) {
            Label(status.title, systemImage: status == .available ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(status == .available ? .green : .orange)

            if status != .available {
                HStack(spacing: 10) {
                    Button("Locate") {
                        isLocatorPresented = true
                    }
                    .buttonStyle(.bordered)
                    Button("Remove", role: .destructive) {
                        model.remove(itemID: item.id)
                        dismiss()
                    }
                    .buttonStyle(.bordered)
                }
            }
        }
    }

    private func metadataSection(for item: MediaItem) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("File & Technical")
                .font(.title2.weight(.semibold))
            LabeledContent("Filename", value: item.fileName)
            LabeledContent("Format", value: item.format.displayName)
            LabeledContent("Source", value: "Files")
        }
        .padding(22)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
    }
}

struct PosterPlaceholderView: View {
    let fileName: String
    let format: StereoFormat

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            LinearGradient(
                colors: [Color(red: 0.22, green: 0.24, blue: 0.26), Color(red: 0.08, green: 0.09, blue: 0.10)],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            VStack(alignment: .leading, spacing: 8) {
                Image(systemName: "film.stack")
                    .font(.system(size: 30, weight: .medium))
                    .foregroundStyle(.white.opacity(0.8))
                Text(fileName)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.white.opacity(0.88))
                    .lineLimit(3)
            }
            .padding(16)
            FormatPill(format: format)
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .trailing)
                .frame(maxHeight: .infinity, alignment: .topTrailing)
        }
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .stroke(.white.opacity(0.22), lineWidth: 1)
        }
        .accessibilityLabel("Filename placeholder for \(fileName), \(format.displayName)")
    }
}

struct FormatPill: View {
    let format: StereoFormat

    var body: some View {
        Text(format.displayName)
            .font(.caption2.weight(.bold))
            .tracking(0.5)
            .foregroundStyle(.primary)
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(.thinMaterial, in: Capsule())
            .accessibilityLabel("Format \(format.displayName)")
    }
}

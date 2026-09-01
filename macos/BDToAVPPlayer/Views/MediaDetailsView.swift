import SwiftUI
import UniformTypeIdentifiers

struct MediaDetailsView: View {
    @ObservedObject var model: PlayerAppModel
    let itemID: String

    @State private var isLocatorPresented = false
    @State private var isRetryingSource = false

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
                    }
                    .accessibilityIdentifier("details-done")
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
            VStack(alignment: .leading, spacing: 22) {
                HStack(alignment: .top, spacing: 24) {
                    MediaThumbnailView(
                        item: item,
                        bookmarkStore: model.bookmarkStore,
                        sourceStatus: model.sourceStatuses[item.id]
                    )
                    .aspectRatio(16 / 9, contentMode: .fit)
                    .frame(width: 250)

                    VStack(alignment: .leading, spacing: 12) {
                        Text(item.title)
                            .font(.title.weight(.semibold))
                            .lineLimit(1)
                            .minimumScaleFactor(0.65)

                        HStack(spacing: 10) {
                            FormatPill(format: item.format)
                            sourceStatusLabel(for: item)
                        }

                        Text(item.fileName)
                            .font(.headline)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)

                        playbackAction(for: item)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }

                metadataSection(for: item)
            }
            .padding(30)
        }
        .scrollIndicators(.hidden)
    }

    @ViewBuilder
    private func playbackAction(for item: MediaItem) -> some View {
        switch model.playbackAvailability(for: item) {
        case .playable:
            Button {
                model.requestPlayback(for: item.id)
            } label: {
                Label("Play", systemImage: "play.fill")
                    .frame(maxWidth: .infinity, minHeight: 60)
            }
            .buttonStyle(.borderedProminent)
            .accessibilityIdentifier("play-movie-\(item.id)")
            .accessibilityHint("Starts playback for this movie.")
        case let .planned(message):
            unavailablePlayback(message: message, label: "Playback coming later", item: item)
        case let .unavailable(message):
            unavailablePlayback(message: message, label: "Play unavailable", item: item)
        }
    }

    private func unavailablePlayback(message: String, label: String, item: MediaItem) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(label, systemImage: "play.slash")
                .font(.headline)
            Text(message)
                .font(.subheadline)
                .foregroundStyle(.secondary)

            let status = model.sourceStatuses[item.id] ?? .missing
            if status != .available && status != .checking {
                HStack(spacing: 12) {
                    if status == .unavailable || status == .stale {
                        Button {
                            Task {
                                isRetryingSource = true
                                await model.refreshSourceStatus(for: item.id)
                                isRetryingSource = false
                            }
                        } label: {
                            if isRetryingSource {
                                ProgressView()
                                    .frame(minWidth: 60, minHeight: 60)
                                    .accessibilityLabel("Retrying source")
                            } else {
                                Text("Try Again")
                                    .frame(minWidth: 60, minHeight: 60)
                            }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(isRetryingSource)
                        .accessibilityIdentifier("details-retry-source")
                    }

                    Button {
                        isLocatorPresented = true
                    } label: {
                        Text("Locate")
                            .frame(minWidth: 60, minHeight: 60)
                    }
                    .buttonStyle(.bordered)
                    .accessibilityIdentifier("details-locate-source")

                    Button(role: .destructive) {
                        model.remove(itemID: item.id)
                    } label: {
                        Text("Remove")
                            .frame(minWidth: 60, minHeight: 60)
                    }
                    .buttonStyle(.bordered)
                }
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
    }

    private func sourceStatusLabel(for item: MediaItem) -> some View {
        let status = model.sourceStatuses[item.id] ?? .missing
        let systemImage: String
        let color: Color
        switch status {
        case .available:
            systemImage = "checkmark.circle.fill"
            color = .green
        case .checking:
            systemImage = "clock"
            color = .blue
        case .unavailable, .missing, .stale:
            systemImage = "exclamationmark.triangle.fill"
            color = .orange
        }
        return Label(
            status.title,
            systemImage: systemImage
        )
        .font(.subheadline.weight(.semibold))
        .foregroundStyle(color)
    }

    private func metadataSection(for item: MediaItem) -> some View {
        let status = model.sourceStatuses[item.id] ?? .missing
        return VStack(alignment: .leading, spacing: 14) {
            Text("File & Technical")
                .font(.title2.weight(.semibold))
            LabeledContent("Filename", value: item.fileName)
            LabeledContent("Format", value: item.format.displayName)
            LabeledContent("Location", value: model.sourceTitle(for: item))
            LabeledContent("Source status", value: status.title)
        }
        .padding(20)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
    }
}

struct FormatPill: View {
    let format: StereoFormat

    var body: some View {
        Text(format.displayName)
            .font(.caption2.weight(.bold))
            .tracking(0.5)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(.thinMaterial, in: Capsule())
            .accessibilityLabel("Format \(format.displayName)")
    }
}

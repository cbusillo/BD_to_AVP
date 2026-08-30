import SwiftUI
import UniformTypeIdentifiers

struct MediaDetailsView: View {
    @ObservedObject var model: PlayerAppModel
    let itemID: String

    @State private var isLocatorPresented = false

    var body: some View {
        Group {
            if let item = model.item(id: itemID) {
                details(for: item)
            } else {
                ContentUnavailableView("Movie unavailable", systemImage: "film")
            }
        }
        .navigationBarTitleDisplayMode(.inline)
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
            VStack(alignment: .leading, spacing: 28) {
                MediaThumbnailView(
                    item: item,
                    bookmarkStore: model.bookmarkStore,
                    sourceStatus: model.sourceStatuses[item.id]
                )
                .aspectRatio(16 / 9, contentMode: .fit)
                .frame(maxWidth: 900)

                VStack(alignment: .leading, spacing: 10) {
                    Text(item.title)
                        .font(.largeTitle.weight(.semibold))
                        .lineLimit(2)
                    Text("\(item.format.displayName) · \(item.fileName)")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                    sourceStatusView(for: item)
                }

                playbackSection(for: item)
            }
            .frame(maxWidth: 900, alignment: .leading)
            .padding(.horizontal, LibraryTheme.contentPadding)
            .padding(.vertical, 28)
            .frame(maxWidth: .infinity, alignment: .center)
        }
        .scrollIndicators(.hidden)
    }

    @ViewBuilder
    private func playbackSection(for item: MediaItem) -> some View {
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
        case let .planned(message), let .unavailable(message):
            Text(message)
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
    }

    private func sourceStatusView(for item: MediaItem) -> some View {
        let status = model.sourceStatuses[item.id] ?? .missing
        return VStack(alignment: .leading, spacing: 10) {
            Label(status.title, systemImage: status == .available ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)

            if status != .available {
                HStack(spacing: 12) {
                    Button("Locate") {
                        isLocatorPresented = true
                    }
                    .buttonStyle(.bordered)
                    .frame(minHeight: 60)

                    Button("Remove", role: .destructive) {
                        model.remove(itemID: item.id)
                    }
                    .buttonStyle(.bordered)
                    .frame(minHeight: 60)
                }
            }
        }
    }
}

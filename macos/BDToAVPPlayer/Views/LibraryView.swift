import SwiftUI
import UniformTypeIdentifiers

struct LibraryView: View {
    @ObservedObject var model: PlayerAppModel
    @State private var isImporterPresented = false

    var body: some View {
        ScrollView {
            if model.visibleItems.isEmpty {
                emptyState
                    .frame(maxWidth: .infinity, minHeight: 420)
            } else {
                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: LibraryTheme.minimumTileWidth), spacing: LibraryTheme.gridSpacing)],
                    spacing: LibraryTheme.gridSpacing
                ) {
                    ForEach(model.visibleItems) { item in
                        NavigationLink(value: item.id) {
                            MediaMovieTile(
                                item: item,
                                sourceStatus: model.sourceStatuses[item.id],
                                bookmarkStore: model.bookmarkStore
                            )
                        }
                        .buttonStyle(.plain)
                        .hoverEffect(.highlight)
                        .contextMenu {
                            if model.playbackAvailability(for: item) == .playable {
                                Button {
                                    model.requestPlayback(for: item.id)
                                } label: {
                                    Label("Play", systemImage: "play.fill")
                                }
                            }
                        }
                    }
                }
            }
        }
        .scrollIndicators(.hidden)
        .contentMargins(.horizontal, LibraryTheme.contentPadding, for: .scrollContent)
        .contentMargins(.vertical, LibraryTheme.contentPadding, for: .scrollContent)
        .navigationTitle("Movies")
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                sortMenu
                addMovieButton
            }
        }
        .fileImporter(
            isPresented: $isImporterPresented,
            allowedContentTypes: [.movie],
            allowsMultipleSelection: false
        ) { result in
            guard case let .success(urls) = result, let url = urls.first else { return }
            Task { await model.importMovie(from: url) }
        }
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label("No movies yet", systemImage: "film.stack")
        } description: {
            Text("Add a movie to see it here.")
        } actions: {
            addMovieButton
        }
    }

    private var sortMenu: some View {
        Menu {
            ForEach(MediaSortOrder.allCases, id: \.self) { order in
                Button {
                    model.sortOrder = order
                } label: {
                    if model.sortOrder == order {
                        Label(order.title, systemImage: "checkmark")
                    } else {
                        Text(order.title)
                    }
                }
            }
        } label: {
            Label("Sort", systemImage: "arrow.up.arrow.down")
        }
        .accessibilityLabel("Sort movies")
    }

    private var addMovieButton: some View {
        Button {
            isImporterPresented = true
        } label: {
            Label("Add Movie", systemImage: "plus")
        }
        .buttonStyle(.borderedProminent)
        .accessibilityHint("Choose one movie file from Files.")
    }
}

struct MediaMovieTile: View {
    let item: MediaItem
    let sourceStatus: MediaSourceStatus?
    let bookmarkStore: BookmarkStore

    var body: some View {
        VStack(alignment: .leading, spacing: LibraryTheme.tileTextSpacing) {
            MediaThumbnailView(
                item: item,
                bookmarkStore: bookmarkStore,
                sourceStatus: sourceStatus
            )
            .aspectRatio(16 / 9, contentMode: .fit)

            Text(item.title)
                .font(.headline)
                .lineLimit(2)
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .topLeading)

            if sourceStatus != .available {
                Label(sourceStatus?.title ?? "Source unavailable", systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .padding(LibraryTheme.tilePadding)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: LibraryTheme.tileCornerRadius, style: .continuous))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(tileAccessibilityLabel)
        .accessibilityHint("Opens movie details.")
    }

    private var tileAccessibilityLabel: String {
        if sourceStatus == .available {
            return "\(item.title), \(item.format.displayName)"
        }
        return "\(item.title), \(item.format.displayName), \(sourceStatus?.title ?? "Source unavailable")"
    }
}

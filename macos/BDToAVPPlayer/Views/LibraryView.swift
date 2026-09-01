import SwiftUI
import UniformTypeIdentifiers

struct LibraryView: View {
    @ObservedObject var model: PlayerAppModel
    @State private var isImporterPresented = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 28) {
                header

                stereoChecksPanel

                if model.importedItems.isEmpty {
                    emptyState
                } else if model.visibleImportedItems.isEmpty {
                    noMatchesState
                } else if model.viewMode == .posters {
                    posterGrid
                } else {
                    fileList
                }
            }
            .padding(.horizontal, 34)
            .padding(.vertical, 28)
        }
        .scrollIndicators(.hidden)
        .navigationTitle("All Movies")
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                viewControls
                filterMenu
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

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Your movies")
                .font(.largeTitle.weight(.semibold))
            Text("Choose a movie to review its source, format, and playback options.")
                .foregroundStyle(.secondary)
        }
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label("No movies yet", systemImage: "film.stack")
        } description: {
            Text("Add a movie file to start building your library.")
        } actions: {
            addMovieButton
        }
        .frame(maxWidth: .infinity, minHeight: 360)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 30, style: .continuous))
    }

    private var noMatchesState: some View {
        ContentUnavailableView {
            Label("No matching movies", systemImage: "line.3.horizontal.decrease.circle")
        } description: {
            Text("No movies match the \(model.formatFilter.title) filter.")
        } actions: {
            Button("Show All Movies") {
                model.formatFilter = .all
            }
            .buttonStyle(.borderedProminent)
        }
        .frame(maxWidth: .infinity, minHeight: 360)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 30, style: .continuous))
    }

    private var posterGrid: some View {
        LazyVGrid(
            columns: [GridItem(.adaptive(minimum: LibraryTheme.minimumTileWidth), spacing: LibraryTheme.gridSpacing)],
            spacing: LibraryTheme.gridSpacing
        ) {
            ForEach(model.visibleImportedItems) { item in
                MediaPosterCard(
                    item: item,
                    sourceStatus: model.sourceStatuses[item.id],
                    sourceTitle: model.sourceTitle(for: item),
                    bookmarkStore: model.bookmarkStore,
                    showDetails: { model.showDetails(for: item.id) },
                    play: model.playbackAvailability(for: item) == .playable
                        ? { model.requestPlayback(for: item.id) }
                        : nil
                )
            }
        }
    }

    private var fileList: some View {
        LazyVStack(spacing: 10) {
            ForEach(model.visibleImportedItems) { item in
                HStack(spacing: 12) {
                    Button {
                        model.showDetails(for: item.id)
                    } label: {
                        MediaFileRow(
                            item: item,
                            sourceStatus: model.sourceStatuses[item.id],
                            sourceTitle: model.sourceTitle(for: item),
                            bookmarkStore: model.bookmarkStore
                        )
                    }
                    .buttonStyle(.plain)
                    .contentShape(.hoverEffect, RoundedRectangle(cornerRadius: 20, style: .continuous))
                    .hoverEffect(.highlight)
                    .accessibilityIdentifier("movie-tile-\(item.id)")

                    if model.playbackAvailability(for: item) == .playable {
                        libraryPlayButton(for: item)
                    }
                }
            }
        }
    }

    private func libraryPlayButton(for item: MediaItem) -> some View {
        Button {
            model.requestPlayback(for: item.id)
        } label: {
            Label("Play", systemImage: "play.fill")
                .frame(minWidth: 72, minHeight: 60)
        }
        .buttonStyle(.borderedProminent)
        .accessibilityIdentifier("play-library-\(item.id)")
        .accessibilityHint("Starts playback without opening movie details.")
    }

    private var stereoChecksPanel: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: "eye.circle.fill")
                    .font(.largeTitle)
                    .foregroundStyle(.blue)

                VStack(alignment: .leading, spacing: 6) {
                    Text("Built-in stereo checks")
                        .font(.title2.weight(.semibold))
                        .accessibilityIdentifier("built-in-stereo-checks-title")
                    Text("No files are needed. Start either check, then cover one eye at a time. Normal shows LEFT EYE ONLY to your left eye and RIGHT EYE ONLY to your right eye; Reversed swaps them.")
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            if let message = model.stereoCheckErrorMessage {
                Label(message, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            } else {
                HStack(spacing: 14) {
                    ForEach(model.builtInStereoCheckItems) { item in
                        Button {
                            model.requestPlayback(for: item.id)
                        } label: {
                            VStack(alignment: .leading, spacing: 6) {
                                Label(
                                    item.format == .sideBySide ? "Start SBS Check" : "Start Over-Under Check",
                                    systemImage: "play.fill"
                                )
                                .font(.headline)
                                Text(item.format.displayName)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            .frame(maxWidth: .infinity, minHeight: 60, alignment: .leading)
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(model.playbackAvailability(for: item) != .playable)
                        .accessibilityIdentifier("play-\(item.id)")
                    }
                }
            }
        }
        .padding(22)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 26, style: .continuous))
    }

    private var viewControls: some View {
        Picker("Library view", selection: $model.viewMode) {
            ForEach(LibraryViewMode.allCases, id: \.self) { mode in
                Text(mode.title).tag(mode)
            }
        }
        .pickerStyle(.segmented)
        .frame(width: 180)
        .accessibilityLabel("Library view")
    }

    private var filterMenu: some View {
        Menu {
            ForEach(MediaFormatFilter.allCases, id: \.self) { filter in
                Button {
                    model.formatFilter = filter
                } label: {
                    if model.formatFilter == filter {
                        Label(filter.title, systemImage: "checkmark")
                    } else {
                        Text(filter.title)
                    }
                }
            }
        } label: {
            Label("Filter", systemImage: "line.3.horizontal.decrease.circle")
        }
        .accessibilityLabel("Filter movies")
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
        .disabled(model.isImporting)
        .accessibilityHint("Choose one movie file from Files.")
    }
}

struct MediaPosterCard: View {
    let item: MediaItem
    let sourceStatus: MediaSourceStatus?
    let sourceTitle: String
    let bookmarkStore: BookmarkStore
    let showDetails: () -> Void
    let play: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button(action: showDetails) {
                cardContent
            }
            .buttonStyle(.plain)
            .contentShape(.hoverEffect, RoundedRectangle(cornerRadius: 18, style: .continuous))
            .hoverEffect(.highlight)
            .accessibilityIdentifier("movie-tile-\(item.id)")
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(accessibilityLabel)
            .accessibilityHint("Opens movie details.")

            HStack(spacing: 10) {
                Label(sourceTitle, systemImage: sourceTitle == "On My Vision Pro" ? "visionpro" : "folder")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Spacer(minLength: 8)

                if let play {
                    Button(action: play) {
                        Label("Play", systemImage: "play.fill")
                            .frame(minHeight: 60)
                    }
                    .buttonStyle(.borderedProminent)
                    .accessibilityIdentifier("play-library-\(item.id)")
                    .accessibilityLabel("Play \(item.title)")
                    .accessibilityHint("Starts playback without opening movie details.")
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .padding(14)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
        .accessibilityElement(children: .contain)
    }

    private var cardContent: some View {
        VStack(alignment: .leading, spacing: 10) {
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

            Text(item.fileName)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)

            if sourceStatus != .available {
                Label(
                    sourceStatus?.title ?? "Source unavailable",
                    systemImage: sourceStatus == .checking ? "clock" : "exclamationmark.triangle"
                )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }

    private var accessibilityLabel: String {
        let status = sourceStatus ?? .missing
        return "\(item.title), \(item.fileName), \(item.format.displayName), \(sourceTitle), \(status.title)"
    }
}

struct MediaFileRow: View {
    let item: MediaItem
    let sourceStatus: MediaSourceStatus?
    let sourceTitle: String
    let bookmarkStore: BookmarkStore

    var body: some View {
        HStack(spacing: 16) {
            MediaThumbnailView(
                item: item,
                bookmarkStore: bookmarkStore,
                sourceStatus: sourceStatus
            )
            .aspectRatio(16 / 9, contentMode: .fit)
            .frame(width: 120)

            VStack(alignment: .leading, spacing: 7) {
                Text(item.title)
                    .font(.headline)
                Text(item.fileName)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                HStack(spacing: 8) {
                    FormatPill(format: item.format)
                    if sourceStatus != .available {
                        Text(sourceStatus?.title ?? "Source unavailable")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            Spacer()
            Image(systemName: "chevron.right")
                .foregroundStyle(.tertiary)
        }
        .padding(12)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
        .accessibilityHint("Opens movie details.")
    }

    private var accessibilityLabel: String {
        let status = sourceStatus ?? .missing
        return "\(item.title), \(item.fileName), \(item.format.displayName), \(sourceTitle), \(status.title)"
    }
}

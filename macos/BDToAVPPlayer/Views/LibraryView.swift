import SwiftUI
import UniformTypeIdentifiers

struct LibraryView: View {
    @ObservedObject var model: PlayerAppModel
    @State private var isImporterPresented = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 28) {
                header

                if model.visibleItems.isEmpty {
                    emptyState
                } else if model.viewMode == .posters {
                    posterGrid
                } else {
                    fileList
                }
            }
            .padding(.horizontal, 34)
            .padding(.vertical, 28)
        }
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
            Text("Choose a movie to inspect its source and playback format.")
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
        .overlay {
            RoundedRectangle(cornerRadius: 30, style: .continuous)
                .stroke(.white.opacity(0.32), lineWidth: 1)
        }
    }

    private var posterGrid: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 185), spacing: 22)], spacing: 24) {
            ForEach(model.visibleItems) { item in
                MediaPosterCard(
                    item: item,
                    sourceStatus: model.sourceStatuses[item.id],
                    showDetails: { model.showDetails(for: item.id) }
                )
            }
        }
    }

    private var fileList: some View {
        LazyVStack(spacing: 10) {
            ForEach(model.visibleItems) { item in
                Button {
                    model.showDetails(for: item.id)
                } label: {
                    MediaFileRow(item: item, sourceStatus: model.sourceStatuses[item.id])
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var viewControls: some View {
        Picker("Library view", selection: $model.viewMode) {
            ForEach(LibraryViewMode.allCases, id: \.self) { mode in
                Text(mode.title).tag(mode)
            }
        }
        .pickerStyle(.segmented)
        .frame(width: 170)
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
        .tint(Color(red: 0.82, green: 0.48, blue: 0.12))
        .accessibilityHint("Choose one movie file from Files.")
    }
}

struct MediaPosterCard: View {
    let item: MediaItem
    let sourceStatus: MediaSourceStatus?
    let showDetails: () -> Void

    var body: some View {
        Button(action: showDetails) {
            VStack(alignment: .leading, spacing: 10) {
                PosterPlaceholderView(fileName: item.fileName, format: item.format)
                    .aspectRatio(0.72, contentMode: .fit)

                Text(item.title)
                    .font(.headline)
                    .lineLimit(2)
                    .frame(maxWidth: .infinity, minHeight: 44, maxHeight: 44, alignment: .topLeading)

                Text(item.fileName)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)

                HStack(spacing: 8) {
                    Label("On My Vision Pro", systemImage: "visionpro")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 4)
                    Text("Details ›")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(.thinMaterial, in: Capsule())
                }

                if sourceStatus != .available {
                    Label(sourceStatus?.title ?? "Source unavailable", systemImage: "exclamationmark.triangle")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                }
            }
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .padding(14)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 24, style: .continuous)
                    .stroke(.white.opacity(0.28), lineWidth: 1)
            }
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("movie-card-\(item.id)")
        .accessibilityLabel("Details for \(item.title)")
        .accessibilityHint("Shows filename, format, source, and playback actions.")
    }
}

struct MediaFileRow: View {
    let item: MediaItem
    let sourceStatus: MediaSourceStatus?

    var body: some View {
        HStack(spacing: 16) {
            PosterPlaceholderView(fileName: item.fileName, format: item.format)
                .frame(width: 68, height: 82)

            VStack(alignment: .leading, spacing: 5) {
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
                            .foregroundStyle(.orange)
                    }
                }
            }
            Spacer()
            Image(systemName: "chevron.right")
                .foregroundStyle(.tertiary)
        }
        .padding(12)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(.white.opacity(0.25), lineWidth: 1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(item.title), \(item.fileName), \(item.format.displayName)")
    }
}

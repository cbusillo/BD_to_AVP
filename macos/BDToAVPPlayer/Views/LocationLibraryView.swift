import SwiftUI

/// One Location's library: the playable titles in a network share or Files folder.
struct LocationLibraryView: View {
    @ObservedObject var model: PlayerAppModel
    @ObservedObject var locations: LocationLibraryModel
    let locationID: UUID
    let chooseFolderAgain: () -> Void
    let removed: () -> Void

    /// How often an open Location looks for added, changed or removed files.
    static let refreshInterval: Duration = .seconds(60)

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                header
                content
            }
            .padding(.horizontal, 34)
            .padding(.vertical, 28)
        }
        .scrollIndicators(.hidden)
        .navigationTitle(name)
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                Button("Refresh", systemImage: "arrow.clockwise") {
                    Task { await locations.refresh(locationID) }
                }
                .disabled(status == .checking)
                .accessibilityIdentifier("location-refresh")
                Menu {
                    Button("Choose Folder Again", systemImage: "folder.badge.gearshape", action: chooseFolderAgain)
                    Button("Remove Location", systemImage: "trash", role: .destructive, action: remove)
                } label: {
                    Label("More", systemImage: "ellipsis")
                }
                .accessibilityIdentifier("location-more")
            }
        }
        .task(id: locationID) {
            while !Task.isCancelled {
                await locations.refresh(locationID)
                try? await Task.sleep(for: Self.refreshInterval)
            }
        }
    }

    private var name: String { locations.location(id: locationID)?.name ?? "Location" }
    private var status: LocationStatus { locations.statuses[locationID] ?? .notChecked }
    private var titles: [MediaItem] {
        (locations.titles[locationID] ?? []).sorted {
            $0.title.localizedStandardCompare($1.title) == .orderedAscending
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 14) {
            VStack(alignment: .leading, spacing: 8) {
                Text(name)
                    .font(.largeTitle.weight(.semibold))
                Text(statusLine)
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier("location-status")
            }
            Spacer()
            if status == .checking {
                ProgressView()
            }
        }
    }

    private var statusLine: String {
        switch status {
        case .notChecked:
            return "Connecting…"
        case .checking:
            if let progress = locations.progress[locationID], progress.total > 0 {
                return "Looking for movies: \(progress.inspected) of \(progress.total) files checked"
            }
            return "Looking for movies…"
        case .available:
            return titles.count == 1 ? "1 movie" : "\(titles.count) movies"
        case .unreachable:
            return "Can't reach this location"
        }
    }

    @ViewBuilder
    private var content: some View {
        if status == .unreachable {
            unreachableState
        } else if titles.isEmpty {
            if status == .available {
                emptyState
            }
        } else {
            LazyVGrid(
                columns: [GridItem(.adaptive(minimum: LibraryTheme.minimumTileWidth), spacing: LibraryTheme.gridSpacing)],
                spacing: LibraryTheme.gridSpacing
            ) {
                ForEach(titles) { item in
                    MediaPosterCard(
                        item: item,
                        sourceStatus: model.sourceStatus(for: item.id),
                        sourceTitle: name,
                        bookmarkStore: model.bookmarkStore,
                        showDetails: { model.showDetails(for: item.id) },
                        play: model.playbackAvailability(for: item) == .playable
                            ? { model.requestPlayback(for: item.id) }
                            : nil
                    )
                }
            }
        }
        if status == .available {
            if locations.truncatedLocations.contains(locationID) {
                Label(
                    "This location has more folders than Shiny 3D Cinema reads. Add a folder closer to your movies to see the rest.",
                    systemImage: "info.circle"
                )
                .font(.callout)
                .foregroundStyle(.secondary)
            }
            unplayableSection
        }
    }

    private var unreachableState: some View {
        ContentUnavailableView {
            Label("Can't reach \(name)", systemImage: "externaldrive.badge.exclamationmark")
        } description: {
            Text("Make sure the server is on, then open it in the Files app so Vision Pro connects to it. Then try again. If it still doesn't open, choose the folder again.")
        } actions: {
            Button("Try Again") {
                Task { await locations.refresh(locationID) }
            }
            .buttonStyle(.borderedProminent)
            .accessibilityIdentifier("location-try-again")
            Button("Choose Folder Again", action: chooseFolderAgain)
                .buttonStyle(.bordered)
            Button("Remove Location", role: .destructive, action: remove)
                .buttonStyle(.bordered)
        }
        .frame(maxWidth: .infinity, minHeight: 360)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 30, style: .continuous))
        .accessibilityIdentifier("location-unreachable")
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label("No movies here yet", systemImage: "film.stack")
        } description: {
            Text("Shiny 3D Cinema lists Blu-ray 3D ISOs and disc folders, MKVs that keep the 3D track, and MV-HEVC movies. Add some to this folder and they appear here.")
        }
        .frame(maxWidth: .infinity, minHeight: 300)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 30, style: .continuous))
    }

    @ViewBuilder
    private var unplayableSection: some View {
        let files = locations.unplayableFiles[locationID] ?? []
        if !files.isEmpty {
            DisclosureGroup(files.count == 1 ? "1 file can't play here" : "\(files.count) files can't play here") {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(files) { file in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(file.relativePath).font(.callout)
                            Text(file.reason).font(.caption).foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(.top, 8)
            }
            .padding(18)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
            .accessibilityIdentifier("location-unplayable")
        }
    }

    private func remove() {
        locations.remove(locationID)
        removed()
    }
}

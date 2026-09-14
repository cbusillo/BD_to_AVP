import SwiftUI

struct MovieLibraryView: View {
    @ObservedObject var model: MovieLibraryModel
    let play: (SharedMoviePlayback) -> Void
    @State private var search = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack {
                VStack(alignment: .leading, spacing: 6) {
                    Text(model.catalog == nil ? "Mac Movies" : model.selectedName).font(.largeTitle.bold())
                    Text(model.status).foregroundStyle(.secondary)
                }
                Spacer()
                if model.isBusy { ProgressView() }
                if model.catalog != nil {
                    Button("Refresh", systemImage: "arrow.clockwise") { model.refresh() }.disabled(model.isBusy)
                    Menu {
                        Button("Choose Another Mac") { model.discover() }
                        Button("Forget This Mac", role: .destructive) { model.forgetConnected() }
                    } label: { Image(systemName: "ellipsis") }
                } else {
                    Button("Find Macs", systemImage: "arrow.clockwise") { model.discover() }
                }
            }
            if let error = model.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
            }
            if let prompt = model.prompt {
                VStack(spacing: 18) {
                    Text("Confirm \(model.selectedName)").font(.title2.bold())
                    Text(prompt.code).font(.system(size: 52, weight: .bold, design: .monospaced))
                    Text("Compare this code with Movie Sharing on the Mac.")
                    HStack {
                        Button("Not My Mac") { model.reject() }
                        Button(model.isWaitingForMac ? "Waiting for Mac…" : "Codes Match") { model.confirm() }
                            .disabled(model.isWaitingForMac)
                    }
                }.frame(maxWidth: .infinity).padding(28).glassBackgroundEffect()
            } else if let catalog = model.catalog {
                TextField("Search movies", text: $search).textFieldStyle(.roundedBorder)
                ForEach(catalog.roots.filter { !$0.isAvailable }) { root in
                    Label("\(root.name) is unavailable. Reconnect the drive or choose the folder again on the Mac.", systemImage: "externaldrive.badge.exclamationmark")
                        .font(.callout).foregroundStyle(.secondary)
                }
                if catalog.truncated {
                    Text("The scan reached its limit of 500 movies, five folder levels, or five seconds. Share a more specific folder to see the rest.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                let movies = catalog.movies.filter { search.isEmpty || $0.title.localizedCaseInsensitiveContains(search) }
                if movies.isEmpty {
                    ContentUnavailableView(search.isEmpty ? "No Movies Yet" : "No Matching Movies", systemImage: "film", description: Text(search.isEmpty ? "Add completed MP4, MOV, or M4V movies to a shared folder on the Mac, then refresh." : "Try a different title."))
                } else {
                    List(movies) { movie in
                        Button {
                            if let selection = model.playback(movie) { play(selection) }
                        } label: {
                            HStack(spacing: 18) {
                                Image(systemName: "play.rectangle.fill").font(.title)
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(movie.title).font(.headline)
                                    Text(catalog.roots.first { $0.id == movie.rootID }?.name ?? "Shared Folder").font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Text(ByteCountFormatter.string(fromByteCount: movie.byteCount, countStyle: .file)).font(.caption).foregroundStyle(.secondary)
                                Image(systemName: "play.fill")
                            }.padding(.vertical, 8)
                        }.buttonStyle(.plain)
                    }.listStyle(.plain)
                }
            } else {
                List {
                    Section("Available Macs") {
                        if model.endpoints.isEmpty {
                            Text("Keep the Mac app open with Movie Sharing enabled. Allow Local Network access when asked.").foregroundStyle(.secondary)
                        }
                        ForEach(model.endpoints) { endpoint in
                            Button { model.connect(endpoint) } label: {
                                Label(endpoint.displayName, systemImage: "desktopcomputer")
                            }.disabled(model.isBusy)
                        }
                    }
                    if !model.savedMacs.isEmpty {
                        Section("Remembered Macs") {
                            ForEach(model.savedMacs) { peer in
                                HStack {
                                    Label(peer.name, systemImage: "desktopcomputer")
                                    Spacer()
                                    Button("Forget") { model.forgetSaved(peer) }
                                }
                            }
                        }
                    }
                }
            }
        }
        .padding(28)
        .task { model.refreshSavedMacs() }
        .onDisappear { model.stopDiscovery() }
    }
}

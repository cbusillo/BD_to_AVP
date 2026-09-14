import SwiftUI

struct MovieSharingSheet: View {
    @ObservedObject var controller: MovieSharingController
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Label("Movie Sharing", systemImage: "visionpro").font(.title2.bold())
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            Text("Choose shared folders once. Browse and pick movies from Vision Pro.")
            Toggle("Share movies on this Mac", isOn: Binding(get: { controller.isSharing }, set: { value in Task { await controller.setSharing(value) } }))
                .disabled(controller.sources.isEmpty || controller.isBusy)
            Text(controller.status).font(.callout).foregroundStyle(.secondary)
            if let candidate = controller.pairingCandidate {
                GroupBox("Confirm Vision Pro") {
                    VStack(spacing: 10) {
                        Text("Compare this code with the one on Vision Pro.")
                        Text(candidate.shortAuthenticationString.formattedDigits).font(.largeTitle.monospacedDigit().bold())
                        HStack {
                            Button("Not My Device", role: .destructive) { Task { await controller.reject() } }
                            Button(candidate.isMacApproved ? "Waiting for Vision Pro…" : "Codes Match") { Task { await controller.approve() } }
                                .disabled(candidate.isMacApproved)
                        }
                    }.frame(maxWidth: .infinity).padding(8)
                }
            }
            GroupBox("Shared Folders") {
                VStack(alignment: .leading, spacing: 12) {
                    if controller.sources.isEmpty { Text("No folders added yet.").foregroundStyle(.secondary) }
                    ForEach(controller.sources) { source in
                        HStack {
                            Label(source.name, systemImage: "folder")
                            Spacer()
                            Button("Remove") { Task { await controller.removeFolder(source.id) } }
                                .disabled(controller.isBusy)
                        }
                    }
                    Button("Add Folder…") { Task { await controller.addFolders() } }
                        .disabled(controller.isBusy || controller.sources.count >= MovieLibraryContract.maximumRoots)
                }.frame(maxWidth: .infinity, alignment: .leading).padding(8)
            }
            if !controller.peers.isEmpty {
                GroupBox("Paired Devices") {
                    VStack {
                        ForEach(controller.peers) { peer in
                            HStack {
                                Label(peer.name, systemImage: "visionpro")
                                Text(String(peer.id.prefix(6))).foregroundStyle(.secondary).font(.caption.monospaced())
                                Spacer()
                                Button("Forget") { Task { await controller.forget(peer) } }
                            }
                        }
                    }.padding(8)
                }
            }
            if let error = controller.errorMessage { Text(error).foregroundStyle(.red).textSelection(.enabled) }
            Text("Keep this app open and the Mac awake on the same trusted network. Shared MP4, MOV, and M4V files appear on Vision Pro; the player supports MV-HEVC and full SBS/OU HEVC stereo movies.")
                .font(.footnote).foregroundStyle(.secondary)
        }
        .padding(24).frame(width: 560)
    }
}

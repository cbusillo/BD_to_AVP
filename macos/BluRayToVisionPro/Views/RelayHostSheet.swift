import SwiftUI
import UniformTypeIdentifiers

struct RelayHostSheet: View {
    @ObservedObject var controller: RelayHostSessionController
    @Environment(\.dismiss) private var dismiss
    @State private var isChoosingFixture = false

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 8) {
                Label("Relay EVENT-HLS Fixture", systemImage: "dot.radiowaves.left.and.right")
                    .font(.title2.weight(.semibold))
                Text("Share an existing local HLS fixture with Vision Pro over your private network. No source-specific disc or decryption details are used.")
                    .foregroundStyle(.secondary)
            }

            GroupBox("Session") {
                VStack(alignment: .leading, spacing: 10) {
                    Text(controller.statusText)
                    LabeledContent("Lifecycle", value: controller.lifecycleText)
                    if let fixtureDirectory = controller.fixtureDirectory {
                        LabeledContent("Fixture", value: fixtureDirectory.lastPathComponent)
                        LabeledContent("Segments", value: "\(controller.segmentCount)")
                    }
                    if let pairingCode = controller.formattedPairingCode {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Single-use pairing code")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Text(pairingCode)
                                .font(.system(.title2, design: .monospaced).weight(.bold))
                                .textSelection(.enabled)
                                .accessibilityIdentifier("relay-pairing-code")
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            HStack {
                Button("Choose EVENT-HLS Folder…") {
                    isChoosingFixture = true
                }
                .disabled(controller.isSessionActive || controller.lifecycle == .starting)

                Spacer()

                if controller.isSessionActive {
                    Button("Stop Relay") {
                        Task { await controller.stop() }
                    }
                    Button("Cancel Relay", role: .destructive) {
                        Task { await controller.cancel() }
                    }
                }
                Button("Done") { dismiss() }
            }
        }
        .padding(24)
        .frame(width: 560)
        .fileImporter(
            isPresented: $isChoosingFixture,
            allowedContentTypes: [.folder],
            allowsMultipleSelection: false
        ) { result in
            guard case let .success(urls) = result, let directory = urls.first else { return }
            Task { await controller.start(directory: directory) }
        }
    }
}

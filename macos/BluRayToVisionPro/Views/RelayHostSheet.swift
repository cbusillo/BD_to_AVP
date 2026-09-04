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
                    if let pairingCandidate = controller.pairingCandidate {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Compare this code with Vision Pro")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Text(pairingCandidate.shortAuthenticationString.digits)
                                .font(.system(size: 42, weight: .bold, design: .monospaced))
                                .tracking(7)
                                .accessibilityLabel("Pairing code \(pairingCandidate.shortAuthenticationString.digits.map(String.init).joined(separator: " "))")
                                .accessibilityIdentifier("relay-pairing-code")
                            Text("Confirm only if every digit matches. This request expires at \(pairingCandidate.expirationDate.formatted(date: .omitted, time: .shortened)).")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                            HStack {
                                Button("Codes Match") {
                                    Task { await controller.approveCodesMatch() }
                                }
                                .buttonStyle(.borderedProminent)
                                .accessibilityIdentifier("relay-codes-match")
                                Button("Not This Device", role: .destructive) {
                                    Task { await controller.rejectCandidate() }
                                }
                                .accessibilityIdentifier("relay-not-this-device")
                            }
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

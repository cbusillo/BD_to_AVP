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
                            Text(pairingCandidate.shortAuthenticationString.formattedDigits)
                                .font(.system(size: 42, weight: .bold, design: .monospaced))
                                .tracking(5)
                                .lineLimit(1)
                                .minimumScaleFactor(0.65)
                                .accessibilityLabel("Comparison code")
                                .accessibilityValue(pairingCandidate.shortAuthenticationString.accessibilityDigits)
                                .accessibilityAddTraits(.isStaticText)
                                .accessibilitySortPriority(2)
                                .accessibilityIdentifier("relay-sas-code")
                            Text("Confirm only if every digit matches.")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                            TimelineView(.periodic(from: .now, by: 1)) { context in
                                Text("\(remainingSeconds(until: pairingCandidate.expirationDate, now: context.date)) seconds remaining")
                                    .font(.footnote.monospacedDigit())
                                    .foregroundStyle(.secondary)
                            }
                            HStack {
                                Button("Codes Match") {
                                    Task { await controller.approveCodesMatch() }
                                }
                                .buttonStyle(.borderedProminent)
                                .disabled(pairingCandidate.isMacApproved)
                                .accessibilityIdentifier("relay-confirm-match-button")
                                Button("Not This Device", role: .destructive) {
                                    Task { await controller.rejectCandidate() }
                                }
                                .accessibilityIdentifier("relay-reject-button")
                            }
                            if pairingCandidate.isMacApproved {
                                Text("Confirmed on this Mac. Waiting for Vision Pro…")
                                    .font(.footnote.weight(.medium))
                                    .foregroundStyle(.secondary)
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

    private func remainingSeconds(until expiration: Date, now: Date) -> Int {
        max(0, Int(ceil(expiration.timeIntervalSince(now))))
    }
}

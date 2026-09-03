import SwiftUI

struct RelayConnectionView: View {
    @ObservedObject var coordinator: RelaySessionCoordinator
    let playRelay: () -> Void

    @StateObject private var pairingEntry = RelayPairingEntryModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: statusIcon)
                    .font(.title2)
                    .foregroundStyle(statusColor)
                    .frame(width: 34, height: 34)
                    .background(statusColor.opacity(0.14), in: Circle())

                VStack(alignment: .leading, spacing: 3) {
                    Text("Live Relay")
                        .font(.headline)
                    Text(statusMessage)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                statusAction
            }

            switch coordinator.state {
            case .discovery:
                discoveryContent
            case .pairing:
                pairingContent
            case .connected:
                connectedContent
            case .reconnecting, .networkUnavailable:
                reconnectingContent
            case .sessionExpired, .failed:
                expiredContent
            case .idle:
                EmptyView()
            }
        }
        .padding(.horizontal, 28)
        .padding(.vertical, 18)
        .background(.thinMaterial)
        .accessibilityIdentifier("relay-connection-panel")
    }

    @ViewBuilder
    private var statusAction: some View {
        switch coordinator.state {
        case .idle, .sessionExpired, .failed:
            Button("Find My Mac") {
                coordinator.startDiscovery()
            }
            .buttonStyle(.borderedProminent)
            .accessibilityIdentifier("relay-discover-button")
        case .connected:
            Button("Disconnect") {
                coordinator.disconnect()
            }
            .buttonStyle(.bordered)
        case .discovery, .pairing, .reconnecting, .networkUnavailable:
            EmptyView()
        }
    }

    @ViewBuilder
    private var discoveryContent: some View {
        if coordinator.discoveredServers.isEmpty {
            HStack(spacing: 10) {
                ProgressView()
                Text("Looking for Macs sharing a live relay nearby…")
                    .foregroundStyle(.secondary)
            }
            .padding(.vertical, 4)
        } else {
            VStack(alignment: .leading, spacing: 8) {
                Text("Choose your Mac")
                    .font(.subheadline.weight(.semibold))
                ForEach(coordinator.discoveredServers) { server in
                    Button {
                        Task { await coordinator.connect(to: server) }
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(server.displayName)
                                    .font(.body.weight(.medium))
                                Text(server.baseURL.host(percentEncoded: false) ?? server.baseURL.absoluteString)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Image(systemName: "chevron.right")
                                .foregroundStyle(.tertiary)
                        }
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("relay-server-\(server.id)")
                }
            }
        }
    }

    private var pairingContent: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Enter the 16-character pairing code shown on \(coordinator.connectedServer?.displayName ?? "your Mac").")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            HStack(spacing: 10) {
                SecureField("Pairing code", text: $pairingEntry.pairingCode)
                    .textContentType(.oneTimeCode)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                    .fontDesign(.monospaced)
                    .accessibilityIdentifier("relay-pairing-code")
                    .onSubmit(submitPairingCode)
                Button("Pair", action: submitPairingCode)
                    .buttonStyle(.borderedProminent)
                    .disabled(pairingEntry.isSubmitting || pairingEntry.pairingCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityIdentifier("relay-pair-button")
            }
            if let pairingErrorMessage = coordinator.pairingErrorMessage {
                Label(pairingErrorMessage, systemImage: "exclamationmark.triangle.fill")
                    .font(.footnote)
                    .foregroundStyle(.orange)
                    .accessibilityIdentifier("relay-pairing-feedback")
            }
        }
    }

    private var connectedContent: some View {
        HStack {
            Label("Authenticated MV-HEVC HLS is ready to play.", systemImage: "checkmark.shield.fill")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Spacer()
            Button {
                playRelay()
            } label: {
                Label("Play Live", systemImage: "play.fill")
            }
            .buttonStyle(.borderedProminent)
            .accessibilityIdentifier("relay-play-button")
        }
    }

    private var reconnectingContent: some View {
        HStack(spacing: 10) {
            ProgressView()
            Text("Keeping your authenticated relay session alive. Playback can resume when your Mac is reachable.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
    }

    private var expiredContent: some View {
        Text("Pairing has ended. Find your Mac again to request a new pairing code.")
            .font(.subheadline)
            .foregroundStyle(.secondary)
    }

    private func submitPairingCode() {
        guard let pairingCode = pairingEntry.beginSubmission() else { return }
        Task {
            await coordinator.submitPairingCode(pairingCode)
            pairingEntry.finishSubmission()
        }
    }

    private var statusIcon: String {
        switch coordinator.state {
        case .connected:
            "checkmark.circle.fill"
        case .reconnecting, .networkUnavailable:
            "arrow.triangle.2.circlepath.circle.fill"
        case .sessionExpired, .failed:
            "exclamationmark.circle.fill"
        case .idle, .discovery, .pairing:
            "dot.radiowaves.left.and.right"
        }
    }

    private var statusColor: Color {
        switch coordinator.state {
        case .connected:
            .green
        case .reconnecting, .networkUnavailable:
            .orange
        case .sessionExpired, .failed:
            .red
        case .idle, .discovery, .pairing:
            .blue
        }
    }

    private var statusMessage: String {
        switch coordinator.state {
        case .idle:
            "Connect to a paired Mac to play its live MV-HEVC relay."
        case .discovery:
            "Looking for relay-enabled Macs on your local network."
        case let .pairing(_, expiresAt):
            "Pairing code expires \(expiresAt.formatted(date: .omitted, time: .shortened))."
        case let .connected(_, expiresAt):
            "Connected until \(expiresAt.formatted(date: .omitted, time: .shortened))."
        case let .reconnecting(attempt):
            "Reconnecting to your Mac (attempt \(attempt + 1))."
        case .networkUnavailable:
            "Waiting for your local network to return."
        case .sessionExpired:
            "The pairing session expired or is no longer available."
        case let .failed(message):
            message
        }
    }
}

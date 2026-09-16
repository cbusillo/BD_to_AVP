import SwiftUI

struct StallRecoveryNotice: View {
    let notice: StallRecoveryState.Notice
    let supportsWaiting: Bool
    let keepWaiting: () -> Void
    let stop: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text("Video output has stopped advancing")
                    .font(.callout.weight(.semibold))
                Text("The converter will retry or stop automatically if output does not resume.")
                    .font(.callout)
                if notice.pendingCommand != nil {
                    Text("Requesting more time…")
                        .font(.caption)
                } else if let message = notice.message {
                    Text(message)
                        .font(.caption)
                } else if !notice.stall.canExtend {
                    Text("No more waiting time can be added for this stall.")
                        .font(.caption)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if supportsWaiting {
                Button("Keep Waiting (2 min)", action: keepWaiting)
                    .disabled(!notice.canRequestWait)
                    .accessibilityIdentifier("stall-keep-waiting")
            }
            Button("Stop", role: .destructive, action: stop)
                .accessibilityIdentifier("stall-stop")
        }
        .padding(12)
        .background(.orange.opacity(0.1))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("conversion-stall-notice")
    }
}

import SwiftUI

struct RouteQualityConflictView: View {
    let conflict: RouteQualityConflict
    let resolve: (RouteQualityResolutionOption) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Route and quality need a decision", systemImage: "arrow.triangle.branch")
                .font(.subheadline.weight(.semibold))
            Text(conflict.reason)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(conflict.resolutions) { option in
                Button {
                    resolve(option)
                } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(option.title)
                                .fontWeight(.semibold)
                            Spacer()
                            if option.choice == .keepRequestedWorkflow,
                               let mappedStep = option.mappedStep
                            {
                                Text(mappedStep.title)
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(.secondary)
                            }
                        }
                        Text(option.detail)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .buttonStyle(.bordered)
                .disabled(!option.isAvailable)
                .accessibilityIdentifier("route-quality-resolution-\(option.id)")
            }
        }
        .padding(12)
        .background(.orange.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
        .overlay {
            RoundedRectangle(cornerRadius: 10)
                .strokeBorder(.orange.opacity(0.35))
        }
    }
}

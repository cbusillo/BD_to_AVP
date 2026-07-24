import SwiftUI

struct VideoRouteSummaryView: View {
    let title: String
    let settings: String
    let detail: String
    let systemImage: String
    let isFallback: Bool

    init(plan: VideoRoutePlan) {
        title = plan.title
        settings = plan.settingsSummary
        detail = plan.detail
        systemImage = plan.systemImage
        isFallback = false
    }

    init(report: VideoRouteReport) {
        title = report.displayTitle
        settings = report.settingsSummary
        detail = report.displayDetail
        systemImage = report.systemImage
        isFallback = report.isFallback
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: systemImage)
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(tint)
                .frame(width: 30, height: 30)
                .background(tint.opacity(0.12), in: Circle())
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 3) {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 6) {
                        Text(title)
                            .fontWeight(.medium)
                        Text(settings)
                            .foregroundStyle(.secondary)
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        Text(title)
                            .fontWeight(.medium)
                        Text(settings)
                            .foregroundStyle(.secondary)
                    }
                }
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(isFallback ? tint : .secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(title). \(settings). \(detail)")
    }

    private var tint: Color {
        isFallback ? .orange : .accentColor
    }
}

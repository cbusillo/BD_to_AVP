import SwiftUI

struct WorkerProgressGauge: View {
    let progress: WorkerProgress?
    var width: CGFloat = 72

    var body: some View {
        Group {
            if let stageFraction = progress?.stageFraction {
                ProgressView(value: stageFraction, total: 1)
                    .progressViewStyle(.linear)
            } else {
                ProgressView()
                    .controlSize(.small)
            }
        }
        .frame(width: width)
        .accessibilityHidden(true)
    }
}

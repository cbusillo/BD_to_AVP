import SwiftUI

/// "Stats for Nerds": a small window beside the movie with its file, coding, buffer and network
/// numbers. A separate window rather than text over the video, because text over the 3D picture
/// fights its depth (#1181).
struct PlaybackStatsView: View {
    static let windowID = "playback-stats"
    /// The one value the window opens with, so opening it again brings back the same window.
    static let windowValue = "current"

    @ObservedObject var session: MVHEVCPlayerSession

    @State private var sections: [PlaybackStatsSection] = []
    @State private var meter = PlaybackThroughputMeter()

    var body: some View {
        NavigationStack {
            Group {
                if sections.isEmpty {
                    ContentUnavailableView(
                        "Nothing Playing",
                        systemImage: "chart.bar.xaxis",
                        description: Text("Start a movie to see its stats here.")
                    )
                } else {
                    List(sections) { section in
                        Section(section.title) {
                            ForEach(section.rows) { row in
                                LabeledContent(row.label) {
                                    Text(row.value)
                                        .monospacedDigit()
                                        .textSelection(.enabled)
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("Stats for Nerds")
        }
        .accessibilityIdentifier("player-stats")
        .task {
            while !Task.isCancelled {
                refresh()
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
        }
    }

    private func refresh() {
        guard let sample = session.statsSample() else {
            sections = []
            meter = PlaybackThroughputMeter()
            return
        }
        let rate = sample.shareBytesRead.flatMap {
            meter.record(bytes: $0, at: ProcessInfo.processInfo.systemUptime)
        }
        sections = PlaybackStatsPresentation.sections(for: sample, readBytesPerSecond: rate)
    }
}

import SwiftUI

/// Settings for when the app may start queued videos on its own.
struct QueueScheduleSettingsPane: View {
    @ObservedObject var controller: QueueAutomationController

    @State private var errorMessage: String?
    @State private var isShowingDetails = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Queue Schedule")
                        .font(.title2.weight(.semibold))
                    Text("Choose when the app may start queued videos on its own. Start Queue always starts right away.")
                        .foregroundStyle(.secondary)
                }

                if let loadErrorMessage = controller.store.loadErrorMessage {
                    Label(loadErrorMessage, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                }

                GroupBox {
                    VStack(alignment: .leading, spacing: 12) {
                        Toggle("Start queued videos only during these times", isOn: binding(\.limitsToOperatingWindows))
                            .toggleStyle(.switch)
                            .accessibilityIdentifier("run-times-toggle")

                        ForEach(controller.runPolicy.operatingWindows) { window in
                            Divider()
                            OperatingWindowEditorRow(
                                window: window,
                                calendar: controller.calendar,
                                update: updateWindow,
                                remove: { removeWindow(window.id) }
                            )
                        }
                        .disabled(!controller.runPolicy.limitsToOperatingWindows)

                        Divider()
                        HStack {
                            Button {
                                addWindow()
                            } label: {
                                Label("Add Run Time", systemImage: "plus")
                            }
                            .disabled(!controller.runPolicy.limitsToOperatingWindows)
                            .accessibilityIdentifier("run-times-add")
                            Spacer()
                        }

                        TimelineView(.everyMinute) { _ in
                            nextRunTimePreview
                        }
                    }
                    .padding(8)
                } label: {
                    Text("Run Times").font(.headline)
                }

                GroupBox {
                    VStack(alignment: .leading, spacing: 10) {
                        Toggle("Wait while on battery power", isOn: binding(\.waitsOnBatteryPower))
                            .accessibilityIdentifier("run-policy-battery-toggle")
                        Toggle("Wait while the Mac is running hot", isOn: binding(\.waitsUnderThermalPressure))
                            .help("Waits while macOS reports serious or critical thermal pressure.")
                            .accessibilityIdentifier("run-policy-thermal-toggle")
                        Text(conditionsSummary)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("run-policy-conditions-now")
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
                } label: {
                    Text("Conditions").font(.headline)
                }

                HStack(spacing: 6) {
                    Text("Run times and conditions apply while the app is open.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Button {
                        isShowingDetails = true
                    } label: {
                        Image(systemName: "questionmark.circle")
                    }
                    .buttonStyle(.borderless)
                    .help("How run times work")
                    .accessibilityLabel("How run times work")
                    .accessibilityIdentifier("queue-schedule-details-button")
                    .popover(isPresented: $isShowingDetails, arrowEdge: .bottom) {
                        QueueScheduleDetailsView()
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .accessibilityIdentifier("queue-schedule-settings-pane")
        .alert(
            "Queue Schedule Could Not Be Updated",
            isPresented: Binding(
                get: { errorMessage != nil },
                set: { if !$0 { errorMessage = nil } }
            )
        ) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(errorMessage ?? "The queue schedule could not be updated.")
        }
    }

    @ViewBuilder
    private var nextRunTimePreview: some View {
        let policy = controller.runPolicy
        let evaluation = controller.runPolicyEvaluation()
        Group {
            if !policy.limitsToOperatingWindows {
                Label("Queued videos may start at any time.", systemImage: "clock")
            } else if let window = evaluation.currentWindow {
                Label(
                    "A run time is open now, until \(QueueRunPolicyFormatting.moment(window.end, relativeTo: evaluation.date, calendar: controller.calendar)).",
                    systemImage: "clock.badge.checkmark.fill"
                )
            } else if let nextStart = evaluation.nextWindowStart {
                Label(
                    "Next run time starts \(nextStart.formatted(Self.nextStartFormat(calendar: controller.calendar))).",
                    systemImage: "clock.arrow.circlepath"
                )
            } else {
                Label(
                    "Add a run time with at least one day. Until then, the app does not start the queue on its own.",
                    systemImage: "exclamationmark.circle"
                )
            }
        }
        .font(.callout)
        .foregroundStyle(policy.limitsToOperatingWindows ? Color.accentColor : Color.secondary)
        .accessibilityIdentifier("run-times-next-start")
    }

    private static func nextStartFormat(calendar: Calendar) -> Date.FormatStyle {
        var format = Date.FormatStyle().weekday(.wide).month(.abbreviated).day().hour().minute()
        format.calendar = calendar
        format.timeZone = calendar.timeZone
        return format
    }

    private var conditionsSummary: String {
        let conditions = controller.currentMachineConditions
        let power = conditions.isOnBatteryPower ? "This Mac is on battery power" : "This Mac is on power"
        let temperature = conditions.isUnderThermalPressure ? "and is running hot." : "and is not running hot."
        return "\(power) \(temperature) A video already converting is never interrupted by these conditions."
    }

    private func binding(_ keyPath: WritableKeyPath<QueueRunPolicy, Bool>) -> Binding<Bool> {
        Binding(
            get: { controller.runPolicy[keyPath: keyPath] },
            set: { value in
                var policy = controller.runPolicy
                policy[keyPath: keyPath] = value
                if keyPath == \QueueRunPolicy.limitsToOperatingWindows, value, policy.operatingWindows.isEmpty {
                    policy.operatingWindows = [QueueOperatingWindow()]
                }
                save(policy)
            }
        )
    }

    private func addWindow() {
        var policy = controller.runPolicy
        policy.operatingWindows.append(QueueOperatingWindow())
        save(policy)
    }

    private func updateWindow(_ window: QueueOperatingWindow) {
        var policy = controller.runPolicy
        guard let index = policy.operatingWindows.firstIndex(where: { $0.id == window.id }) else {
            return
        }
        policy.operatingWindows[index] = window
        save(policy)
    }

    private func removeWindow(_ id: UUID) {
        var policy = controller.runPolicy
        policy.operatingWindows.removeAll { $0.id == id }
        save(policy)
    }

    private func save(_ policy: QueueRunPolicy) {
        do {
            try controller.updateRunPolicy(policy)
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct OperatingWindowEditorRow: View {
    let window: QueueOperatingWindow
    let calendar: Calendar
    let update: (QueueOperatingWindow) -> Void
    let remove: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 4) {
                ForEach(QueueWeekday.ordered(for: calendar)) { day in
                    DayToggle(
                        title: calendar.shortWeekdaySymbols[day.calendarWeekday - 1],
                        accessibilityName: calendar.weekdaySymbols[day.calendarWeekday - 1],
                        isOn: window.days.contains(day)
                    ) {
                        var updated = window
                        if updated.days.contains(day) {
                            updated.days.remove(day)
                        } else {
                            updated.days.insert(day)
                        }
                        update(updated)
                    }
                    .accessibilityIdentifier("run-time-day-\(window.id.uuidString)-\(day.rawValue)")
                }
                Spacer(minLength: 8)
                Button(action: remove) {
                    Label("Remove Run Time", systemImage: "minus.circle")
                        .labelStyle(.iconOnly)
                }
                .buttonStyle(.borderless)
                .help("Remove this run time")
                .accessibilityIdentifier("run-time-remove-\(window.id.uuidString)")
            }

            HStack(spacing: 10) {
                Text("From")
                DatePicker("Start time", selection: timeBinding(\.start), displayedComponents: .hourAndMinute)
                    .datePickerStyle(.field)
                    .labelsHidden()
                    .fixedSize()
                    .accessibilityIdentifier("run-time-start-\(window.id.uuidString)")
                Text("to")
                DatePicker("End time", selection: timeBinding(\.end), displayedComponents: .hourAndMinute)
                    .datePickerStyle(.field)
                    .labelsHidden()
                    .fixedSize()
                    .accessibilityIdentifier("run-time-end-\(window.id.uuidString)")
                Text(summary)
                    .font(.caption)
                    .foregroundStyle(window.days.isEmpty ? Color.orange : Color.secondary)
                Spacer(minLength: 0)
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("run-time-row-\(window.id.uuidString)")
    }

    private var summary: String {
        if window.days.isEmpty {
            return "Choose at least one day."
        }
        let days: String
        if window.days == QueueWeekday.everyDay {
            days = "Every day"
        } else if window.days == QueueWeekday.weekdays {
            days = "Weekdays"
        } else if window.days == [.saturday, .sunday] {
            days = "Weekends"
        } else {
            days = QueueWeekday.ordered(for: calendar)
                .filter(window.days.contains)
                .map { calendar.shortWeekdaySymbols[$0.calendarWeekday - 1] }
                .joined(separator: ", ")
        }
        if window.start == window.end {
            return "\(days), for 24 hours"
        }
        return window.continuesIntoNextDay ? "\(days), ending the next morning" : days
    }

    private func timeBinding(_ keyPath: WritableKeyPath<QueueOperatingWindow, QueueClockTime>) -> Binding<Date> {
        Binding(
            get: {
                let time = window[keyPath: keyPath]
                let reference = calendar.startOfDay(for: Date(timeIntervalSinceReferenceDate: 0))
                return calendar.date(bySettingHour: time.hour, minute: time.minute, second: 0, of: reference) ?? reference
            },
            set: { date in
                let components = calendar.dateComponents([.hour, .minute], from: date)
                var updated = window
                updated[keyPath: keyPath] = QueueClockTime(hour: components.hour ?? 0, minute: components.minute ?? 0)
                update(updated)
            }
        )
    }
}

/// The rules behind run times, kept out of the pane so it stays short.
struct QueueScheduleDetailsView: View {
    private let details = [
        ("macwindow", "The app must be open. Run times do not wake the Mac or keep it awake."),
        ("moon.zzz", "A run time that passes while the Mac sleeps or the app is closed is not made up later."),
        ("film", "A video that is converting when a run time ends or a condition starts finishes normally. No new video starts until the queue may run again."),
        ("arrow.counterclockwise", "Interrupted or stopped videos never restart on their own."),
        ("pause.circle", "A pause, or a pending Start Later window, always comes first."),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("How Run Times Work")
                .font(.headline)
            ForEach(details, id: \.1) { symbol, text in
                Label {
                    Text(text).fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: symbol).foregroundStyle(.secondary)
                }
                .font(.callout)
            }
        }
        .padding(16)
        .frame(width: 340, alignment: .leading)
        .accessibilityIdentifier("queue-schedule-details")
    }
}

private struct DayToggle: View {
    let title: String
    let accessibilityName: String
    let isOn: Bool
    let toggle: () -> Void

    var body: some View {
        Button(action: toggle) {
            Text(title)
                .font(.caption.weight(.medium))
                .frame(minWidth: 34)
                .padding(.vertical, 4)
                .foregroundStyle(isOn ? Color.white : Color.primary)
                .background(isOn ? Color.accentColor : Color.secondary.opacity(0.12), in: Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(accessibilityName)
        .accessibilityAddTraits(isOn ? .isSelected : [])
    }
}

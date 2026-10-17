import Foundation

/// A day of the week, stored by name so another process can read the policy without a calendar.
enum QueueWeekday: String, Codable, CaseIterable, Comparable, Identifiable {
    case sunday, monday, tuesday, wednesday, thursday, friday, saturday

    var id: String { rawValue }

    /// The `Calendar` weekday number, where Sunday is 1.
    var calendarWeekday: Int {
        Self.allCases.firstIndex(of: self)! + 1
    }

    init?(calendarWeekday: Int) {
        guard Self.allCases.indices.contains(calendarWeekday - 1) else {
            return nil
        }
        self = Self.allCases[calendarWeekday - 1]
    }

    static let weekdays: Set<QueueWeekday> = [.monday, .tuesday, .wednesday, .thursday, .friday]
    static let everyDay = Set(allCases)

    static func < (lhs: QueueWeekday, rhs: QueueWeekday) -> Bool {
        lhs.calendarWeekday < rhs.calendarWeekday
    }

    /// The days in the order the calendar's week starts, for display.
    static func ordered(for calendar: Calendar) -> [QueueWeekday] {
        let first = calendar.firstWeekday - 1
        return (0..<7).map { allCases[(first + $0) % 7] }
    }
}

/// A wall-clock time of day.
struct QueueClockTime: Codable, Hashable, Comparable {
    let hour: Int
    let minute: Int

    init(hour: Int, minute: Int = 0) {
        self.hour = min(max(hour, 0), 23)
        self.minute = min(max(minute, 0), 59)
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let hour = try values.decode(Int.self, forKey: .hour)
        let minute = try values.decodeIfPresent(Int.self, forKey: .minute) ?? 0
        guard (0...23).contains(hour), (0...59).contains(minute) else {
            throw DecodingError.dataCorruptedError(forKey: .hour, in: values, debugDescription: "Time out of range")
        }
        self.init(hour: hour, minute: minute)
    }

    private enum CodingKeys: String, CodingKey { case hour, minute }

    var minutesSinceMidnight: Int { hour * 60 + minute }

    static func < (lhs: QueueClockTime, rhs: QueueClockTime) -> Bool {
        lhs.minutesSinceMidnight < rhs.minutesSinceMidnight
    }

    /// This time on the day that starts at `dayStart`.
    ///
    /// A time the clock skips on a daylight-saving change resolves to the moment the clock jumps,
    /// and a time that happens twice resolves to its first occurrence.
    func date(onDayStarting dayStart: Date, calendar: Calendar) -> Date {
        let match = calendar.nextDate(
            after: dayStart.addingTimeInterval(-1),
            matching: DateComponents(hour: hour, minute: minute, second: 0),
            matchingPolicy: .nextTime,
            repeatedTimePolicy: .first,
            direction: .forward
        )
        guard let match,
              let nextDay = calendar.date(byAdding: .day, value: 1, to: dayStart),
              match < calendar.startOfDay(for: nextDay)
        else {
            return dayStart
        }
        return match
    }
}

/// A recurring weekly period when the queue may run on its own.
///
/// The days are the days the period starts. An end at or before the start continues into the next
/// day, so 10:00 PM–6:00 AM on Friday runs from Friday night into Saturday morning, and an end equal
/// to the start covers a full 24 hours.
struct QueueOperatingWindow: Codable, Identifiable, Equatable {
    let id: UUID
    var days: Set<QueueWeekday>
    var start: QueueClockTime
    var end: QueueClockTime

    init(
        id: UUID = UUID(),
        days: Set<QueueWeekday> = QueueWeekday.everyDay,
        start: QueueClockTime = QueueClockTime(hour: 1),
        end: QueueClockTime = QueueClockTime(hour: 7)
    ) {
        self.id = id
        self.days = days
        self.start = start
        self.end = end
    }

    private enum CodingKeys: String, CodingKey { case id, days, start, end }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(UUID.self, forKey: .id)
        days = Set(try values.decode([QueueWeekday].self, forKey: .days))
        start = try values.decode(QueueClockTime.self, forKey: .start)
        end = try values.decode(QueueClockTime.self, forKey: .end)
    }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(id, forKey: .id)
        try values.encode(days.sorted(), forKey: .days)
        try values.encode(start, forKey: .start)
        try values.encode(end, forKey: .end)
    }

    var continuesIntoNextDay: Bool { end <= start }

    /// The period this window covers when it starts on the day beginning at `dayStart`, if it runs that day.
    func occurrence(onDayStarting dayStart: Date, calendar: Calendar) -> DateInterval? {
        let weekday = calendar.component(.weekday, from: dayStart)
        guard let day = QueueWeekday(calendarWeekday: weekday), days.contains(day) else {
            return nil
        }
        let startDate = start.date(onDayStarting: dayStart, calendar: calendar)
        let endDayStart: Date
        if continuesIntoNextDay {
            guard let nextDay = calendar.date(byAdding: .day, value: 1, to: dayStart) else {
                return nil
            }
            endDayStart = calendar.startOfDay(for: nextDay)
        } else {
            endDayStart = dayStart
        }
        let endDate = end.date(onDayStarting: endDayStart, calendar: calendar)
        guard endDate > startDate else {
            return nil
        }
        return DateInterval(start: startDate, end: endDate)
    }
}

/// When the queue may start videos on its own. Every rule is off until the person turns it on.
struct QueueRunPolicy: Codable, Equatable {
    /// Start videos on their own only during `operatingWindows`.
    var limitsToOperatingWindows: Bool
    var operatingWindows: [QueueOperatingWindow]
    var waitsOnBatteryPower: Bool
    /// Wait while macOS reports serious or critical thermal pressure.
    var waitsUnderThermalPressure: Bool
    /// What the Mac does once a run finishes with nothing left to do. It does not affect when videos start.
    var postRunAction: QueuePostRunAction

    init(
        limitsToOperatingWindows: Bool = false,
        operatingWindows: [QueueOperatingWindow] = [],
        waitsOnBatteryPower: Bool = false,
        waitsUnderThermalPressure: Bool = false,
        postRunAction: QueuePostRunAction = .doNothing
    ) {
        self.limitsToOperatingWindows = limitsToOperatingWindows
        self.operatingWindows = operatingWindows
        self.waitsOnBatteryPower = waitsOnBatteryPower
        self.waitsUnderThermalPressure = waitsUnderThermalPressure
        self.postRunAction = postRunAction
    }

    static let none = QueueRunPolicy()

    var isActive: Bool {
        limitsToOperatingWindows || waitsOnBatteryPower || waitsUnderThermalPressure
    }

    private enum CodingKeys: String, CodingKey {
        case limitsToOperatingWindows, operatingWindows, waitsOnBatteryPower, waitsUnderThermalPressure, postRunAction
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            limitsToOperatingWindows: try values.decodeIfPresent(Bool.self, forKey: .limitsToOperatingWindows) ?? false,
            operatingWindows: try values.decodeIfPresent([QueueOperatingWindow].self, forKey: .operatingWindows) ?? [],
            waitsOnBatteryPower: try values.decodeIfPresent(Bool.self, forKey: .waitsOnBatteryPower) ?? false,
            waitsUnderThermalPressure: try values.decodeIfPresent(Bool.self, forKey: .waitsUnderThermalPressure) ?? false,
            // An action this build does not know, from a later build, reads as Do Nothing.
            postRunAction: (try? values.decodeIfPresent(QueuePostRunAction.self, forKey: .postRunAction)) ?? .doNothing
        )
    }
}

/// The power and thermal state the policy depends on.
struct QueueMachineConditions: Equatable {
    var isOnBatteryPower: Bool
    var thermalState: ProcessInfo.ThermalState

    init(isOnBatteryPower: Bool = false, thermalState: ProcessInfo.ThermalState = .nominal) {
        self.isOnBatteryPower = isOnBatteryPower
        self.thermalState = thermalState
    }

    var isUnderThermalPressure: Bool {
        thermalState == .serious || thermalState == .critical
    }
}

/// Why the policy keeps the queue from starting a video right now.
enum QueueRunPolicyHold: Equatable, Codable {
    /// Outside every run time; `nextStart` is `nil` when no run time is set.
    case outsideRunTimes(nextStart: Date?)
    case onBatteryPower
    case thermalPressure

    var kind: QueueRunPolicyHoldKind {
        switch self {
        case .outsideRunTimes: .outsideRunTimes
        case .onBatteryPower: .onBatteryPower
        case .thermalPressure: .thermalPressure
        }
    }
}

enum QueueRunPolicyHoldKind: String, Codable, Equatable {
    case outsideRunTimes
    case onBatteryPower
    case thermalPressure
}

/// Which rules apply to a start.
enum QueueRunPolicyScope: Equatable {
    /// Run times and machine conditions, for starts the app makes on its own.
    case all
    /// Machine conditions only, for a pending Start Later window, which runs at its own time.
    case machineConditionsOnly
}

/// The policy applied to one moment, with the given power and thermal state.
struct QueueRunPolicyEvaluation: Equatable {
    let date: Date
    /// The run time containing `date`, joined with run times that overlap or follow it directly.
    let currentWindow: DateInterval?
    /// The next run time start after `date`, within the coming week.
    let nextWindowStart: Date?
    /// Every rule that holds right now, run times first.
    let holds: [QueueRunPolicyHold]

    func hold(for scope: QueueRunPolicyScope) -> QueueRunPolicyHold? {
        switch scope {
        case .all:
            holds.first
        case .machineConditionsOnly:
            holds.first { $0.kind != .outsideRunTimes }
        }
    }

    var allowsStarting: Bool { holds.isEmpty }

    static func evaluate(
        _ policy: QueueRunPolicy,
        at date: Date,
        conditions: QueueMachineConditions,
        calendar: Calendar
    ) -> QueueRunPolicyEvaluation {
        let windows = policy.operatingWindows
        let currentWindow = Self.window(containing: date, windows: windows, calendar: calendar)
        let nextWindowStart = Self.nextStart(after: date, windows: windows, calendar: calendar)
        var holds: [QueueRunPolicyHold] = []
        if policy.limitsToOperatingWindows, currentWindow == nil {
            holds.append(.outsideRunTimes(nextStart: nextWindowStart))
        }
        if policy.waitsOnBatteryPower, conditions.isOnBatteryPower {
            holds.append(.onBatteryPower)
        }
        if policy.waitsUnderThermalPressure, conditions.isUnderThermalPressure {
            holds.append(.thermalPressure)
        }
        return QueueRunPolicyEvaluation(
            date: date,
            currentWindow: currentWindow,
            nextWindowStart: nextWindowStart,
            holds: holds
        )
    }

    /// The next moment the evaluation can change on its own, from a run time starting or ending.
    var nextBoundary: Date? {
        [currentWindow?.end, nextWindowStart].compactMap { $0 }.min()
    }

    private static func occurrences(around date: Date, windows: [QueueOperatingWindow], dayOffsets: ClosedRange<Int>, calendar: Calendar) -> [DateInterval] {
        let today = calendar.startOfDay(for: date)
        return dayOffsets.flatMap { offset -> [DateInterval] in
            guard let day = calendar.date(byAdding: .day, value: offset, to: today) else {
                return []
            }
            let dayStart = calendar.startOfDay(for: day)
            return windows.compactMap { $0.occurrence(onDayStarting: dayStart, calendar: calendar) }
        }
    }

    private static func window(containing date: Date, windows: [QueueOperatingWindow], calendar: Calendar) -> DateInterval? {
        let candidates = occurrences(around: date, windows: windows, dayOffsets: -1...8, calendar: calendar)
        let containing = candidates.filter { $0.start <= date && date < $0.end }
        guard var merged = containing.min(by: { $0.start < $1.start }) else {
            return nil
        }
        merged = DateInterval(start: merged.start, end: containing.map(\.end).max() ?? merged.end)
        // Run times that overlap or start exactly when this one ends keep the queue running.
        var extended = true
        while extended {
            extended = false
            for interval in candidates where interval.start <= merged.end && interval.end > merged.end {
                merged = DateInterval(start: merged.start, end: interval.end)
                extended = true
            }
        }
        return merged
    }

    private static func nextStart(after date: Date, windows: [QueueOperatingWindow], calendar: Calendar) -> Date? {
        occurrences(around: date, windows: windows, dayOffsets: 0...8, calendar: calendar)
            .map(\.start)
            .filter { $0 > date }
            .min()
    }
}

/// The parts of the queue's state that decide whether the policy starts it.
struct QueueRunPolicyQueueSnapshot: Equatable {
    var runState: PersistentQueueRunState
    var hasPendingStartLaterSchedule: Bool
    var isOtherWorkActive: Bool
    /// Items in the waiting state, in queue order. Interrupted, stopped, and held items are never included.
    var waitingItemIDs: [UUID]
    /// Items an automatic start left waiting because of a machine condition, to start when it clears.
    var deferredItemIDs: Set<UUID>
    /// For items deferred from a Start Later run, when that window ends.
    var deferredWindowEnd: Date?
    /// Whether the running queue was started by the app on its own and follows the policy.
    var isRunningUnderPolicy: Bool

    init(
        runState: PersistentQueueRunState = .idle,
        hasPendingStartLaterSchedule: Bool = false,
        isOtherWorkActive: Bool = false,
        waitingItemIDs: [UUID] = [],
        deferredItemIDs: Set<UUID> = [],
        deferredWindowEnd: Date? = nil,
        isRunningUnderPolicy: Bool = false
    ) {
        self.runState = runState
        self.hasPendingStartLaterSchedule = hasPendingStartLaterSchedule
        self.isOtherWorkActive = isOtherWorkActive
        self.waitingItemIDs = waitingItemIDs
        self.deferredItemIDs = deferredItemIDs
        self.deferredWindowEnd = deferredWindowEnd
        self.isRunningUnderPolicy = isRunningUnderPolicy
    }
}

/// What the policy does with the queue at one moment.
enum QueueRunPolicyDecision: Equatable {
    /// The policy has nothing to start and nothing to explain.
    case inactive
    /// The app started the queue on its own; it keeps going until `until`, when one applies.
    case running(until: Date?)
    /// Waiting items stay waiting for this reason.
    case waiting(QueueRunPolicyHold, itemCount: Int)
    /// Start the queue with these items; `windowEnd` carries a Start Later window's end.
    case start(itemIDs: [UUID], windowEnd: Date?)
    /// A run time is open but the queue is not idle for another reason, such as other work.
    case blockedByOtherWork(itemCount: Int)

    /// Decides deterministically from the policy, the moment's evaluation, and the queue.
    ///
    /// The person's controls always win: a pause, a requested pause, or a pending Start Later
    /// window is never overridden. Only items in the waiting state are ever started, so interrupted
    /// or stopped work waits for a person.
    static func decide(
        policy: QueueRunPolicy,
        evaluation: QueueRunPolicyEvaluation,
        queue: QueueRunPolicyQueueSnapshot
    ) -> QueueRunPolicyDecision {
        switch queue.runState {
        case .running, .pauseAfterCurrent:
            guard queue.isRunningUnderPolicy, policy.isActive else {
                return .inactive
            }
            return .running(until: policy.limitsToOperatingWindows ? evaluation.currentWindow?.end : nil)
        case .paused:
            return .inactive
        case .idle:
            break
        }
        if queue.hasPendingStartLaterSchedule {
            return .inactive
        }
        let waiting = queue.waitingItemIDs
        let deferredWindowIsOpen = queue.deferredWindowEnd.map { evaluation.date < $0 } ?? false
        let deferred = waiting.filter { queue.deferredItemIDs.contains($0) }

        // Items deferred from a Start Later run follow that window, not the run times.
        if deferredWindowIsOpen, !deferred.isEmpty {
            if let hold = evaluation.hold(for: .machineConditionsOnly) {
                return .waiting(hold, itemCount: deferred.count)
            }
            if queue.isOtherWorkActive {
                return .blockedByOtherWork(itemCount: deferred.count)
            }
            return .start(itemIDs: deferred, windowEnd: queue.deferredWindowEnd)
        }

        let candidates = policy.limitsToOperatingWindows ? waiting : deferred
        guard policy.isActive || !deferred.isEmpty, !candidates.isEmpty else {
            return .inactive
        }
        if let hold = evaluation.hold(for: .all) {
            return .waiting(hold, itemCount: candidates.count)
        }
        if queue.isOtherWorkActive {
            return .blockedByOtherWork(itemCount: candidates.count)
        }
        return .start(itemIDs: candidates, windowEnd: nil)
    }
}

/// One thing the policy did, kept in the automation file so its decisions can be reviewed later.
struct QueueRunPolicyActivity: Codable, Equatable, Identifiable {
    enum Kind: String, Codable, Equatable {
        /// The app started the queue on its own.
        case started
        /// Waiting items were kept from starting.
        case held
        /// A run the app started stopped starting new videos; the current one finished.
        case stoppedStartingNewItems
        /// The app tried to start the queue but it could not run.
        case startFailed
    }

    let id: UUID
    let occurredAt: Date
    let kind: Kind
    let hold: QueueRunPolicyHoldKind?
    let itemCount: Int
    /// When the hold is outside run times, the next run time start.
    let nextStart: Date?

    init(
        id: UUID = UUID(),
        occurredAt: Date,
        kind: Kind,
        hold: QueueRunPolicyHold? = nil,
        itemCount: Int
    ) {
        self.id = id
        self.occurredAt = occurredAt
        self.kind = kind
        self.hold = hold?.kind
        self.itemCount = itemCount
        if case let .outsideRunTimes(nextStart)? = hold {
            self.nextStart = nextStart
        } else {
            self.nextStart = nil
        }
    }

    func message(calendar: Calendar = .autoupdatingCurrent) -> String {
        let videos = itemCount == 1 ? "1 video" : "\(itemCount) videos"
        switch kind {
        case .started:
            return "Started the queue with \(videos)."
        case .startFailed:
            return "Could not start the queue on its own; it needs your attention."
        case .held:
            return "Kept \(videos) waiting \(holdPhrase(calendar: calendar))."
        case .stoppedStartingNewItems:
            return "Stopped starting new videos \(holdPhrase(calendar: calendar)); \(videos) still waiting."
        }
    }

    private func holdPhrase(calendar: Calendar) -> String {
        switch hold {
        case .outsideRunTimes:
            if let nextStart {
                return "until \(QueueRunPolicyFormatting.moment(nextStart, relativeTo: occurredAt, calendar: calendar))"
            }
            return "because no run times are set"
        case .onBatteryPower:
            return "while on battery power"
        case .thermalPressure:
            return "while the Mac cools down"
        case nil:
            return ""
        }
    }
}

enum QueueRunPolicyFormatting {
    /// A time, with its day when it is not within the next day.
    static func moment(_ date: Date, relativeTo now: Date, calendar: Calendar = .autoupdatingCurrent) -> String {
        var time = Date.FormatStyle(date: .omitted, time: .shortened)
        time.calendar = calendar
        time.timeZone = calendar.timeZone
        if date.timeIntervalSince(now) < 24 * 60 * 60 {
            return date.formatted(time)
        }
        var day = Date.FormatStyle().weekday(.wide)
        day.calendar = calendar
        day.timeZone = calendar.timeZone
        return "\(date.formatted(day)) \(date.formatted(time))"
    }

    static func waitingTitle(for hold: QueueRunPolicyHold, now: Date, calendar: Calendar = .autoupdatingCurrent) -> String {
        switch hold {
        case let .outsideRunTimes(nextStart?):
            "Waiting for \(moment(nextStart, relativeTo: now, calendar: calendar))"
        case .outsideRunTimes(nil):
            "Waiting — no run times are set"
        case .onBatteryPower:
            "Waiting while on battery power"
        case .thermalPressure:
            "Waiting while the Mac cools down"
        }
    }

    static func waitingDetail(for hold: QueueRunPolicyHold, itemCount: Int) -> String {
        let videos = itemCount == 1 ? "1 waiting video starts" : "\(itemCount) waiting videos start"
        let when = switch hold {
        case .outsideRunTimes(nextStart: .some):
            "at the next run time"
        case .outsideRunTimes(nextStart: nil):
            "once a run time is set in Settings"
        case .onBatteryPower:
            "when the Mac is connected to power"
        case .thermalPressure:
            "when the Mac is cooler"
        }
        return "\(videos) \(when). Start Queue runs them now."
    }
}

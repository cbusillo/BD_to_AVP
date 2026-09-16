import Foundation

struct LiveObservabilityStatus: Equatable, Sendable {
    typealias ProcessState = LiveToolObservabilityStatus.ProcessState
    typealias ActivityState = LiveToolObservabilityStatus.ActivityState
    typealias ArtifactStatus = LiveToolObservabilityStatus.ArtifactStatus

    private var tools: [String: LiveToolObservabilityStatus] = [:]
    private var focusedToolKey: String?
    private var artifactToolKey: String?
    private var retiredToolKeys: [String] = []
    private var jobID: String?
    private(set) var stageID: String?
    private(set) var updatedAt: Date?

    static let empty = LiveObservabilityStatus()

    var toolID: String? { focusedTool?.toolID }
    var toolRunID: String? { focusedTool?.toolRunID }
    var processState: ProcessState? { focusedTool?.processState }
    var lastOutputAgeSeconds: Int64? { focusedTool?.lastOutputAgeSeconds }
    var lastOutputAgeSampledAt: Date? { focusedTool?.lastOutputAgeSampledAt }
    var artifactRole: String? { artifactTool?.artifactRole }
    var artifactState: String? { artifactTool?.artifactState }
    var artifactSizeBytes: Int64? { artifactTool?.artifactSizeBytes }
    var artifactModificationAgeSeconds: Int64? { artifactTool?.artifactModificationAgeSeconds }
    var artifactGrowthBytesPerSecond: Int64? { artifactTool?.artifactGrowthBytesPerSecond }
    var hasDetails: Bool { stageID != nil || tools.values.contains { $0.hasDetails } }

    var artifacts: [ArtifactStatus] {
        tools.keys.sorted().flatMap { tools[$0]?.artifacts ?? [] }.sorted { left, right in
            let leftPriority = LiveToolObservabilityStatus.artifactPriority(left.role)
            let rightPriority = LiveToolObservabilityStatus.artifactPriority(right.role)
            return leftPriority == rightPriority ? left.role < right.role : leftPriority < rightPriority
        }
    }

    private var focusedTool: LiveToolObservabilityStatus? {
        focusedToolKey.flatMap { tools[$0] }
    }

    private var artifactTool: LiveToolObservabilityStatus? {
        artifactToolKey.flatMap { tools[$0] }
    }

    mutating func receive(_ event: ObservabilityEvent, receivedAt: Date) {
        let incomingRunID = LiveToolObservabilityStatus.safeIdentifier(event.context.tool?.runID)
        let incomingToolID = LiveToolObservabilityStatus.safeIdentifier(event.context.tool?.id)
        let incomingJobID = LiveToolObservabilityStatus.safeIdentifier(event.context.correlation.jobID)
        let incomingStageID = LiveToolObservabilityStatus.safeIdentifier(event.context.stage?.id)
        let jobChanged = jobID != nil && incomingJobID != nil && jobID != incomingJobID
        if jobChanged {
            tools.removeAll(keepingCapacity: true)
            retiredToolKeys.removeAll(keepingCapacity: true)
            focusedToolKey = nil
            artifactToolKey = nil
            stageID = nil
        }
        if let incomingJobID { jobID = incomingJobID }
        guard incomingRunID != nil || incomingToolID != nil else {
            if let incomingStageID, let stageID, incomingStageID != stageID {
                retireTools(Array(tools.keys))
            }
            if let incomingStageID { stageID = incomingStageID }
            updatedAt = receivedAt
            return
        }
        let key = incomingRunID.map { "run:\($0)" } ?? "legacy:\(incomingToolID ?? "unknown")"
        if event.kind != "tool.started", retiredToolKeys.contains(key) { return }
        let stageChanged = stageID != nil && incomingStageID != nil && stageID != incomingStageID
        let processStates = tools.values.compactMap(\.processState)
        let completedAttempt = !processStates.isEmpty && processStates.allSatisfy(\.isTerminal)
        if (stageChanged && tools[key] == nil) || (event.kind == "tool.started" && completedAttempt) {
            retireTools(Array(tools.keys))
        }
        if let incomingStageID { stageID = incomingStageID }

        if event.kind == "tool.started" {
            retireTools(tools.keys.filter { $0 != key && tools[$0]?.processState?.isTerminal == true })
            retiredToolKeys.removeAll { $0 == key }
        }

        var tool = tools[key] ?? .empty
        if event.kind == "tool.started", tool.processState?.isTerminal == true {
            tool = .empty
        }
        tool.receive(event, receivedAt: receivedAt)
        tools[key] = tool
        focusedToolKey = key
        if event.data.artifact != nil { artifactToolKey = key }
        updatedAt = receivedAt
    }

    private mutating func retireTools(_ keys: [String]) {
        for key in keys {
            if key.hasPrefix("run:"), !retiredToolKeys.contains(key) {
                retiredToolKeys.append(key)
            }
            tools.removeValue(forKey: key)
            if focusedToolKey == key { focusedToolKey = nil }
            if artifactToolKey == key { artifactToolKey = nil }
        }
        if retiredToolKeys.count > 32 {
            retiredToolKeys.removeFirst(retiredToolKeys.count - 32)
        }
    }

    func currentLastOutputAgeSeconds(at date: Date) -> Int64? {
        focusedTool?.currentLastOutputAgeSeconds(at: date)
    }

    func activityState(at date: Date, thresholdSeconds: Int64 = 60) -> ActivityState {
        let runningTools = tools.values.filter { $0.processState == .running || $0.processState == .cancelling }
        guard !runningTools.isEmpty else { return .active }
        // A producer can keep printing identical statistics while the encoder makes no progress.
        if runningTools.contains(where: { $0.hasStalledArtifact(at: date, thresholdSeconds: thresholdSeconds) }) {
            return .stalled
        }
        let states = runningTools.map { $0.activityState(at: date, thresholdSeconds: thresholdSeconds) }
        if states.contains(.active) { return .active }
        if states.contains(.toolQuietArtifactsActive) { return .toolQuietArtifactsActive }
        return .stalled
    }

    func isStalled(at date: Date, thresholdSeconds: Int64 = 60) -> Bool {
        activityState(at: date, thresholdSeconds: thresholdSeconds) == .stalled
    }

    static func processState(for event: ObservabilityEvent) -> ProcessState? {
        LiveToolObservabilityStatus.processState(for: event)
    }
}

struct LiveToolObservabilityStatus: Equatable, Sendable {
    enum ProcessState: String, Equatable, Sendable {
        case running
        case cancelling
        case completed
        case failed
        case cancelled

        var isTerminal: Bool {
            self == .completed || self == .failed || self == .cancelled
        }
    }

    enum ActivityState: Equatable, Sendable {
        case active
        case toolQuietArtifactsActive
        case stalled
    }

    struct ArtifactStatus: Equatable, Sendable {
        let role: String
        let state: String?
        let sizeBytes: Int64?
        let modificationAgeSeconds: Int64?
        let growthBytesPerSecond: Int64?
        let sampledAt: Date

        func currentModificationAgeSeconds(at date: Date) -> Int64? {
            guard let modificationAgeSeconds else {
                return nil
            }
            let hostElapsed = max(0, Int64(date.timeIntervalSince(sampledAt).rounded(.down)))
            return modificationAgeSeconds + hostElapsed
        }

        func isRecentlyActive(at date: Date, thresholdSeconds: Int64) -> Bool {
            guard state == "growing" else {
                return false
            }
            let sampleAgeSeconds = max(0, Int64(date.timeIntervalSince(sampledAt).rounded(.down)))
            if let growthBytesPerSecond,
               growthBytesPerSecond > 0,
               sampleAgeSeconds < thresholdSeconds
            {
                return true
            }
            guard let modificationAgeSeconds = currentModificationAgeSeconds(at: date) else {
                return false
            }
            return modificationAgeSeconds < thresholdSeconds
        }
    }

    private(set) var stageID: String?
    private(set) var toolID: String?
    private(set) var toolRunID: String?
    private(set) var processState: ProcessState?
    private(set) var lastOutputAgeSeconds: Int64?
    private(set) var lastOutputAgeSampledAt: Date?
    private var artifactSamples: [ArtifactStatus] = []
    private var mostRecentArtifactRole: String?
    private(set) var updatedAt: Date?

    static let empty = LiveToolObservabilityStatus()

    var artifacts: [ArtifactStatus] {
        artifactSamples.sorted { left, right in
            let leftPriority = Self.artifactPriority(left.role)
            let rightPriority = Self.artifactPriority(right.role)
            if leftPriority != rightPriority {
                return leftPriority < rightPriority
            }
            return left.role < right.role
        }
    }

    var artifactRole: String? {
        focusedArtifact?.role
    }

    var artifactState: String? {
        focusedArtifact?.state
    }

    var artifactSizeBytes: Int64? {
        focusedArtifact?.sizeBytes
    }

    var artifactModificationAgeSeconds: Int64? {
        focusedArtifact?.modificationAgeSeconds
    }

    var artifactGrowthBytesPerSecond: Int64? {
        focusedArtifact?.growthBytesPerSecond
    }

    var hasDetails: Bool {
        stageID != nil
            || toolID != nil
            || processState != nil
            || lastOutputAgeSeconds != nil
            || !artifactSamples.isEmpty
    }

    mutating func receive(_ event: ObservabilityEvent, receivedAt: Date) {
        let incomingToolID = Self.safeIdentifier(event.context.tool?.id)
        let incomingToolRunID = Self.safeIdentifier(event.context.tool?.runID)
        let belongsToCurrentTool = incomingToolRunID != nil
            ? incomingToolRunID == toolRunID
            : incomingToolID == nil || incomingToolID == toolID
        let toolDidChange: Bool
        if let incomingToolRunID {
            toolDidChange = incomingToolRunID != toolRunID
        } else if let incomingToolID {
            toolDidChange = incomingToolID != toolID
        } else {
            toolDidChange = false
        }
        if let stageID = Self.safeIdentifier(event.context.stage?.id) {
            self.stageID = stageID
        }
        if toolDidChange {
            artifactSamples.removeAll(keepingCapacity: true)
            mostRecentArtifactRole = nil
        }
        if let processState = Self.processState(for: event) {
            let wouldRegressTerminalState = self.processState?.isTerminal == true
                && !processState.isTerminal
                && event.kind != "tool.started"
                && belongsToCurrentTool
            if !wouldRegressTerminalState {
                self.processState = processState
            }
        }
        if let incomingToolID {
            toolID = incomingToolID
        }
        if let incomingToolRunID {
            toolRunID = incomingToolRunID
        }
        if let age = event.data.activity?.lastOutputAgeSeconds {
            lastOutputAgeSeconds = age
            lastOutputAgeSampledAt = receivedAt
        }
        if let artifact = event.data.artifact {
            updateArtifact(artifact, receivedAt: receivedAt)
        }
        updatedAt = receivedAt
    }

    func currentLastOutputAgeSeconds(at date: Date) -> Int64? {
        guard let lastOutputAgeSeconds else {
            return nil
        }
        guard processState == .running || processState == .cancelling,
              let lastOutputAgeSampledAt
        else {
            return lastOutputAgeSeconds
        }
        let hostElapsed = max(
            0,
            Int64(date.timeIntervalSince(lastOutputAgeSampledAt).rounded(.down))
        )
        return lastOutputAgeSeconds + hostElapsed
    }

    func activityState(at date: Date, thresholdSeconds: Int64 = 60) -> ActivityState {
        guard processState == .running || processState == .cancelling else {
            return .active
        }
        if hasStalledArtifact(at: date, thresholdSeconds: thresholdSeconds) {
            return .stalled
        }
        guard let age = currentLastOutputAgeSeconds(at: date), age >= thresholdSeconds else {
            return .active
        }
        if artifacts.contains(where: { $0.isRecentlyActive(at: date, thresholdSeconds: thresholdSeconds) }) {
            return .toolQuietArtifactsActive
        }
        return .stalled
    }

    func hasStalledArtifact(at date: Date, thresholdSeconds: Int64) -> Bool {
        artifacts.contains { artifact in
            guard artifact.state == "growing",
                  let age = artifact.currentModificationAgeSeconds(at: date),
                  age >= thresholdSeconds
            else {
                return false
            }
            return !artifact.isRecentlyActive(at: date, thresholdSeconds: thresholdSeconds)
        }
    }

    func isStalled(at date: Date, thresholdSeconds: Int64 = 60) -> Bool {
        activityState(at: date, thresholdSeconds: thresholdSeconds) == .stalled
    }

    static func processState(for event: ObservabilityEvent) -> ProcessState? {
        switch event.kind {
        case "tool.started":
            return .running
        case "tool.completed":
            return .completed
        case "tool.failed":
            return .failed
        case "tool.cancelled":
            return .cancelled
        default:
            break
        }
        if event.data.cancellation?.requested == true {
            return .cancelling
        }
        if event.context.process?.signal != nil {
            return .failed
        }
        if let exitCode = event.context.process?.exitCode {
            return exitCode == 0 ? .completed : .failed
        }
        if event.context.process?.processID != nil || event.context.process?.processGroupID != nil {
            return .running
        }
        return nil
    }

    private static func nonNegative(_ value: Int64?) -> Int64? {
        guard let value, value >= 0 else {
            return nil
        }
        return value
    }

    private var focusedArtifact: ArtifactStatus? {
        if let mostRecentArtifactRole,
           let artifact = artifactSamples.first(where: { $0.role == mostRecentArtifactRole })
        {
            return artifact
        }
        return artifacts.first
    }

    private mutating func updateArtifact(_ artifact: ObservabilityArtifact, receivedAt: Date) {
        guard let role = Self.safeIdentifier(artifact.role) else {
            return
        }
        let snapshot = ArtifactStatus(
            role: role,
            state: Self.safeIdentifier(artifact.state),
            sizeBytes: Self.nonNegative(artifact.sizeBytes),
            modificationAgeSeconds: Self.nonNegative(artifact.modificationAgeSeconds),
            growthBytesPerSecond: Self.nonNegative(artifact.growthBytesPerSecond),
            sampledAt: receivedAt
        )
        if let existingIndex = artifactSamples.firstIndex(where: { $0.role == role }) {
            artifactSamples[existingIndex] = snapshot
        } else {
            artifactSamples.append(snapshot)
        }
        mostRecentArtifactRole = role
    }

    static func artifactPriority(_ role: String) -> Int {
        switch role {
        case "left_eye_video_output":
            return 0
        case "right_eye_video_output":
            return 1
        case "stereo_video_output":
            return 2
        default:
            return 100
        }
    }

    static func safeIdentifier(_ value: String?) -> String? {
        guard let value,
              !value.isEmpty,
              value.utf8.count <= 128,
              value.unicodeScalars.allSatisfy({ scalar in
                  CharacterSet.alphanumerics.contains(scalar)
                      || scalar == "."
                      || scalar == "_"
                      || scalar == "-"
              })
        else {
            return nil
        }
        return value
    }
}

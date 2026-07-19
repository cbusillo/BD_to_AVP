import Foundation

struct LiveObservabilityStatus: Equatable, Sendable {
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

    private(set) var stageID: String?
    private(set) var toolID: String?
    private(set) var processState: ProcessState?
    private(set) var lastOutputAgeSeconds: Int64?
    private(set) var lastOutputAgeSampledAt: Date?
    private(set) var artifactRole: String?
    private(set) var artifactState: String?
    private(set) var artifactSizeBytes: Int64?
    private(set) var artifactModificationAgeSeconds: Int64?
    private(set) var artifactGrowthBytesPerSecond: Int64?
    private(set) var updatedAt: Date?

    static let empty = LiveObservabilityStatus()

    var hasDetails: Bool {
        stageID != nil
            || toolID != nil
            || processState != nil
            || lastOutputAgeSeconds != nil
            || artifactRole != nil
            || artifactState != nil
            || artifactSizeBytes != nil
            || artifactGrowthBytesPerSecond != nil
    }

    mutating func receive(_ event: ObservabilityEvent, receivedAt: Date) {
        let incomingToolID = Self.safeIdentifier(event.context.tool?.id)
        let belongsToCurrentTool = incomingToolID == nil || incomingToolID == toolID
        if let stageID = Self.safeIdentifier(event.context.stage?.id) {
            self.stageID = stageID
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
        if let age = event.data.activity?.lastOutputAgeSeconds {
            lastOutputAgeSeconds = age
            lastOutputAgeSampledAt = receivedAt
        }
        if let artifact = event.data.artifact {
            artifactRole = Self.safeIdentifier(artifact.role)
            artifactState = Self.safeIdentifier(artifact.state)
            artifactSizeBytes = Self.nonNegative(artifact.sizeBytes)
            artifactModificationAgeSeconds = Self.nonNegative(artifact.modificationAgeSeconds)
            artifactGrowthBytesPerSecond = Self.nonNegative(artifact.growthBytesPerSecond)
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

    func isStalled(at date: Date, thresholdSeconds: Int64 = 60) -> Bool {
        guard processState == .running || processState == .cancelling,
              let age = currentLastOutputAgeSeconds(at: date)
        else {
            return false
        }
        return age >= thresholdSeconds
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

    private static func safeIdentifier(_ value: String?) -> String? {
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

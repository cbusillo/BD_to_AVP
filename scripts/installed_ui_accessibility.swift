import AppKit
import ApplicationServices
import Foundation

private let navigationChildrenAttribute = "AXChildrenInNavigationOrder"
private let maximumElements = 2_000
private let pollInterval: TimeInterval = 0.25

private enum CollectorMode: String {
    case candidate
    case updater
}

private enum CollectorError: Error, CustomStringConvertible {
    case invalidArgument(String)
    case accessibilityUnavailable
    case multipleApplications(Int)
    case timedOut(CollectorMode)

    var description: String {
        switch self {
        case let .invalidArgument(message):
            return message
        case .accessibilityUnavailable:
            return "The installed UI accessibility collector is not trusted for AX inspection."
        case let .multipleApplications(count):
            return "Expected at most one production app process, found \(count)."
        case let .timedOut(mode):
            return "Timed out waiting for \(mode.rawValue) accessibility evidence."
        }
    }
}

private struct CollectorConfiguration {
    let mode: CollectorMode
    let bundleIdentifier: String
    let expectedURL: String
    let outputURL: URL
    let timeout: TimeInterval

    init(arguments: [String]) throws {
        func value(for option: String) throws -> String {
            guard let index = arguments.firstIndex(of: option), arguments.indices.contains(index + 1) else {
                throw CollectorError.invalidArgument("Missing required option: \(option)")
            }
            return arguments[index + 1]
        }

        let modeValue = try value(for: "--mode")
        guard let mode = CollectorMode(rawValue: modeValue) else {
            throw CollectorError.invalidArgument("Unsupported collector mode: \(modeValue)")
        }
        let timeoutValue = try value(for: "--timeout-seconds")
        guard let timeout = TimeInterval(timeoutValue), timeout > 0 else {
            throw CollectorError.invalidArgument("Collector timeout must be a positive number.")
        }
        self.mode = mode
        bundleIdentifier = try value(for: "--bundle-identifier")
        expectedURL = try value(for: "--expected-url")
        outputURL = URL(fileURLWithPath: try value(for: "--output"))
        self.timeout = timeout
    }
}

private struct AccessibilityRecord {
    let actions: [String]
    let enabled: Bool
    let help: String
    let identifier: String
    let label: String
    let role: String
    let url: String?

    var jsonValue: [String: Any] {
        var value: [String: Any] = [
            "actions": actions,
            "enabled": enabled,
            "help": help,
            "identifier": identifier,
            "label": label,
            "role": role,
        ]
        if let url {
            value["url"] = url
        }
        return value
    }
}

private func attributeValue(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else {
        return nil
    }
    return value
}

private func attribute<T>(_ element: AXUIElement, _ name: String) -> T? {
    attributeValue(element, name) as? T
}

private func actionNames(_ element: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyActionNames(element, &names) == .success else {
        return []
    }
    return (names as? [String] ?? []).sorted()
}

private func normalizedURL(_ value: CFTypeRef?) -> String? {
    if let url = value as? URL {
        return url.absoluteString
    }
    return value as? String
}

private func accessibilityElements(root: AXUIElement) -> [AXUIElement] {
    var pending = [root]
    var elements: [AXUIElement] = []
    var visited = Set<CFHashCode>()
    while let element = pending.popLast(), elements.count < maximumElements {
        guard visited.insert(CFHash(element)).inserted else {
            continue
        }
        elements.append(element)
        let windows: [AXUIElement] = attribute(element, kAXWindowsAttribute) ?? []
        let navigationChildren: [AXUIElement] = attribute(element, navigationChildrenAttribute) ?? []
        let children: [AXUIElement] = attribute(element, kAXChildrenAttribute) ?? []
        pending.append(contentsOf: (windows + navigationChildren + children).reversed())
    }
    return elements
}

private func record(for element: AXUIElement, identifier: String) -> AccessibilityRecord {
    AccessibilityRecord(
        actions: actionNames(element),
        enabled: attribute(element, kAXEnabledAttribute) ?? false,
        help: attribute(element, kAXHelpAttribute) ?? "",
        identifier: identifier,
        label: attribute(element, kAXTitleAttribute) ?? attribute(element, kAXDescriptionAttribute) ?? "",
        role: attribute(element, kAXRoleAttribute) ?? "",
        url: normalizedURL(attributeValue(element, kAXURLAttribute))
    )
}

private let candidateIdentifiers = [
    "all-releases-link",
    "save-profile-action",
    "update-action",
    "update-route-picker",
]

private func collectCandidateRecords(
    elements: [AXUIElement],
    expectedURL: String,
    records: inout [String: AccessibilityRecord]
) {
    for element in elements {
        guard let identifier: String = attribute(element, kAXIdentifierAttribute),
              candidateIdentifiers.contains(identifier),
              records[identifier] == nil
        else {
            continue
        }
        let candidate = record(for: element, identifier: identifier)
        if identifier == "all-releases-link", candidate.url != expectedURL {
            continue
        }
        records[identifier] = candidate
    }
}

private func candidatePayload(records: [String: AccessibilityRecord]) -> [String: Any]? {
    guard records.count == candidateIdentifiers.count else {
        return nil
    }
    return [
        "elements": candidateIdentifiers.map { records[$0]!.jsonValue },
        "schema_version": 1,
    ]
}

private func updaterPayload(elements: [AXUIElement], expectedURL: String) -> [String: Any]? {
    let observedURLs = Set(elements.compactMap { element -> String? in
        guard let role: String = attribute(element, kAXRoleAttribute), role == "AXLink" else {
            return nil
        }
        return normalizedURL(attributeValue(element, kAXURLAttribute))
    })
    guard observedURLs.contains(expectedURL) else {
        return nil
    }
    return [
        "release_notes_url": expectedURL,
        "release_notes_url_observed": true,
        "schema_version": 1,
    ]
}

private func writeJSON(_ value: [String: Any], to outputURL: URL) throws {
    try FileManager.default.createDirectory(
        at: outputURL.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    let data = try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys])
    try (data + Data("\n".utf8)).write(to: outputURL, options: .atomic)
}

private func collect(configuration: CollectorConfiguration) throws {
    guard AXIsProcessTrusted() else {
        throw CollectorError.accessibilityUnavailable
    }
    let deadline = Date().addingTimeInterval(configuration.timeout)
    var candidateRecords: [String: AccessibilityRecord] = [:]
    repeat {
        let applications = NSRunningApplication.runningApplications(
            withBundleIdentifier: configuration.bundleIdentifier
        )
        if applications.count > 1 {
            throw CollectorError.multipleApplications(applications.count)
        }
        if let application = applications.first {
            let elements = accessibilityElements(
                root: AXUIElementCreateApplication(application.processIdentifier)
            )
            let payload: [String: Any]?
            switch configuration.mode {
            case .candidate:
                collectCandidateRecords(
                    elements: elements,
                    expectedURL: configuration.expectedURL,
                    records: &candidateRecords
                )
                payload = candidatePayload(records: candidateRecords)
            case .updater:
                payload = updaterPayload(elements: elements, expectedURL: configuration.expectedURL)
            }
            if let payload {
                try writeJSON(payload, to: configuration.outputURL)
                return
            }
        }
        Thread.sleep(forTimeInterval: pollInterval)
    } while Date() < deadline
    throw CollectorError.timedOut(configuration.mode)
}

do {
    let configuration = try CollectorConfiguration(arguments: Array(CommandLine.arguments.dropFirst()))
    try collect(configuration: configuration)
} catch {
    FileHandle.standardError.write(Data("installed UI accessibility error: \(error)\n".utf8))
    exit(2)
}

import Foundation

final class DiagnosticRedactor {
    private let scope: String
    private var pathTokens: [String: String] = [:]
    private var jobTokens: [UUID: String] = [:]
    private var identifierTokens: [String: String] = [:]
    private var nameTokens: [String: String] = [:]
    private var exactReplacements: [String: String] = [:]

    init(bundleID: UUID) {
        scope = String(bundleID.uuidString.replacingOccurrences(of: "-", with: "").prefix(8)).uppercased()
    }

    func pathToken(for url: URL) -> String {
        pathToken(for: url.standardizedFileURL.path)
    }

    func pathToken(for rawPath: String) -> String {
        let normalizedPath = Self.normalizedPath(rawPath)
        if let token = pathTokens[normalizedPath] {
            return token
        }
        let token = String(format: "<path:%@:%03d>", scope, pathTokens.count + 1)
        pathTokens[normalizedPath] = token
        registerExact(rawPath, replacement: token)
        registerExact(normalizedPath, replacement: token)
        if normalizedPath.hasPrefix("/") {
            registerExact(URL(fileURLWithPath: normalizedPath).absoluteString, replacement: token)
        }
        let filename = URL(fileURLWithPath: normalizedPath).lastPathComponent
        if !filename.isEmpty && filename != "/" {
            registerExact(filename, replacement: token)
        }
        return token
    }

    func jobToken(for jobID: UUID?) -> String? {
        guard let jobID else {
            return nil
        }
        if let token = jobTokens[jobID] {
            return token
        }
        let token = String(format: "<job:%@:%03d>", scope, jobTokens.count + 1)
        jobTokens[jobID] = token
        registerExact(jobID.uuidString, replacement: token)
        return token
    }

    func registerSensitiveName(_ name: String, replacement: String? = nil) {
        let normalizedName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard normalizedName.count >= 3 else {
            return
        }
        if exactReplacements.contains(where: { key, _ in
            key.caseInsensitiveCompare(normalizedName) == .orderedSame
        }) {
            return
        }
        if let replacement {
            registerExact(normalizedName, replacement: replacement)
            return
        }
        if let token = nameTokens[normalizedName.lowercased()] {
            registerExact(normalizedName, replacement: token)
            return
        }
        let token = String(format: "<name:%@:%03d>", scope, nameTokens.count + 1)
        nameTokens[normalizedName.lowercased()] = token
        registerExact(normalizedName, replacement: token)
    }

    func redact(_ value: String?) -> String? {
        guard let value else {
            return nil
        }
        return redact(value)
    }

    func redact(_ value: String) -> String {
        var redacted = Self.removingUnsafeControlCharacters(from: value)
        redacted = redactCommandArguments(in: redacted)
        for (sensitiveValue, replacement) in exactReplacements.sorted(by: { $0.key.count > $1.key.count }) {
            redacted = redacted.replacingOccurrences(
                of: sensitiveValue,
                with: replacement,
                options: [.caseInsensitive]
            )
        }
        redacted = replaceCapturedMatches(
            pattern: #"[\"'`](file://(?:/|%2F)[^\"'`\r\n]+|/(?:[^\"'`\r\n]+))[\"'`]"#,
            in: redacted,
            captureGroup: 1
        ) { [weak self] path in
            self?.pathToken(for: path.removingPercentEncoding ?? path) ?? "<path:redacted>"
        }
        redacted = replaceMatches(
            pattern: #"(?i)/Volumes/[^\r\n]+"#,
            in: redacted
        ) { [weak self] path in
            self?.pathToken(for: path) ?? "<path:redacted>"
        }
        redacted = replaceMatches(
            pattern: #"(?i)file:///(?:[^\s\]\[\)\(\{\},;:\"'<>|]+)"#,
            in: redacted
        ) { [weak self] path in
            self?.pathToken(for: path.removingPercentEncoding ?? path) ?? "<path:redacted>"
        }
        redacted = replaceMatches(
            pattern: #"(?<![A-Za-z0-9:/])/(?!/)(?:[^\s\]\[\)\(\{\},;:\"'<>|]+/?)+"#,
            in: redacted
        ) { [weak self] path in
            self?.pathToken(for: path) ?? "<path:redacted>"
        }
        redacted = replaceMatches(
            pattern: #"(?<![A-Za-z0-9])~/(?:[^\s\]\[\)\(\{\},;:\"'<>|]+/?)+"#,
            in: redacted
        ) { [weak self] path in
            self?.pathToken(for: path) ?? "<path:redacted>"
        }
        redacted = replaceMatches(
            pattern: #"(?i)(?<![A-Za-z0-9])[A-Z]:\\(?:[^\s\]\[\)\(\{\},;:\"'<>|]+\\?)+"#,
            in: redacted
        ) { [weak self] path in
            self?.pathToken(for: path) ?? "<path:redacted>"
        }
        redacted = replaceMatches(
            pattern: #"(?i)(?<![A-Za-z0-9_.-])[^\s/\\:\"'<>|]+\.(?:iso|mkv|m2ts|mts|mov|mp4|srt|sup|hevc|h264|json|log)(?![A-Za-z0-9_.-])"#,
            in: redacted
        ) { [weak self] filename in
            guard let self else {
                return "<name:redacted>"
            }
            self.registerSensitiveName(filename)
            return self.nameTokens[filename.lowercased()] ?? "<name:redacted>"
        }
        redacted = replaceCapturedMatches(
            pattern: #"(?i)\b(authorization|api[_-]?key|password|secret|token|serial(?:[_ -]?number)?|device[_-]?id)\b\s*[:=]\s*(?:Bearer\s+[A-Za-z0-9._~+/=-]+|\"[^\"]*\"|'[^']*'|[^\s,;]+)"#,
            in: redacted,
            captureGroup: 1
        ) { key in
            "\(key)=<redacted>"
        }
        redacted = replaceMatches(
            pattern: #"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"#,
            in: redacted
        ) { _ in "Bearer <redacted>" }
        redacted = replaceMatches(
            pattern: #"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"#,
            in: redacted
        ) { _ in "<credential:redacted>" }
        redacted = replaceMatches(
            pattern: #"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"#,
            in: redacted
        ) { _ in "<credential:redacted>" }
        redacted = replaceMatches(
            pattern: #"(?i)\b[0-9a-f]{32,}\b"#,
            in: redacted
        ) { _ in "<identifier:redacted>" }
        redacted = replaceMatches(
            pattern: #"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[1-5][0-9A-Fa-f]{3}-[89ABab][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}\b"#,
            in: redacted
        ) { [weak self] identifier in
            self?.identifierToken(for: identifier) ?? "<identifier:redacted>"
        }
        redacted = replaceMatches(
            pattern: #"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"#,
            in: redacted
        ) { _ in "<email:redacted>" }
        redacted = replaceMatches(
            pattern: #"\b(?:\d{1,3}\.){3}\d{1,3}\b"#,
            in: redacted
        ) { _ in "<network:redacted>" }
        redacted = replaceMatches(
            pattern: #"(?i)\b(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\b"#,
            in: redacted
        ) { _ in "<network:redacted>" }
        return redacted
    }

    private func identifierToken(for identifier: String) -> String {
        let normalized = identifier.lowercased()
        if let token = identifierTokens[normalized] {
            return token
        }
        let token = String(format: "<identifier:%@:%03d>", scope, identifierTokens.count + 1)
        identifierTokens[normalized] = token
        return token
    }

    private func registerExact(_ value: String, replacement: String) {
        let normalizedValue = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard normalizedValue.count >= 3, normalizedValue != replacement else {
            return
        }
        exactReplacements[normalizedValue] = replacement
    }

    private func redactCommandArguments(in text: String) -> String {
        let lines = text.components(separatedBy: "\n")
        return lines.map { line in
            guard let expression = try? NSRegularExpression(
                pattern: #"(?i)(?:^|[\s\"'])(?:/[^\s\"']*/)?(ffmpeg|ffprobe|makemkvcon|mp4box|edge264(?:_test)?|pgsrip|python(?:3(?:\.\d+)?)?|ditto|unzip|zip)(?=\s)"#
            ),
                let match = expression.firstMatch(
                    in: line,
                    range: NSRange(line.startIndex..., in: line)
                ),
                let fullRange = Range(match.range(at: 0), in: line),
                let toolRange = Range(match.range(at: 1), in: line)
            else {
                return line
            }
            let remainder = line[fullRange.upperBound...].trimmingCharacters(in: .whitespaces)
            let normalizedRemainder = remainder.lowercased()
            guard !remainder.isEmpty,
                  !normalizedRemainder.hasPrefix("version "),
                  !normalizedRemainder.hasPrefix("configuration")
            else {
                return line
            }
            var prefix = String(line[..<fullRange.lowerBound])
            if !prefix.isEmpty && !prefix.hasSuffix(" ") {
                prefix += " "
            }
            return "\(prefix)\(line[toolRange]) <arguments:redacted>"
        }
        .joined(separator: "\n")
    }

    private func replaceMatches(
        pattern: String,
        in text: String,
        transform: (String) -> String
    ) -> String {
        replaceCapturedMatches(pattern: pattern, in: text, captureGroup: 0, transform: transform)
    }

    private func replaceCapturedMatches(
        pattern: String,
        in text: String,
        captureGroup: Int,
        transform: (String) -> String
    ) -> String {
        guard let expression = try? NSRegularExpression(pattern: pattern) else {
            return text
        }
        let matches = expression.matches(
            in: text,
            range: NSRange(text.startIndex..., in: text)
        )
        let replacements: [(Range<String.Index>, String)] = matches.compactMap { match in
            guard let replacementRange = Range(match.range(at: 0), in: text),
                  let capturedRange = Range(match.range(at: captureGroup), in: text)
            else {
                return nil
            }
            return (replacementRange, transform(String(text[capturedRange])))
        }
        var result = text
        for (range, replacement) in replacements.reversed() {
            result.replaceSubrange(range, with: replacement)
        }
        return result
    }

    private static func normalizedPath(_ path: String) -> String {
        let decoded = path.removingPercentEncoding ?? path
        if decoded.lowercased().hasPrefix("file://"), let url = URL(string: decoded), url.isFileURL {
            return url.standardizedFileURL.path
        }
        if decoded.hasPrefix("/") || decoded.hasPrefix("~") {
            return NSString(string: decoded).standardizingPath
        }
        return decoded
    }

    private static func removingUnsafeControlCharacters(from text: String) -> String {
        let ansiPattern = #"\u{001B}\[[0-?]*[ -/]*[@-~]"#
        let withoutANSI: String
        if let expression = try? NSRegularExpression(pattern: ansiPattern) {
            withoutANSI = expression.stringByReplacingMatches(
                in: text,
                range: NSRange(text.startIndex..., in: text),
                withTemplate: ""
            )
        } else {
            withoutANSI = text
        }
        return String(
            withoutANSI.unicodeScalars.filter { scalar in
                scalar == "\n" || scalar == "\r" || scalar == "\t" || scalar.value >= 0x20
            }
        )
    }
}

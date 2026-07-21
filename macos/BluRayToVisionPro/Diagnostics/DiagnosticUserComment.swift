import Foundation

/// Normalizes and bounds the optional user-provided "What went wrong?" description
/// before it is redacted and written into an immutable diagnostic bundle.
///
/// Normalization is deliberately conservative and happens before redaction:
/// line endings collapse to `\n`, control characters are removed, the ends are
/// trimmed, and the text is bounded by both a user-visible character count and a
/// UTF-8 byte size that stays well within the archive budget. Empty or
/// whitespace-only input produces `nil` so the bundle omits the field entirely.
struct DiagnosticUserComment: Equatable, Sendable {
    /// Maximum number of user-visible characters retained in the description.
    static let maximumCharacterCount = 2_000

    /// Maximum UTF-8 byte size retained after normalization. This is comfortably
    /// within the 1,500,000-byte uncompressed archive budget even after redaction.
    static let maximumByteCount = 8 * 1_024

    /// Normalized, control-character-free text with `\n` line endings. Never empty.
    let text: String

    /// True when normalization dropped characters to satisfy the character or byte bound.
    let truncated: Bool

    /// Returns a normalized comment, or `nil` when the raw text is empty or whitespace-only.
    static func normalize(_ rawText: String) -> DiagnosticUserComment? {
        let unifiedLineEndings = rawText
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        let withoutControlCharacters = String(
            String.UnicodeScalarView(
                unifiedLineEndings.unicodeScalars.filter { scalar in
                    if scalar == "\n" || scalar == "\t" {
                        return true
                    }
                    let category = scalar.properties.generalCategory
                    return category != .control && category != .format
                }
            )
        )
        let trimmed = withoutControlCharacters.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return nil
        }

        var bounded = trimmed
        var truncated = false
        if bounded.count > maximumCharacterCount {
            bounded = String(bounded.prefix(maximumCharacterCount))
            truncated = true
        }
        let byteBounded = boundedUTF8Prefix(bounded, maximumBytes: maximumByteCount)
        return DiagnosticUserComment(
            text: byteBounded.text,
            truncated: truncated || byteBounded.truncated
        )
    }

    private static func boundedUTF8Prefix(
        _ value: String,
        maximumBytes: Int
    ) -> (text: String, truncated: Bool) {
        let data = Data(value.utf8)
        guard data.count > maximumBytes else {
            return (value, false)
        }
        var count = maximumBytes
        while count > 0 {
            if let decoded = String(data: data.prefix(count), encoding: .utf8) {
                return (decoded, true)
            }
            count -= 1
        }
        return ("", true)
    }
}

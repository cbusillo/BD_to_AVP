import Foundation

/// Builds a safe GitHub "new issue" URL for the public BD_to_AVP repository.
///
/// The draft only ever carries a small allowlist of non-secret fields: a
/// redacted user description, the app version/build, the captured stage, and the
/// public support code that links to the private report without being able to
/// download it. Report IDs, upload/status tokens, private object keys, raw ZIP
/// contents, and local paths are never accepted by this type, so they cannot
/// reach the browser draft. The app opens the URL in the user's browser and
/// never calls the GitHub API or embeds any credential.
struct GitHubIssueDraft: Equatable, Sendable {
    static let repositoryOwner = "cbusillo"
    static let repositoryName = "BD_to_AVP"

    /// Maximum characters retained in the issue title.
    static let maximumTitleCharacterCount = 120

    /// Maximum characters retained in the issue body before URL encoding, keeping
    /// the whole URL comfortably within browser and GitHub length limits.
    static let maximumBodyCharacterCount = 4_000

    /// The only characters left unescaped in the query. Everything else, including
    /// every URL sub-delimiter (`&`, `=`, `+`, …), is percent-encoded so no field
    /// value can inject an extra query parameter.
    private static let queryValueAllowed = CharacterSet(
        charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )

    let supportCode: String
    let appVersion: String
    let appBuild: String
    let capturedStage: String
    let redactedDescription: String?

    var title: String {
        Self.bounded(
            "Beta diagnostics report \(supportCode)",
            maximumCharacterCount: Self.maximumTitleCharacterCount
        )
    }

    var body: String {
        let description = redactedDescription?.trimmingCharacters(in: .whitespacesAndNewlines)
        let descriptionSection = (description?.isEmpty == false)
            ? description!
            : "_No description was provided._"
        let lines = [
            "> GitHub issues are public and stay editable until you submit them.",
            "> Review this text and remove anything you would not share publicly.",
            "",
            "**Support code:** \(supportCode)",
            "**App version:** \(appVersion) (\(appBuild))",
            "**Captured stage:** \(capturedStage)",
            "",
            "## What went wrong?",
            descriptionSection,
        ]
        return Self.bounded(
            lines.joined(separator: "\n"),
            maximumCharacterCount: Self.maximumBodyCharacterCount
        )
    }

    /// Returns the GitHub new-issue URL, or `nil` if it cannot be constructed.
    func url() -> URL? {
        var components = URLComponents()
        components.scheme = "https"
        components.host = "github.com"
        components.path = "/\(Self.repositoryOwner)/\(Self.repositoryName)/issues/new"
        components.percentEncodedQueryItems = [
            URLQueryItem(name: "title", value: Self.encoded(title)),
            URLQueryItem(name: "body", value: Self.encoded(body)),
        ]
        return components.url
    }

    private static func encoded(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: queryValueAllowed) ?? ""
    }

    private static func bounded(_ value: String, maximumCharacterCount: Int) -> String {
        guard value.count > maximumCharacterCount else {
            return value
        }
        let marker = "…"
        let keep = max(0, maximumCharacterCount - marker.count)
        return String(value.prefix(keep)) + marker
    }
}

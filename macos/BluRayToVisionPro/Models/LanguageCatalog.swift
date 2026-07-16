import Foundation

enum LanguageCatalogError: LocalizedError {
    case missingResource
    case invalidResource
    case unsupportedSchema(Int)

    var errorDescription: String? {
        switch self {
        case .missingResource:
            "The bundled language catalog is missing."
        case .invalidResource:
            "The bundled language catalog is invalid."
        case let .unsupportedSchema(version):
            "Language catalog schema version \(version) is not supported."
        }
    }
}

struct LanguageCatalog {
    struct Language: Decodable, Hashable, Identifiable {
        let code: String
        let name: String
        let alpha2: String?
        let bibliographic: String

        var id: String { code }
        var displayName: String { "\(name) (\(code))" }

        var aliasSummary: String {
            [alpha2, bibliographic == code ? nil : bibliographic]
                .compactMap { $0 }
                .joined(separator: " · ")
        }
    }

    static let shared: LanguageCatalog = {
        do {
            return try LanguageCatalog(bundle: .main)
        } catch {
            preconditionFailure("Unable to load the language catalog: \(error.localizedDescription)")
        }
    }()

    private static let commonCodes = [
        "eng", "spa", "fra", "deu", "nld", "zho", "jpn", "por",
        "rus", "ita", "kor", "ara", "pol", "swe", "hin",
    ]
    private static let specialCodes = Set(["mis", "mul", "und", "zxx"])

    let languages: [Language]
    let commonLanguages: [Language]
    private let languagesByCode: [String: Language]
    private let canonicalCodesByAlias: [String: String]

    init(bundle: Bundle) throws {
        guard let url = bundle.url(forResource: "iso639_languages", withExtension: "json") else {
            throw LanguageCatalogError.missingResource
        }
        try self.init(data: Data(contentsOf: url))
    }

    init(data: Data) throws {
        let document: Document
        do {
            document = try JSONDecoder().decode(Document.self, from: data)
        } catch {
            throw LanguageCatalogError.invalidResource
        }
        guard document.schemaVersion == 1 else {
            throw LanguageCatalogError.unsupportedSchema(document.schemaVersion)
        }
        guard !document.languages.isEmpty else {
            throw LanguageCatalogError.invalidResource
        }

        var codes = Set<String>()
        var names = Set<String>()
        var aliases: [String: String] = [:]
        for language in document.languages {
            guard Self.isASCIIAlphaCode(language.code, length: 3),
                  Self.isASCIIAlphaCode(language.bibliographic, length: 3),
                  language.alpha2.map({ Self.isASCIIAlphaCode($0, length: 2) }) ?? true,
                  !language.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  !Self.specialCodes.contains(language.code),
                  codes.insert(language.code).inserted,
                  names.insert(language.name.localizedLowercase).inserted
            else {
                throw LanguageCatalogError.invalidResource
            }

            for alias in Set([language.code, language.alpha2, language.bibliographic].compactMap { $0 }) {
                if let existingCode = aliases[alias], existingCode != language.code {
                    throw LanguageCatalogError.invalidResource
                }
                aliases[alias] = language.code
            }
        }

        let sortedLanguages = document.languages.sorted {
            $0.name.localizedStandardCompare($1.name) == .orderedAscending
        }
        let indexedLanguages = Dictionary(uniqueKeysWithValues: sortedLanguages.map { ($0.code, $0) })
        languages = sortedLanguages
        languagesByCode = indexedLanguages
        canonicalCodesByAlias = aliases
        commonLanguages = Self.commonCodes.compactMap { code in
            indexedLanguages[code]
        }
    }

    func language(matching suppliedCode: String) -> Language? {
        guard let primaryCode = Self.primaryLanguageCode(suppliedCode),
              let canonicalCode = canonicalCodesByAlias[primaryCode]
        else {
            return nil
        }
        return languagesByCode[canonicalCode]
    }

    func language(canonicalCode: String) -> Language? {
        languagesByCode[canonicalCode]
    }

    func search(_ query: String) -> [Language] {
        let normalizedQuery = Self.normalizedSearchText(query)
        guard !normalizedQuery.isEmpty else {
            return languages
        }

        return languages
            .compactMap { language -> (Language, Int)? in
                let name = Self.normalizedSearchText(language.name)
                let aliases = [language.code, language.alpha2, language.bibliographic]
                    .compactMap { $0 }
                    .map(Self.normalizedSearchText)
                let rank: Int
                if aliases.contains(normalizedQuery) {
                    rank = 0
                } else if name.hasPrefix(normalizedQuery) {
                    rank = 1
                } else if aliases.contains(where: { $0.hasPrefix(normalizedQuery) }) {
                    rank = 2
                } else if name.contains(normalizedQuery) {
                    rank = 3
                } else if aliases.contains(where: { $0.contains(normalizedQuery) }) {
                    rank = 4
                } else {
                    return nil
                }
                return (language, rank)
            }
            .sorted { left, right in
                if left.1 != right.1 {
                    return left.1 < right.1
                }
                return left.0.name.localizedStandardCompare(right.0.name) == .orderedAscending
            }
            .map(\.0)
    }

    private static func isASCIIAlphaCode(_ value: String, length: Int) -> Bool {
        value.count == length && value.unicodeScalars.allSatisfy { (97 ... 122).contains($0.value) }
    }

    private static func primaryLanguageCode(_ value: String) -> String? {
        let normalized = value
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
            .replacingOccurrences(of: "_", with: "-")
        let parts = normalized.split(separator: "-", omittingEmptySubsequences: false).map(String.init)
        guard (1 ... 3).contains(parts.count),
              let primaryCode = parts.first,
              (primaryCode.count == 2 || primaryCode.count == 3),
              isASCIIAlphaCode(primaryCode, length: primaryCode.count)
        else {
            return nil
        }
        for suffix in parts.dropFirst() {
            let validSuffix = isASCIIAlphaCode(suffix, length: 2)
                || isASCIIAlphaCode(suffix, length: 4)
                || (suffix.count == 3 && suffix.unicodeScalars.allSatisfy { (48 ... 57).contains($0.value) })
            guard validSuffix else {
                return nil
            }
        }
        return primaryCode
    }

    private static func normalizedSearchText(_ value: String) -> String {
        value
            .folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
    }

    private struct Document: Decodable {
        let schemaVersion: Int
        let languages: [Language]

        enum CodingKeys: String, CodingKey {
            case schemaVersion = "schema_version"
            case languages
        }
    }
}

struct SubtitleLanguage: Codable, Equatable, Hashable, Identifiable {
    let code: String

    var id: String { code }
    var name: String { catalogLanguage.name }
    var alpha2: String? { catalogLanguage.alpha2 }
    var bibliographic: String { catalogLanguage.bibliographic }
    var displayName: String { catalogLanguage.displayName }

    static let english = SubtitleLanguage(canonicalCode: "eng")
    static let spanish = SubtitleLanguage(canonicalCode: "spa")
    static let french = SubtitleLanguage(canonicalCode: "fra")
    static let german = SubtitleLanguage(canonicalCode: "deu")
    static let dutch = SubtitleLanguage(canonicalCode: "nld")
    static let chinese = SubtitleLanguage(canonicalCode: "zho")
    static let japanese = SubtitleLanguage(canonicalCode: "jpn")
    static let portuguese = SubtitleLanguage(canonicalCode: "por")
    static let russian = SubtitleLanguage(canonicalCode: "rus")
    static let italian = SubtitleLanguage(canonicalCode: "ita")
    static let korean = SubtitleLanguage(canonicalCode: "kor")

    init?(code: String, catalog: LanguageCatalog = .shared) {
        guard let language = catalog.language(matching: code) else {
            return nil
        }
        self.code = language.code
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let suppliedCode = try container.decode(String.self)
        guard let language = SubtitleLanguage(code: suppliedCode) else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unsupported subtitle language code: \(suppliedCode)."
            )
        }
        self = language
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(code)
    }

    private init(canonicalCode: String) {
        code = canonicalCode
    }

    private var catalogLanguage: LanguageCatalog.Language {
        guard let language = LanguageCatalog.shared.language(canonicalCode: code) else {
            preconditionFailure("Unsupported canonical subtitle language: \(code)")
        }
        return language
    }
}

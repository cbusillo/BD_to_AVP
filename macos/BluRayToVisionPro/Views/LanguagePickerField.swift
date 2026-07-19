import SwiftUI

struct LanguagePickerField: View {
    @Binding var selection: SubtitleLanguage
    @State private var isPresented = false

    var body: some View {
        LabeledContent("Subtitle language") {
            Button {
                isPresented.toggle()
            } label: {
                HStack(spacing: 6) {
                    Text(selection.displayName)
                        .lineLimit(1)
                    Image(systemName: "chevron.up.chevron.down")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                }
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .accessibilityLabel("Subtitle language: \(selection.displayName)")
            .accessibilityHint("Opens a searchable list of subtitle languages")
            .popover(isPresented: $isPresented, arrowEdge: .trailing) {
                LanguagePickerPopover(selection: $selection, isPresented: $isPresented)
            }
        }
    }
}

private struct LanguagePickerPopover: View {
    @Binding var selection: SubtitleLanguage
    @Binding var isPresented: Bool
    @State private var query = ""
    @State private var highlightedCode: String?
    @FocusState private var searchIsFocused: Bool
    @FocusState private var resultsAreFocused: Bool

    private let catalog = LanguageCatalog.shared

    private var results: [LanguageCatalog.Language] {
        catalog.search(query)
    }

    private var commonCodes: Set<String> {
        Set(catalog.commonLanguages.map(\.code))
    }

    private var highlightedLanguage: LanguageCatalog.Language? {
        highlightedCode.flatMap(catalog.language(canonicalCode:))
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                TextField("Search subtitle languages or code", text: $query)
                    .textFieldStyle(.plain)
                    .focused($searchIsFocused)
                    .accessibilityLabel("Search subtitle languages")
                    .onSubmit {
                        if let highlightedLanguage {
                            select(highlightedLanguage)
                        }
                    }
                    .onKeyPress(.downArrow) {
                        highlightedCode = query.isEmpty ? selection.code : results.first?.code
                        resultsAreFocused = highlightedCode != nil
                        return highlightedCode == nil ? .ignored : .handled
                    }
                if !query.isEmpty {
                    Button {
                        query = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Clear subtitle language search")
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)

            Divider()

            if !query.isEmpty, results.isEmpty {
                ContentUnavailableView(
                    "No Languages Found",
                    systemImage: "magnifyingglass",
                    description: Text("Try a language name or an ISO code such as Dutch, nl, nld, or dut.")
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(selection: $highlightedCode) {
                    if query.isEmpty {
                        Section("Common") {
                            ForEach(catalog.commonLanguages) { language in
                                languageRow(language)
                            }
                        }
                        Section("All Languages") {
                            ForEach(catalog.languages.filter { !commonCodes.contains($0.code) }) { language in
                                languageRow(language)
                            }
                        }
                    } else {
                        ForEach(results) { language in
                            languageRow(language)
                        }
                    }
                }
                .listStyle(.plain)
                .focused($resultsAreFocused)
                .onKeyPress(.return) {
                    guard let highlightedLanguage else {
                        return .ignored
                    }
                    select(highlightedLanguage)
                    return .handled
                }
            }
        }
        .frame(width: 340, height: 390)
        .onAppear {
            highlightedCode = selection.code
            searchIsFocused = true
        }
        .onChange(of: query) { _, newQuery in
            highlightedCode = newQuery.isEmpty ? selection.code : results.first?.code
        }
        .onExitCommand {
            isPresented = false
        }
    }

    private func languageRow(_ language: LanguageCatalog.Language) -> some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(language.name)
                    .foregroundStyle(.primary)
                HStack(spacing: 6) {
                    Text(language.code)
                        .monospaced()
                    if !language.aliasSummary.isEmpty {
                        Text(language.aliasSummary)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Spacer(minLength: 12)
            if language.code == selection.code {
                Image(systemName: "checkmark")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(.tint)
                    .accessibilityHidden(true)
            }
        }
        .contentShape(Rectangle())
        .tag(language.code)
        .onTapGesture {
            select(language)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(language.displayName)
        .accessibilityAddTraits(.isButton)
        .accessibilityAddTraits(language.code == selection.code ? .isSelected : [])
        .accessibilityAction {
            select(language)
        }
    }

    private func select(_ language: LanguageCatalog.Language) {
        guard let selectedLanguage = SubtitleLanguage(code: language.code, catalog: catalog) else {
            assertionFailure("Language catalog returned an unselectable code: \(language.code)")
            isPresented = false
            return
        }
        selection = selectedLanguage
        isPresented = false
    }
}

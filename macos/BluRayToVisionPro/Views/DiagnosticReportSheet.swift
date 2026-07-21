import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct DiagnosticReportSheet: View {
    @ObservedObject var viewModel: DiagnosticReportViewModel

    @Environment(\.dismiss) private var dismiss
    @State private var copiedSupportCode = false
    @State private var isConfirmingDiscard = false
    @FocusState private var isDescriptionFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            content
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)

            Divider()
            actions
        }
        .padding(24)
        .frame(minWidth: 420, idealWidth: 560, maxWidth: 680, minHeight: 360, idealHeight: 560, maxHeight: 720)
        .interactiveDismissDisabled(viewModel.phase.isBusy)
        .alert(
            "Local Diagnostic Copy",
            isPresented: Binding(
                get: { viewModel.exportErrorMessage != nil },
                set: { if !$0 { viewModel.clearExportError() } }
            )
        ) {
            Button("OK", role: .cancel) {
                viewModel.clearExportError()
            }
        } message: {
            Text(viewModel.exportErrorMessage ?? "The local diagnostic copy could not be updated.")
        }
        .confirmationDialog(
            "Discard this local diagnostic copy?",
            isPresented: $isConfirmingDiscard,
            titleVisibility: .visible
        ) {
            Button("Discard Local Copy", role: .destructive) {
                if viewModel.discardLocalCopy() {
                    dismiss()
                }
            }
            Button("Keep Copy", role: .cancel) {}
        } message: {
            Text("Save or share the ZIP first if you may need it later.")
        }
    }

    @ViewBuilder
    private var content: some View {
        switch viewModel.phase {
        case .idle:
            DiagnosticReportHeader(
                systemImage: "stethoscope",
                title: "Prepare Diagnostics",
                message: "Capture a privacy-safe support bundle without stopping the current conversion.",
                color: .accentColor
            )
        case .composing:
            composingContent
        case .capturing:
            captureContent
        case .review:
            reviewContent
        case let .uploading(progress):
            uploadingContent(progress: progress)
        case .cancelling:
            cancellingContent
        case let .success(receipt):
            successContent(receipt)
        case .cancelled:
            fallbackContent(
                title: "Upload Cancelled",
                message: "The conversion was not stopped. The local diagnostic ZIP is still available to retry, save, or share.",
                color: .orange
            )
        case let .failed(failure):
            if failure.stage == .capture {
                DiagnosticReportHeader(
                    systemImage: "exclamationmark.triangle.fill",
                    title: failure.title,
                    message: failure.message,
                    color: .orange
                )
            } else {
                fallbackContent(
                    title: failure.title,
                    message: failure.message,
                    color: failure.kind == .offline ? .orange : .red
                )
            }
        }
    }

    private var composingContent: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                DiagnosticReportHeader(
                    systemImage: "text.bubble",
                    title: "Describe the Problem",
                    message: "Optionally tell us what went wrong. Your note is included with the diagnostics, or you can leave it blank and continue.",
                    color: .accentColor
                )
                DiagnosticNotice(
                    systemImage: "lock.shield",
                    text: "Do not enter passwords or personal information. Your text is redacted for paths, names, and credentials before it is added to the reviewable ZIP.",
                    color: .orange
                )
                VStack(alignment: .leading, spacing: 6) {
                    Text("What went wrong? (optional)")
                        .font(.headline)
                    TextEditor(text: descriptionBinding)
                        .font(.body)
                        .frame(minHeight: 120, maxHeight: 220)
                        .focused($isDescriptionFocused)
                        .overlay(alignment: .topLeading) {
                            if viewModel.userDescription.isEmpty {
                                Text("Describe what happened, where it stopped, and what you expected.")
                                    .foregroundStyle(.secondary)
                                    .padding(.top, 8)
                                    .padding(.leading, 5)
                                    .allowsHitTesting(false)
                                    .accessibilityHidden(true)
                            }
                        }
                        .overlay(
                            RoundedRectangle(cornerRadius: 6)
                                .stroke(Color.secondary.opacity(0.3))
                        )
                        .accessibilityLabel("What went wrong")
                        .accessibilityHint(
                            "Optional description included with the diagnostics. Do not enter passwords or personal information."
                        )
                    HStack {
                        Spacer()
                        Text("\(viewModel.userDescription.count)/\(DiagnosticUserComment.maximumCharacterCount)")
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(descriptionCounterColor)
                            .accessibilityLabel(
                                "\(viewModel.userDescription.count) of \(DiagnosticUserComment.maximumCharacterCount) characters used"
                            )
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task {
            await Task.yield()
            isDescriptionFocused = true
        }
    }

    private var descriptionBinding: Binding<String> {
        Binding(
            get: { viewModel.userDescription },
            set: { newValue in
                let limit = DiagnosticUserComment.maximumCharacterCount
                viewModel.userDescription = newValue.count > limit
                    ? String(newValue.prefix(limit))
                    : newValue
            }
        )
    }

    private var descriptionCounterColor: Color {
        viewModel.userDescription.count >= DiagnosticUserComment.maximumCharacterCount ? .orange : .secondary
    }

    private var captureContent: some View {
        VStack(alignment: .leading, spacing: 18) {
            DiagnosticReportHeader(
                systemImage: "doc.zipper",
                title: "Capturing Diagnostics",
                message: "The app is freezing a bounded snapshot and creating a privacy-safe ZIP. Your conversion keeps running.",
                color: .accentColor
            )
            ProgressView()
                .controlSize(.large)
                .accessibilityLabel("Capturing diagnostic bundle")
            Label("No source media, screenshots, credentials, or raw paths are included.", systemImage: "lock.shield")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var reviewContent: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                DiagnosticReportHeader(
                    systemImage: viewModel.isUploadAvailable ? "paperplane.fill" : "square.and.arrow.down.fill",
                    title: viewModel.isUploadAvailable ? "Review Before Sending" : "Review Before Saving",
                    message: viewModel.isUploadAvailable
                        ? "Review exactly what the diagnostic ZIP includes, then choose Send Diagnostics."
                        : "Online submission is not configured in this build. You can still save or share the complete privacy-safe ZIP.",
                    color: .accentColor
                )

                if !viewModel.isUploadAvailable {
                    DiagnosticNotice(
                        systemImage: "externaldrive.badge.exclamationmark",
                        text: "Local-only mode: no endpoint or credential can be entered in the app.",
                        color: .orange
                    )
                }

                if let preview = viewModel.artifact?.preview {
                    diagnosticSizeSummary(preview)
                    categorySection(
                        title: "Included",
                        systemImage: "checkmark.circle.fill",
                        color: .green,
                        categories: preview.includedCategories
                    )
                    categorySection(
                        title: "Excluded",
                        systemImage: "minus.circle.fill",
                        color: .secondary,
                        categories: preview.excludedCategories
                    )
                    if let description = preview.userDescription {
                        userDescriptionSection(description)
                    }
                    fileSection(preview)
                    if !preview.truncationNotices.isEmpty {
                        truncationSection(preview.truncationNotices)
                    }
                }

                savedCopyNotice
                discardButton
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func uploadingContent(progress: Double) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            DiagnosticReportHeader(
                systemImage: "arrow.up.doc.fill",
                title: "Sending Diagnostics",
                message: "The exact reviewed ZIP is being uploaded over HTTPS. Cancelling this upload never stops the conversion.",
                color: .accentColor
            )
            ProgressView(value: progress, total: 1)
                .progressViewStyle(.linear)
                .accessibilityLabel("Diagnostic upload progress")
                .accessibilityValue("\(Int((progress * 100).rounded())) percent")
            Text("\(Int((progress * 100).rounded()))%")
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
        }
    }

    private var cancellingContent: some View {
        VStack(alignment: .leading, spacing: 18) {
            DiagnosticReportHeader(
                systemImage: "xmark.circle",
                title: "Cancelling Upload",
                message: "Only the diagnostic upload is being cancelled. The conversion continues unchanged.",
                color: .orange
            )
            ProgressView()
                .controlSize(.large)
                .accessibilityLabel("Cancelling diagnostic upload")
        }
    }

    private func successContent(_ receipt: DiagnosticReportReceipt) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                DiagnosticReportHeader(
                    systemImage: "checkmark.circle.fill",
                    title: "Diagnostics Sent",
                    message: "Give this support code to the maintainer helping you. The code identifies the private report but cannot download it.",
                    color: .green
                )

                GroupBox {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Support Code")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                        ViewThatFits(in: .horizontal) {
                            HStack(spacing: 12) {
                                supportCodeText(receipt.supportCode)
                                Spacer(minLength: 8)
                                copySupportCodeButton(receipt.supportCode)
                            }
                            VStack(alignment: .leading, spacing: 10) {
                                supportCodeText(receipt.supportCode)
                                copySupportCodeButton(receipt.supportCode)
                            }
                        }
                        Text("Available until \(receipt.expiresAt.formatted(date: .abbreviated, time: .shortened))")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .accessibilityLabel(
                                "Report expires \(receipt.expiresAt.formatted(date: .complete, time: .shortened))"
                            )
                    }
                    .padding(4)
                }

                gitHubHandoffSection(receipt)

                if viewModel.hasLocalArtifact {
                    DiagnosticNotice(
                        systemImage: "externaldrive.badge.exclamationmark",
                        text: "The report was sent, but the temporary local copy still needs attention.",
                        color: .orange
                    )
                    fallbackExportButtons
                    discardButton
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func fallbackContent(title: String, message: String, color: Color) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                DiagnosticReportHeader(
                    systemImage: "exclamationmark.triangle.fill",
                    title: title,
                    message: message,
                    color: color
                )
                DiagnosticNotice(
                    systemImage: "doc.zipper",
                    text: "The reviewed local ZIP has been kept. Retry creates a fresh report and new upload authorization.",
                    color: .accentColor
                )
                if let preview = viewModel.artifact?.preview {
                    diagnosticSizeSummary(preview)
                }
                savedCopyNotice
                discardButton
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder
    private var actions: some View {
        switch viewModel.phase {
        case .idle:
            HStack {
                Spacer()
                Button("Close") { dismiss() }
            }
        case .composing:
            HStack {
                Button("Cancel") {
                    viewModel.prepareForNewDiagnosticSession()
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)
                Spacer()
                Button {
                    viewModel.beginCapture()
                } label: {
                    Label("Capture Diagnostics", systemImage: "doc.zipper")
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .help("Capture a privacy-safe diagnostic bundle, including your description")
                .accessibilityLabel("Capture diagnostics including your description")
            }
        case .capturing:
            HStack {
                Spacer()
                Button("Cancel Capture", role: .cancel) {
                    viewModel.cancelCapture()
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)
                .help("Cancel only diagnostic capture; the conversion keeps running")
                .accessibilityLabel("Cancel diagnostic capture")
            }
        case .review:
            reviewActions
        case .uploading:
            HStack {
                Spacer()
                Button("Cancel Upload", role: .cancel) {
                    viewModel.cancelUpload()
                }
                .keyboardShortcut(.cancelAction)
                .help("Cancel only the diagnostic upload; the conversion keeps running")
                .accessibilityLabel("Cancel diagnostic upload without stopping conversion")
            }
        case .cancelling:
            EmptyView()
        case .success:
            HStack {
                if !viewModel.hasLocalArtifact {
                    Button("Capture New Diagnostics") {
                        copiedSupportCode = false
                        viewModel.composeNewDiagnostics()
                    }
                    .help("Describe and capture another privacy-safe diagnostic bundle")
                }
                Spacer()
                Button("Done") { dismiss() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
            }
        case .cancelled:
            fallbackActions
        case let .failed(failure):
            if failure.stage == .capture {
                HStack {
                    Button("Close") { dismiss() }
                    Spacer()
                    Button("Try Again") { viewModel.beginCapture() }
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut(.defaultAction)
                }
            } else {
                fallbackActions
            }
        }
    }

    private var reviewActions: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 10) {
                fallbackExportButtons
                Spacer()
                Button("Not Now") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                if viewModel.isUploadAvailable {
                    sendButton
                }
            }
            VStack(alignment: .trailing, spacing: 10) {
                HStack(spacing: 10) {
                    fallbackExportButtons
                    Spacer()
                }
                HStack(spacing: 10) {
                    Spacer()
                    Button("Not Now") { dismiss() }
                        .keyboardShortcut(.cancelAction)
                    if viewModel.isUploadAvailable {
                        sendButton
                    }
                }
            }
        }
    }

    private var fallbackActions: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 10) {
                fallbackExportButtons
                Spacer()
                Button("Close") { dismiss() }
                if viewModel.canRetryUpload {
                    retryButton
                }
            }
            VStack(alignment: .trailing, spacing: 10) {
                HStack(spacing: 10) {
                    fallbackExportButtons
                    Spacer()
                }
                HStack(spacing: 10) {
                    Spacer()
                    Button("Close") { dismiss() }
                    if viewModel.canRetryUpload {
                        retryButton
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var fallbackExportButtons: some View {
        if viewModel.isUploadAvailable {
            saveCopyButton
                .buttonStyle(.bordered)
        } else {
            saveCopyButton
                .buttonStyle(.borderedProminent)
        }

        if let shareURL = viewModel.shareURL {
            ShareLink(item: shareURL) {
                Label("Share…", systemImage: "square.and.arrow.up")
            }
            .help("Open the macOS sharing menu for the diagnostic ZIP")
            .accessibilityLabel("Share the diagnostic ZIP")
        }
    }

    private var saveCopyButton: some View {
        Button {
            showSavePanel()
        } label: {
            Label("Save a Copy…", systemImage: "square.and.arrow.down")
        }
        .help("Save the reviewed diagnostic ZIP to a location you choose")
        .accessibilityLabel("Save a copy of the diagnostic ZIP")
    }

    private var sendButton: some View {
        Button {
            viewModel.uploadCapturedArtifact()
        } label: {
            Label("Send Diagnostics", systemImage: "paperplane.fill")
        }
        .buttonStyle(.borderedProminent)
        .keyboardShortcut(.defaultAction)
        .help("Send the exact reviewed ZIP over HTTPS")
        .accessibilityLabel("Consent and send the reviewed diagnostics")
    }

    private var retryButton: some View {
        Button("Retry Send") {
            viewModel.retryUpload()
        }
        .buttonStyle(.borderedProminent)
        .keyboardShortcut(.defaultAction)
        .help("Create a fresh report authorization and retry the same reviewed ZIP")
        .accessibilityLabel("Retry sending diagnostics with a fresh report")
    }

    private var discardButton: some View {
        Button("Discard Local Copy", role: .destructive) {
            isConfirmingDiscard = true
        }
        .buttonStyle(.plain)
        .font(.caption)
        .help("Permanently remove the temporary diagnostic ZIP")
        .accessibilityLabel("Discard the temporary local diagnostic copy")
    }

    @ViewBuilder
    private var savedCopyNotice: some View {
        if let savedCopyURL = viewModel.lastSavedCopyURL {
            DiagnosticNotice(
                systemImage: "checkmark.circle.fill",
                text: "Saved a copy as \(savedCopyURL.lastPathComponent).",
                color: .green
            )
        }
    }

    private func diagnosticSizeSummary(_ preview: DiagnosticBundlePreview) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                LabeledContent("Compressed ZIP") {
                    Text(Self.byteCount(preview.archiveBytes))
                        .monospacedDigit()
                }
                LabeledContent("Maximum allowed") {
                    Text(Self.byteCount(preview.maximumArchiveBytes))
                        .monospacedDigit()
                }
                ProgressView(
                    value: Double(preview.archiveBytes),
                    total: Double(preview.maximumArchiveBytes)
                )
                .accessibilityLabel("Diagnostic ZIP size")
                .accessibilityValue(
                    "\(Self.byteCount(preview.archiveBytes)) of \(Self.byteCount(preview.maximumArchiveBytes))"
                )
            }
            .padding(4)
        } label: {
            Label("Bundle Size", systemImage: "doc.zipper")
                .font(.headline)
        }
    }

    private func categorySection(
        title: String,
        systemImage: String,
        color: Color,
        categories: [String]
    ) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(categories.enumerated()), id: \.offset) { _, category in
                    Label {
                        Text(category)
                            .fixedSize(horizontal: false, vertical: true)
                    } icon: {
                        Image(systemName: systemImage)
                            .foregroundStyle(color)
                    }
                    .font(.callout)
                    .accessibilityLabel("\(title): \(category)")
                }
            }
            .padding(4)
        } label: {
            Text("\(title) (\(categories.count))")
                .font(.headline)
        }
    }

    private func fileSection(_ preview: DiagnosticBundlePreview) -> some View {
        GroupBox {
            VStack(spacing: 0) {
                ForEach(Array(preview.files.enumerated()), id: \.offset) { index, file in
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        Text(file.name)
                            .font(.callout.monospaced())
                            .lineLimit(1)
                        Spacer(minLength: 8)
                        Text(Self.byteCount(file.uncompressedBytes))
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                        if file.truncated {
                            Text("Truncated")
                                .font(.caption2.weight(.semibold))
                                .foregroundStyle(.orange)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(Color.orange.opacity(0.12), in: Capsule())
                        }
                    }
                    .padding(.vertical, 6)
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(
                        "\(file.name), \(Self.byteCount(file.uncompressedBytes))\(file.truncated ? ", truncated" : "")"
                    )
                    if index < preview.files.count - 1 {
                        Divider()
                    }
                }
            }
            .padding(.horizontal, 4)
        } label: {
            Label("Archive Contents", systemImage: "doc.on.doc")
                .font(.headline)
        }
    }

    private func truncationSection(_ notices: [String]) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(notices.enumerated()), id: \.offset) { _, notice in
                    Label(notice, systemImage: "scissors")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(4)
        } label: {
            Label("Truncation Notices", systemImage: "exclamationmark.circle")
                .font(.headline)
        }
    }

    private func userDescriptionSection(_ description: String) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                Text("This redacted text is included in the ZIP exactly as shown.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Text(description)
                    .font(.callout)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(4)
        } label: {
            Label("Your Description (Included)", systemImage: "text.bubble")
                .font(.headline)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Your description is included: \(description)")
    }

    @ViewBuilder
    private func gitHubHandoffSection(_ receipt: DiagnosticReportReceipt) -> some View {
        if let draft = viewModel.gitHubIssueDraft(), let url = draft.url() {
            VStack(alignment: .leading, spacing: 10) {
                DiagnosticNotice(
                    systemImage: "exclamationmark.bubble",
                    text: "GitHub issues are public and stay editable in your browser before you submit. The draft includes only your redacted description, the app version, the captured stage, and this support code. Do not add passwords or personal information.",
                    color: .orange
                )
                Button {
                    NSWorkspace.shared.open(url)
                } label: {
                    Label("Create GitHub Issue…", systemImage: "arrow.up.forward.square")
                }
                .help("Open a prefilled, public GitHub issue draft in your browser")
                .accessibilityLabel("Create a public GitHub issue draft in your browser")
            }
        }
    }

    private func supportCodeText(_ supportCode: String) -> some View {
        Text(supportCode)
            .font(.title3.monospaced().weight(.semibold))
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .accessibilityLabel("Support code \(supportCode)")
    }

    private func copySupportCodeButton(_ supportCode: String) -> some View {
        Button {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(supportCode, forType: .string)
            copiedSupportCode = true
        } label: {
            Label(copiedSupportCode ? "Copied" : "Copy Code", systemImage: copiedSupportCode ? "checkmark" : "doc.on.doc")
        }
        .help("Copy the support code to the clipboard")
        .accessibilityLabel(copiedSupportCode ? "Support code copied" : "Copy support code")
    }

    private func showSavePanel() {
        guard let artifact = viewModel.artifact else {
            return
        }
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.zip]
        panel.canCreateDirectories = true
        panel.isExtensionHidden = false
        panel.nameFieldStringValue = artifact.suggestedFilename
        panel.title = "Save Diagnostic Copy"
        panel.message = "Save the reviewed privacy-safe diagnostic ZIP."
        panel.begin { response in
            guard response == .OK, let destinationURL = panel.url else {
                return
            }
            Task { @MainActor in
                _ = viewModel.saveCopy(to: destinationURL)
            }
        }
    }

    private static func byteCount(_ bytes: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
    }
}

private struct DiagnosticReportHeader: View {
    let systemImage: String
    let title: String
    let message: String
    let color: Color

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: systemImage)
                .font(.system(size: 34, weight: .semibold))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(color)
                .frame(width: 42)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 5) {
                Text(title)
                    .font(.title2.weight(.semibold))
                Text(message)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

private struct DiagnosticNotice: View {
    let systemImage: String
    let text: String
    let color: Color

    var body: some View {
        Label {
            Text(text)
                .fixedSize(horizontal: false, vertical: true)
        } icon: {
            Image(systemName: systemImage)
                .foregroundStyle(color)
        }
        .font(.callout)
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(color.opacity(0.09), in: RoundedRectangle(cornerRadius: 9))
    }
}

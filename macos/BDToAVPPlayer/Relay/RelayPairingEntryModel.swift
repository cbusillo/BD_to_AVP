import Combine
import Foundation

@MainActor
final class RelayPairingEntryModel: ObservableObject {
    @Published var pairingCode = ""
    @Published private(set) var isSubmitting = false

    func beginSubmission() -> String? {
        let submittedCode = pairingCode
        pairingCode = ""
        guard !submittedCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return nil
        }
        isSubmitting = true
        return submittedCode
    }

    func finishSubmission() {
        isSubmitting = false
    }
}

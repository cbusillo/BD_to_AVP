import Foundation

@MainActor
final class AppWorkCoordinator: UpdateInstallPostponing {
    let conversion: ConversionViewModel
    let preview: PreviewViewModel

    init(conversion: ConversionViewModel, preview: PreviewViewModel) {
        self.conversion = conversion
        self.preview = preview
    }

    var hasActiveWorker: Bool {
        conversion.hasPendingWork || preview.hasActiveWorker
    }

    func stopForQuit() async {
        async let conversionStop: Void = conversion.stopForQuit()
        async let previewStop: Void = preview.stopForQuit()
        _ = await (conversionStop, previewStop)
    }

    func postponeInstallUntilIdle(_ installHandler: @escaping () -> Void) -> Bool {
        let activePostponers: [any UpdateInstallPostponing] = [conversion, preview].filter { postponer in
            switch postponer {
            case let conversion as ConversionViewModel:
                conversion.hasPendingWork
            case let preview as PreviewViewModel:
                preview.hasActiveWorker
            default:
                false
            }
        }
        guard !activePostponers.isEmpty else {
            return false
        }

        var remaining = activePostponers.count
        var didRunHandler = false
        let activityFinished = {
            remaining -= 1
            guard remaining == 0, !didRunHandler else {
                return
            }
            didRunHandler = true
            installHandler()
        }
        for postponer in activePostponers {
            if !postponer.postponeInstallUntilIdle(activityFinished) {
                activityFinished()
            }
        }
        return true
    }
}

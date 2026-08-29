import Foundation
import UserNotifications

struct QueueNotificationRequest: Equatable {
    let identifier: String
    let threadIdentifier: String
    let title: String
    let body: String
}

@MainActor
protocol QueueNotificationDelivering: AnyObject {
    func requestAuthorization() async -> Bool
    func deliver(_ request: QueueNotificationRequest) async
}

final class UserNotificationsQueueNotificationDelivery: QueueNotificationDelivering {
    private let center: UNUserNotificationCenter

    init(center: UNUserNotificationCenter = .current()) {
        self.center = center
    }

    func requestAuthorization() async -> Bool {
        do {
            return try await center.requestAuthorization(options: [.alert, .sound])
        } catch {
            return false
        }
    }

    func deliver(_ request: QueueNotificationRequest) async {
        let content = UNMutableNotificationContent()
        content.title = request.title
        content.body = request.body
        content.threadIdentifier = request.threadIdentifier
        content.sound = .default

        let notification = UNNotificationRequest(
            identifier: request.identifier,
            content: content,
            trigger: nil
        )
        try? await center.add(notification)
    }
}

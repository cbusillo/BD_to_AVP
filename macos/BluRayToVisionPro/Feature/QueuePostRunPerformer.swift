import AppKit
import CoreServices
import Foundation
import IOKit
import IOKit.pwr_mgt

/// Whether macOS lets the app ask for a post-run action.
enum QueuePostRunPermission: Equatable {
    case granted
    /// The person declined, or turned it off in System Settings.
    case denied
    /// macOS would have to ask the person first.
    case notDetermined
    case unavailable
}

enum QueuePostRunPerformError: LocalizedError, Equatable {
    case refusedInTests
    case unsupported
    case systemRefused(Int32)

    var errorDescription: String? {
        switch self {
        case .refusedInTests:
            "Sleep and shut down are never requested while tests run."
        case .unsupported:
            "There is nothing to do for this setting."
        case let .systemRefused(code):
            "macOS refused the request (\(code))."
        }
    }
}

/// Asks macOS to sleep or shut down. Every system power request goes through this, so tests substitute a fake.
@MainActor
protocol QueuePostRunPerforming: AnyObject {
    /// Reports permission without ever showing a prompt.
    func permission(for action: QueuePostRunAction) async -> QueuePostRunPermission
    /// Reports permission, letting macOS ask the person if it has not yet; called only while someone is choosing the setting.
    func requestPermission(for action: QueuePostRunAction) async -> QueuePostRunPermission
    func perform(_ action: QueuePostRunAction) async throws
}

/// Reads how long the Mac has gone without keyboard, mouse, or trackpad input.
enum SystemHIDIdleTime {
    /// Seconds since the last input event, from the IOHIDSystem `HIDIdleTime` property; `nil` when unavailable.
    nonisolated static func current() -> TimeInterval? {
        let service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOHIDSystem"))
        guard service != IO_OBJECT_NULL else {
            return nil
        }
        defer { IOObjectRelease(service) }
        guard let property = IORegistryEntryCreateCFProperty(
            service,
            "HIDIdleTime" as CFString,
            kCFAllocatorDefault,
            0
        )?.takeRetainedValue() else {
            return nil
        }
        let nanoseconds: UInt64
        if let number = property as? NSNumber {
            nanoseconds = number.uint64Value
        } else if let data = property as? Data, data.count >= MemoryLayout<UInt64>.size {
            nanoseconds = data.withUnsafeBytes { $0.loadUnaligned(as: UInt64.self) }
        } else {
            return nil
        }
        return TimeInterval(nanoseconds) / 1_000_000_000
    }
}

/// Sleeps through the power-management API and shuts down by asking loginwindow, as the Apple
/// menu does, so apps with unsaved documents can object.
@MainActor
final class SystemQueuePostRunPerformer: QueuePostRunPerforming {
    private static let loginWindowBundleIdentifier = "com.apple.loginwindow"
    /// `kCoreEventClass` ('aevt') and `kAEShutDown` ('shut').
    private static let coreEventClass: AEEventClass = 0x6165_7674
    private static let shutDownEventID: AEEventID = 0x7368_7574
    /// `typeWildCard` ('****').
    private static let wildCard: DescType = 0x2A2A_2A2A

    private let environment: [String: String]

    init(environment: [String: String] = ProcessInfo.processInfo.environment) {
        self.environment = environment
    }

    func permission(for action: QueuePostRunAction) async -> QueuePostRunPermission {
        await determinePermission(for: action, askingIfNeeded: false)
    }

    func requestPermission(for action: QueuePostRunAction) async -> QueuePostRunPermission {
        await determinePermission(for: action, askingIfNeeded: true)
    }

    func perform(_ action: QueuePostRunAction) async throws {
        // The unit-test host never puts the Mac to sleep or shuts it down, whatever a test configures.
        guard !AppDelegate.isUnitTestHost(environment: environment) else {
            throw QueuePostRunPerformError.refusedInTests
        }
        switch action {
        case .doNothing:
            throw QueuePostRunPerformError.unsupported
        case .sleep:
            let port = IOPMFindPowerManagement(kIOMainPortDefault)
            guard port != IO_OBJECT_NULL else {
                throw QueuePostRunPerformError.systemRefused(-1)
            }
            defer { IOServiceClose(port) }
            let result = IOPMSleepSystem(port)
            guard result == kIOReturnSuccess else {
                throw QueuePostRunPerformError.systemRefused(result)
            }
        case .shutDown:
            let event = NSAppleEventDescriptor(
                eventClass: Self.coreEventClass,
                eventID: Self.shutDownEventID,
                targetDescriptor: NSAppleEventDescriptor(bundleIdentifier: Self.loginWindowBundleIdentifier),
                returnID: AEReturnID(kAutoGenerateReturnID),
                transactionID: AETransactionID(kAnyTransactionID)
            )
            do {
                _ = try event.sendEvent(options: [.noReply], timeout: 10)
            } catch {
                throw QueuePostRunPerformError.systemRefused(Int32((error as NSError).code))
            }
        }
    }

    private func determinePermission(for action: QueuePostRunAction, askingIfNeeded: Bool) async -> QueuePostRunPermission {
        switch action {
        case .doNothing:
            return .unavailable
        case .sleep:
            // Sleep goes through power management, which needs no permission.
            return .granted
        case .shutDown:
            let wildCard = Self.wildCard
            let bundleIdentifier = Self.loginWindowBundleIdentifier
            // Asking can wait for the person to answer macOS's prompt, so it runs off the main actor.
            let status = await Task.detached(priority: .userInitiated) { () -> OSStatus in
                let target = NSAppleEventDescriptor(bundleIdentifier: bundleIdentifier)
                guard let descriptor = target.aeDesc else {
                    return OSStatus(procNotFound)
                }
                return AEDeterminePermissionToAutomateTarget(descriptor, wildCard, wildCard, askingIfNeeded)
            }.value
            switch status {
            case noErr:
                return .granted
            case OSStatus(errAEEventNotPermitted):
                return .denied
            case OSStatus(errAEEventWouldRequireUserConsent):
                return .notDetermined
            default:
                return .unavailable
            }
        }
    }
}

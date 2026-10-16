import Foundation
import IOKit.ps

/// Reports the Mac's power source and thermal state, and when either changes.
@MainActor
protocol QueueMachineConditionsProviding: AnyObject {
    var current: QueueMachineConditions { get }
    /// Calls `onChange` on the main actor after the power source or thermal state changes.
    func startObserving(_ onChange: @escaping @MainActor () -> Void)
    func stopObserving()
}

/// Reads the providing power source from IOKit and the thermal state from `ProcessInfo`.
@MainActor
final class SystemQueueMachineConditions: QueueMachineConditionsProviding {
    private var onChange: (@MainActor () -> Void)?
    private var powerSource: CFRunLoopSource?
    private var thermalObserver: NSObjectProtocol?

    var current: QueueMachineConditions {
        QueueMachineConditions(
            isOnBatteryPower: Self.isOnBatteryPower(),
            thermalState: ProcessInfo.processInfo.thermalState
        )
    }

    /// A Mac without a battery reports AC power; a UPS counts as battery power.
    nonisolated static func isOnBatteryPower() -> Bool {
        guard let snapshot = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
              let type = IOPSGetProvidingPowerSourceType(snapshot)?.takeUnretainedValue()
        else {
            return false
        }
        return (type as String) != kIOPSACPowerValue
    }

    func startObserving(_ onChange: @escaping @MainActor () -> Void) {
        stopObserving()
        self.onChange = onChange
        let context = Unmanaged.passUnretained(self).toOpaque()
        if let source = IOPSNotificationCreateRunLoopSource({ context in
            guard let context else {
                return
            }
            let monitor = Unmanaged<SystemQueueMachineConditions>.fromOpaque(context).takeUnretainedValue()
            MainActor.assumeIsolated {
                monitor.onChange?()
            }
        }, context)?.takeRetainedValue() {
            CFRunLoopAddSource(CFRunLoopGetMain(), source, .defaultMode)
            powerSource = source
        }
        thermalObserver = NotificationCenter.default.addObserver(
            forName: ProcessInfo.thermalStateDidChangeNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.onChange?()
            }
        }
    }

    func stopObserving() {
        if let powerSource {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), powerSource, .defaultMode)
            self.powerSource = nil
        }
        if let thermalObserver {
            NotificationCenter.default.removeObserver(thermalObserver)
            self.thermalObserver = nil
        }
        onChange = nil
    }
}

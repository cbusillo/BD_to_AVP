import Foundation
import IOKit.ps
import XCTest
@testable import BluRayToVisionPro

final class QueueMachineConditionsMonitorTests: XCTestCase {
    @MainActor
    func testPowerAndThermalDeliveryStopAndRestartTogether() throws {
        let power = PowerNotificationFixture()
        let monitor = SystemQueueMachineConditions(createPowerSource: power.makeSource)
        var firstChanges = 0
        monitor.startObserving { firstChanges += 1 }
        let firstSource = try XCTUnwrap(power.source)
        power.deliver()
        XCTAssertEqual(firstChanges, 1)
        NotificationCenter.default.post(name: ProcessInfo.thermalStateDidChangeNotification, object: nil)
        XCTAssertEqual(firstChanges, 2)

        // Retiring a registration must discard an already pending power event too.
        CFRunLoopSourceSignal(firstSource)
        monitor.stopObserving()
        monitor.stopObserving()
        XCTAssertFalse(CFRunLoopSourceIsValid(firstSource))
        XCTAssertFalse(CFRunLoopContainsSource(CFRunLoopGetMain(), firstSource, .defaultMode))
        CFRunLoopRunInMode(.defaultMode, 0.01, false)
        NotificationCenter.default.post(name: ProcessInfo.thermalStateDidChangeNotification, object: nil)
        XCTAssertEqual(firstChanges, 2)

        var restartedChanges = 0
        monitor.startObserving { restartedChanges += 1 }
        power.deliver()
        NotificationCenter.default.post(name: ProcessInfo.thermalStateDidChangeNotification, object: nil)
        XCTAssertEqual(restartedChanges, 2)
        XCTAssertEqual(firstChanges, 2)
        monitor.stopObserving()
    }

    @MainActor
    func testDroppingMonitorRetiresPendingPowerAndThermalRegistrations() throws {
        let power = PowerNotificationFixture()
        var monitor: SystemQueueMachineConditions? = SystemQueueMachineConditions(createPowerSource: power.makeSource)
        var changes = 0
        monitor!.startObserving { changes += 1 }
        let source = try XCTUnwrap(power.source)
        weak var released = monitor
        CFRunLoopSourceSignal(source)
        monitor = nil
        XCTAssertNil(released)
        XCTAssertFalse(CFRunLoopSourceIsValid(source))
        XCTAssertFalse(CFRunLoopContainsSource(CFRunLoopGetMain(), source, .defaultMode))
        CFRunLoopRunInMode(.defaultMode, 0.01, false)
        NotificationCenter.default.post(name: ProcessInfo.thermalStateDidChangeNotification, object: nil)
        XCTAssertEqual(changes, 0)
    }

    @MainActor
    func testRealIOPSSourceIsInvalidatedWhenMonitorIsDropped() throws {
        var source: CFRunLoopSource?
        var monitor: SystemQueueMachineConditions? = SystemQueueMachineConditions(createPowerSource: { callback, context in
            source = IOPSNotificationCreateRunLoopSource(callback, context)?.takeRetainedValue()
            return source
        })
        monitor!.startObserving {}
        let registration = try XCTUnwrap(source)
        XCTAssertTrue(CFRunLoopContainsSource(CFRunLoopGetMain(), registration, .defaultMode))
        monitor = nil
        XCTAssertFalse(CFRunLoopSourceIsValid(registration))
        XCTAssertFalse(CFRunLoopContainsSource(CFRunLoopGetMain(), registration, .defaultMode))
    }
}

/// Drives the production C callback through a real run-loop source without changing system power.
@MainActor
private final class PowerNotificationFixture {
    private(set) var source: CFRunLoopSource?
    private var callback: IOPowerSourceCallbackType?
    private var callbackContext: UnsafeMutableRawPointer?

    func makeSource(callback: @escaping IOPowerSourceCallbackType, context: UnsafeMutableRawPointer) -> CFRunLoopSource? {
        self.callback = callback
        callbackContext = context
        var sourceContext = CFRunLoopSourceContext(
            version: 0,
            info: Unmanaged.passUnretained(self).toOpaque(),
            retain: nil, release: nil, copyDescription: nil, equal: nil, hash: nil,
            schedule: nil, cancel: nil,
            perform: { info in
                MainActor.assumeIsolated {
                    let fixture = Unmanaged<PowerNotificationFixture>.fromOpaque(info!).takeUnretainedValue()
                    fixture.callback?(fixture.callbackContext)
                }
            }
        )
        source = CFRunLoopSourceCreate(nil, 0, &sourceContext)
        return source
    }

    func deliver() {
        CFRunLoopSourceSignal(source!)
        CFRunLoopRunInMode(.defaultMode, 0.01, false)
    }
}

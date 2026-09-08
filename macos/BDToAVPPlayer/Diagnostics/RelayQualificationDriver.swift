#if BD_TO_AVP_QUALIFICATION
import AVFoundation
import CoreVideo
import Foundation

/// Opt-in device qualification through the production coordinator. Mac approval
/// still requires comparing the emitted code with the visible host code.
@MainActor
enum RelayQualificationDriver {
    static func runIfRequested(coordinator: RelaySessionCoordinator, player: MVHEVCPlayerSession) async {
        guard let serverName = ProcessInfo.processInfo.environment["BD_TO_AVP_RELAY_QUALIFICATION_SERVER"],
              !serverName.isEmpty else { return }
        emit("discovery_started")
        coordinator.startDiscovery()
        do {
            try await wait(seconds: 15) { !coordinator.discoveredServers.isEmpty || coordinator.state != .discovery }
            let matches = coordinator.discoveredServers.filter { $0.displayName == serverName }
            guard matches.count == 1, let endpoint = matches.first else {
                emit("discovery_failed state=\(coordinator.state) matching_servers=\(matches.count)")
                return
            }
            await coordinator.connect(to: endpoint)
            guard let code = coordinator.shortAuthenticationString else {
                emit("pairing_failed state=\(coordinator.state)")
                return
            }
            emit("comparison_code=\(code.digits)")
            await coordinator.confirmCodesMatch()
            emit("waiting_for_mac state=\(coordinator.state)")
            try await wait(seconds: 240) {
                if case .confirming = coordinator.state { return false }
                return true
            }
            guard let configuration = coordinator.remotePlaybackConfiguration() else {
                emit("confirmation_failed state=\(coordinator.state)")
                return
            }
            emit("paired")
            let preparationStarted = ContinuousClock.now
            await player.prepareRelayPlayback(configuration)
            emitPlayerDetails(player)
            do {
                try await wait(seconds: 30) { player.state == .ready || player.state == .failed }
            } catch {
                emit("startup_target_met=false target_seconds=30")
                // Continue observation without relaxing the acceptance target.
                try await wait(seconds: 90) { player.state == .ready || player.state == .failed }
            }
            emit("preparation_elapsed=\(preparationStarted.duration(to: .now))")
            emit("player_state=\(player.state) error=\(player.failureMessage ?? "none")")
            guard player.state == .ready else { return }
            let video = AVPlayerItemVideoOutput(pixelBufferAttributes: [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            ])
            let item = player.player.currentItem
            item?.add(video)
            defer { item?.remove(video) }
            var decodedFrames = 0
            for _ in 0 ..< 10 {
                try await Task.sleep(for: .seconds(1))
                let time = player.player.currentTime()
                if video.hasNewPixelBuffer(forItemTime: time),
                   video.copyPixelBuffer(forItemTime: time, itemTimeForDisplay: nil) != nil {
                    decodedFrames += 1
                }
                emit("playback_time=\(player.currentTime) duration=\(player.duration) state=\(player.state)")
            }
            emit("decoded_frame_samples=\(decodedFrames)")
        } catch {
            emitPlayerDetails(player)
            emit("stopped error=\(error.localizedDescription) pairing_state=\(coordinator.state) player_state=\(player.state)")
        }
    }

    private static func emitPlayerDetails(_ player: MVHEVCPlayerSession) {
        let item = player.player.currentItem
        emit("item_present=\(item != nil) item_status=\(item?.status.rawValue ?? -1) player_status=\(player.player.status.rawValue) waiting=\(player.player.reasonForWaitingToPlay?.rawValue ?? "none")")
        emit("item_error=\(String(describing: item?.error)) player_error=\(String(describing: player.player.error))")
        for event in item?.errorLog()?.events ?? [] {
            emit("media_error status=\(event.errorStatusCode) comment=\(event.errorComment ?? "none")")
        }
    }

    private static func wait(seconds: Int, until condition: () -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(seconds))
        while !condition() {
            guard ContinuousClock.now < deadline else { throw URLError(.timedOut) }
            try await Task.sleep(for: .milliseconds(100))
        }
    }

    private static func emit(_ message: String) {
        FileHandle.standardOutput.write(Data("RELAY_QUALIFICATION \(message)\n".utf8))
    }
}
#endif

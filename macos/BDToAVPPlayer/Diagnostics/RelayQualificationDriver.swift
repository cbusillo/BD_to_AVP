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
            if ProcessInfo.processInfo.environment["BD_TO_AVP_RELAY_TRANSFER_PROBE"] == "1" {
                try await probeTransfers(configuration)
                return
            }
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
            emit("readiness_target_met=\(preparationStarted.duration(to: .now) <= .seconds(30)) target_seconds=30")
            emit("player_state=\(player.state) error=\(player.failureMessage ?? "none")")
            guard player.state == .ready else { return }
            let video = AVPlayerItemVideoOutput(pixelBufferAttributes: [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            ])
            let item = player.player.currentItem
            item?.add(video)
            defer { item?.remove(video) }
            var decodedFrames = 0
            var decodedIntervals = Set<Int>()
            var latestDecodedTime: Double = -.infinity
            var loggedSecond = -1
            let fixtureDuration = player.duration
            guard fixtureDuration.isFinite, fixtureDuration >= 2, fixtureDuration <= 30 else {
                throw qualificationFailure("Decoded fixture probe requires a finalized fixture between two and thirty seconds.")
            }
            let observationDeadline = ContinuousClock.now.advanced(by: .seconds(fixtureDuration + 10))
            while ContinuousClock.now < observationDeadline {
                try await Task.sleep(for: .milliseconds(50))
                guard player.state == .ready else { throw qualificationFailure("Player left ready state during fixture observation.") }
                let time = player.player.currentTime()
                var displayTime = CMTime.invalid
                if video.hasNewPixelBuffer(forItemTime: time),
                   video.copyPixelBuffer(forItemTime: time, itemTimeForDisplay: &displayTime) != nil,
                   displayTime.seconds.isFinite {
                    if decodedFrames == 0 {
                        emit("first_decoded_frame_elapsed=\(preparationStarted.duration(to: .now)) startup_target_met=\(preparationStarted.duration(to: .now) <= .seconds(30))")
                    }
                    decodedFrames += 1
                    latestDecodedTime = displayTime.seconds
                    decodedIntervals.insert(Int(displayTime.seconds / 2))
                }
                if time.seconds.isFinite, Int(time.seconds) != loggedSecond {
                    loggedSecond = Int(time.seconds)
                    emit("playback_time=\(time.seconds) duration=\(fixtureDuration) state=\(player.state)")
                }
                if time.seconds >= fixtureDuration - 0.1 { break }
            }
            emit("decoded_frame_samples=\(decodedFrames)")
            let requiredIntervals = Set(0..<Int(ceil(fixtureDuration / 2)))
            guard latestDecodedTime >= fixtureDuration - 0.25,
                  decodedIntervals.isSuperset(of: requiredIntervals) else {
                throw qualificationFailure("Missing decoded samples across the fixture timeline or near its end.")
            }
            emit("decoded_timeline_passed intervals=\(requiredIntervals.count) final_sample_time=\(latestDecodedTime)")
            if ProcessInfo.processInfo.environment["BD_TO_AVP_RELAY_CONTROL_PROBE"] == "1" {
                try await probeControls(coordinator: coordinator, player: player)
            }
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

    private static func probeControls(coordinator: RelaySessionCoordinator, player: MVHEVCPlayerSession) async throws {
        guard player.duration >= 5, let originalSession = coordinator.remotePlaybackConfiguration()?.session.sessionID else {
            throw qualificationFailure("Control probe requires a paired fixture at least five seconds long.")
        }
        player.pause()
        player.seek(to: 2)
        try await wait(seconds: 5) { abs(player.player.currentTime().seconds - 2) < 0.15 }
        let pausedAt = player.player.currentTime().seconds
        try await Task.sleep(for: .seconds(1))
        let drift = abs(player.player.currentTime().seconds - pausedAt)
        guard drift < 0.1, player.player.timeControlStatus == .paused else {
            throw qualificationFailure("Pause did not hold the playback clock.")
        }
        emit("pause_passed drift_seconds=\(drift)")
        player.seek(to: 1)
        try await wait(seconds: 5) { abs(player.player.currentTime().seconds - 1) < 0.15 }
        guard player.relaySeekNotice == nil else { throw qualificationFailure("Retained seek was rejected.") }
        emit("retained_backward_seek_passed from=\(pausedAt) to=\(player.player.currentTime().seconds)")
        player.play()
        try await wait(seconds: 5) { player.player.currentTime().seconds >= 2 && player.isPlaying }
        emit("resume_passed playback_time=\(player.player.currentTime().seconds)")
        player.pause()

        // Inject only the path notification, then verify a real authenticated
        // network round trip. This does not claim physical Wi-Fi-loss coverage.
        coordinator.handleNetworkAvailability(.unavailable)
        guard coordinator.state == .networkUnavailable else { throw qualificationFailure("Network-loss state was not entered.") }
        coordinator.handleNetworkAvailability(.available)
        try await wait(seconds: 10) {
            if case .connected = coordinator.state { return true }
            return false
        }
        guard coordinator.remotePlaybackConfiguration()?.session.sessionID == originalSession else {
            throw qualificationFailure("Reconnect replaced the paired session.")
        }
        emit("same_session_reconnect_passed trigger=simulated_path_event transport=real_authenticated_request")

        let loopbackURL = (player.player.currentItem?.asset as? AVURLAsset)?.url
        player.finish()
        coordinator.disconnect()
        guard player.state == .idle, player.player.currentItem == nil,
              coordinator.state == .idle, coordinator.remotePlaybackConfiguration() == nil,
              let loopbackURL else { throw qualificationFailure("Playback cleanup left active state.") }
        var request = URLRequest(url: loopbackURL)
        request.timeoutInterval = 2
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        do {
            _ = try await session.data(for: request)
        } catch {
            emit("finish_cleanup_passed loopback_unreachable=true player_item_removed=true session_cleared=true")
            emit("control_probe_complete")
            return
        }
        throw qualificationFailure("Loopback listener remained reachable after finish.")
    }

    private static func qualificationFailure(_ message: String) -> NSError {
        NSError(domain: "RelayQualification", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }

    private static func probeTransfers(_ configuration: RelayRemotePlaybackConfiguration) async throws {
        var source = try RelayRemotePlaybackSource(
            session: configuration.session, serverBaseURL: configuration.serverBaseURL
        )
        try await source.refreshRetainedWindow(transport: configuration.transport)
        let client = RelayAuthenticatedResourceClient(
            signer: configuration.session, transport: configuration.transport,
            serverBaseURL: configuration.serverBaseURL, maximumTransientRetries: 0
        )
        for segment in source.retainedSeekPolicy.window?.segments.prefix(3) ?? [] {
            let started = ContinuousClock.now
            let result = try await client.load(source.resolveSegmentURL(for: segment))
            emit("direct_transfer resource=\(segment.resourceIdentifier) bytes=\(result.0.count) elapsed=\(started.duration(to: .now))")
        }
        emit("direct_transfer_complete")
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

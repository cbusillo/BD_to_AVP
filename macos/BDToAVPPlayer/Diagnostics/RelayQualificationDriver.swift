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
            guard let item = player.player.currentItem else {
                throw qualificationFailure("Ready player has no fixture item.")
            }
            item.add(video)
            defer { item.remove(video) }
            var decodedFrames = 0
            var decodedIntervals = Set<Int>()
            var latestDecodedTime: Double = -.infinity
            var loggedSecond = -1
            // The published UI duration follows the one-second retained-window
            // refresh. Use the prepared HLS item, whose timeline is authoritative
            // for these decoded display timestamps, rather than racing that UI.
            try await wait(seconds: 5) { item.duration.seconds.isFinite && item.duration.seconds >= 2 }
            let fixtureDuration = item.duration.seconds
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
                    if decodedFrames == 1,
                       ProcessInfo.processInfo.environment["BD_TO_AVP_RELAY_INTERRUPTION_PROBE"] == "1",
                       let loopbackURL = (item.asset as? AVURLAsset)?.url {
                        emit("waiting_for_external_interruption")
                        try await wait(seconds: 60) { player.state == .failed }
                        guard player.player.currentItem == nil, player.playerItem == nil,
                              player.failureMessage != nil, player.isRelayPlayback else {
                            throw qualificationFailure("Interruption did not release the player item and preserve relay failure context.")
                        }
                        try await requireUnreachable(loopbackURL)
                        emit("interruption_cleanup_passed loopback_unreachable=true player_item_removed=true failure_visible=true relay_retry_identity=true coordinator_state=\(coordinator.state)")
                        return
                    }
                }
                if time.seconds.isFinite, Int(time.seconds) != loggedSecond {
                    loggedSecond = Int(time.seconds)
                    emit("playback_time=\(time.seconds) duration=\(fixtureDuration) state=\(player.state) time_control=\(player.player.timeControlStatus.rawValue) waiting_reason=\(player.player.reasonForWaitingToPlay?.rawValue ?? "none")")
                }
                if time.seconds >= fixtureDuration - 0.1 { break }
            }
            emit("decoded_frame_samples=\(decodedFrames)")
            // Ignore a sub-250 ms container/audio tail when counting video
            // intervals, matching the independently enforced final-frame gate.
            let requiredIntervals = Set(0...Int(floor((fixtureDuration - 0.25) / 2)))
            guard latestDecodedTime >= fixtureDuration - 0.25,
                  decodedIntervals.isSuperset(of: requiredIntervals) else {
                emit("decoded_timeline_passed=false final_sample_time=\(latestDecodedTime)")
                throw qualificationFailure("Missing decoded samples across the fixture timeline or near its end.")
            }
            emit("decoded_timeline_passed=true intervals=\(requiredIntervals.count) final_sample_time=\(latestDecodedTime)")
            if ProcessInfo.processInfo.environment["BD_TO_AVP_RELAY_CONTROL_PROBE"] == "1" {
                try await probeControls(coordinator: coordinator, player: player)
            }
        } catch {
            emitPlayerDetails(player)
            emit("stopped error=\(error.localizedDescription) pairing_state=\(coordinator.state) player_state=\(player.state)")
        }
    }

    // MARK: - Live ISO playback through Mac Movies

    /// Live qualification: `BD_TO_AVP_LIVE_QUALIFICATION_MAC` names the Mac (its Bonjour name),
    /// `BD_TO_AVP_LIVE_QUALIFICATION_TITLE` the ISO file name or title. It connects with the
    /// remembered pairing (the Mac auto-approves a known device), plays the title with its
    /// default audio, and scripts play, pause and resume, a retained backward seek, an evicted
    /// seek, and Done. `BD_TO_AVP_LIVE_QUALIFICATION_PLAY_SECONDS` overrides the 600 s play.
    static func runLiveIfRequested(library: MovieLibraryModel, player: MVHEVCPlayerSession) async {
        let environment = ProcessInfo.processInfo.environment
        guard let macName = environment["BD_TO_AVP_LIVE_QUALIFICATION_MAC"], !macName.isEmpty,
              let titleName = environment["BD_TO_AVP_LIVE_QUALIFICATION_TITLE"], !titleName.isEmpty else { return }
        let playSeconds = environment["BD_TO_AVP_LIVE_QUALIFICATION_PLAY_SECONDS"].flatMap(Double.init) ?? 600
        let started = ContinuousClock.now
        func liveEmit(_ message: String) {
            let elapsed = started.duration(to: .now)
            let seconds = Double(elapsed.components.seconds) + Double(elapsed.components.attoseconds) / 1e18
            emit("LIVE t=\(String(format: "%.3f", seconds)) \(message)")
        }
        do {
            liveEmit("discovery_started mac=\(macName)")
            library.discover()
            try await wait(seconds: 30) { library.endpoints.contains { $0.displayName == macName } }
            guard let endpoint = library.endpoints.first(where: { $0.displayName == macName }) else { return }
            library.connect(endpoint)
            // A remembered pairing still shows its prompt until the Mac's approval and the catalog
            // arrive, so only an unremembered prompt stops this wait.
            try await wait(seconds: 60) {
                library.catalog != nil || library.prompt?.isRemembered == false || library.errorMessage != nil
            }
            if let prompt = library.prompt, !prompt.isRemembered {
                liveEmit("comparison_code=\(prompt.code) remembered=false")
                library.confirm()
                try await wait(seconds: 240) { library.catalog != nil || library.errorMessage != nil }
            }
            guard library.catalog != nil else {
                liveEmit("connect_failed error=\(library.errorMessage ?? "none")")
                return
            }
            liveEmit("connected live_titles=\(library.liveTitles.count)")
            func matchingTitle() -> SharedLiveTitle? {
                library.liveTitles.first { $0.fileName == titleName || $0.title == titleName }
            }
            try await wait(seconds: 150) {
                guard let title = matchingTitle() else { return false }
                return title.state != .checking
            }
            guard let title = matchingTitle(), title.state == .eligible else {
                liveEmit("title_unavailable state=\(matchingTitle()?.state.rawValue ?? "missing") reason=\(matchingTitle()?.reason ?? "none")")
                return
            }
            liveEmit("title_eligible duration=\(title.durationSeconds ?? -1) audio_pid=\(library.startingAudio(for: title)?.pid ?? -1) audio=\(library.startingAudio(for: title)?.label ?? "none")")
            liveEmit("live_start_sent")
            library.startLive(title) { configuration, context in
                liveEmit("status_live stream=\(context.streamID.uuidString.lowercased())")
                let outcome = await player.prepareRelayPlayback(configuration, live: context)
                liveEmit("preparation_outcome=\(outcome)")
                return outcome
            }
            try await wait(seconds: 150) {
                player.state == .ready || player.state == .failed
                    || (library.startingLiveTitle == nil && library.errorMessage != nil)
            }
            guard player.state == .ready, let item = player.player.currentItem else {
                liveEmit("start_failed player_state=\(player.state) error=\(player.failureMessage ?? library.errorMessage ?? "none")")
                return
            }
            liveEmit("player_ready forward_buffer=\(item.preferredForwardBufferDuration)")
            let video = AVPlayerItemVideoOutput(pixelBufferAttributes: [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            ])
            item.add(video)
            defer { item.remove(video) }
            try await wait(seconds: 60) {
                let time = player.player.currentTime()
                return video.hasNewPixelBuffer(forItemTime: time)
                    && video.copyPixelBuffer(forItemTime: time, itemTimeForDisplay: nil) != nil
            }
            liveEmit("first_frame playback_time=\(player.player.currentTime().seconds)")

            var rebufferStart: ContinuousClock.Instant?
            var rebuffers = 0
            var longestRebuffer: Duration = .zero
            func observe(for seconds: Double, label: String) async throws {
                let deadline = ContinuousClock.now.advanced(by: .milliseconds(Int64(seconds * 1_000)))
                var lastSample = ContinuousClock.now
                while ContinuousClock.now < deadline {
                    try await Task.sleep(for: .milliseconds(100))
                    guard player.state == .ready else {
                        throw qualificationFailure("Live playback left ready: \(player.failureMessage ?? "none")")
                    }
                    let waiting = player.player.timeControlStatus == .waitingToPlayAtSpecifiedRate
                    if waiting, rebufferStart == nil {
                        rebufferStart = .now
                        liveEmit("rebuffer_start playback_time=\(player.player.currentTime().seconds) reason=\(player.player.reasonForWaitingToPlay?.rawValue ?? "none")")
                    } else if !waiting, let began = rebufferStart {
                        let length = began.duration(to: .now)
                        rebuffers += 1
                        longestRebuffer = max(longestRebuffer, length)
                        rebufferStart = nil
                        liveEmit("rebuffer_end duration=\(length)")
                    }
                    if lastSample.duration(to: .now) >= .seconds(10) {
                        lastSample = .now
                        let time = player.player.currentTime().seconds
                        liveEmit("sample phase=\(label) playback_time=\(time) latest_available=\(player.duration) lead=\(player.duration - time) earliest=\(player.earliestSeekTime) notice=\(player.liveNotice ?? "none")")
                    }
                }
            }
            try await observe(for: playSeconds, label: "play")
            liveEmit("play_complete rebuffers=\(rebuffers) longest_rebuffer=\(longestRebuffer)")

            player.pause()
            let pausedAt = player.player.currentTime().seconds
            try await Task.sleep(for: .seconds(5))
            liveEmit("pause_result drift=\(abs(player.player.currentTime().seconds - pausedAt)) status=\(player.player.timeControlStatus.rawValue)")
            player.play()
            try await wait(seconds: 10) { player.player.timeControlStatus == .playing }
            liveEmit("resume_result playback_time=\(player.player.currentTime().seconds)")
            try await observe(for: 10, label: "resume")

            let backwardTarget = max(player.earliestSeekTime + 5, player.player.currentTime().seconds - 20)
            player.seek(to: backwardTarget)
            try await wait(seconds: 10) { abs(player.player.currentTime().seconds - backwardTarget) < 1 }
            liveEmit("seek_backward_result target=\(backwardTarget) position=\(player.player.currentTime().seconds) notice=\(player.relaySeekNotice ?? "none")")
            try await observe(for: 10, label: "after_backward_seek")

            let earliest = player.earliestSeekTime
            if earliest > 1 {
                player.seek(to: 0)
                try await wait(seconds: 10) { player.relaySeekNotice != nil }
                try await Task.sleep(for: .seconds(2))
                liveEmit("seek_evicted_result target=0 earliest_playable=\(earliest) position=\(player.player.currentTime().seconds) notice=\(player.relaySeekNotice ?? "none")")
            } else {
                liveEmit("seek_evicted_result skipped=true earliest_playable=\(earliest)")
            }
            try await observe(for: 10, label: "after_evicted_seek")

            let cleanup = library.livePlayback?.stop()
            player.finish()
            library.finishLivePlayback()
            let result = await cleanup?.value
            liveEmit("cancel_acknowledged result=\(result.map { "\($0)" } ?? "none")")
            liveEmit("live_qualification_complete")
        } catch {
            liveEmit("stopped error=\(error.localizedDescription) player_state=\(player.state) failure=\(player.failureMessage ?? "none")")
            let cleanup = library.livePlayback?.stop() ?? library.liveOperation?.cleanUp()
            player.finish()
            library.finishLivePlayback()
            if let result = await cleanup?.value { liveEmit("cancel_acknowledged result=\(result)") }
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

        guard let interruptedLoopbackURL = (player.player.currentItem?.asset as? AVURLAsset)?.url else {
            throw qualificationFailure("Active relay has no loopback URL.")
        }
        // Inject only the path notification, then verify a real authenticated
        // network round trip. This does not claim physical Wi-Fi-loss coverage.
        coordinator.handleNetworkAvailability(.unavailable)
        guard coordinator.state == .networkUnavailable else { throw qualificationFailure("Network-loss state was not entered.") }
        try await wait(seconds: 5) { player.state == .failed && player.player.currentItem == nil }
        try await requireUnreachable(interruptedLoopbackURL)
        emit("network_cleanup_passed trigger=simulated_path_event loopback_unreachable=true player_item_removed=true")
        coordinator.handleNetworkAvailability(.available)
        try await wait(seconds: 10) {
            if case .connected = coordinator.state { return true }
            return false
        }
        guard coordinator.remotePlaybackConfiguration()?.session.sessionID == originalSession else {
            throw qualificationFailure("Reconnect replaced the paired session.")
        }
        emit("same_session_reconnect_passed trigger=simulated_path_event transport=real_authenticated_request")
        guard let retryConfiguration = coordinator.remotePlaybackConfiguration() else {
            throw qualificationFailure("Reconnected relay has no retry configuration.")
        }
        await player.prepareRelayPlayback(retryConfiguration)
        try await wait(seconds: 10) { player.state == .ready || player.state == .failed }
        guard player.state == .ready else { throw qualificationFailure("Relay retry failed after reconnect.") }
        emit("same_session_playback_retry_passed")

        let loopbackURL = (player.player.currentItem?.asset as? AVURLAsset)?.url
        player.finish()
        coordinator.disconnect()
        guard player.state == .idle, player.player.currentItem == nil,
              coordinator.state == .idle, coordinator.remotePlaybackConfiguration() == nil,
              let loopbackURL else { throw qualificationFailure("Playback cleanup left active state.") }
        try await requireUnreachable(loopbackURL)
        emit("finish_cleanup_passed loopback_unreachable=true player_item_removed=true session_cleared=true")
        emit("control_probe_complete")
    }

    private static func requireUnreachable(_ url: URL) async throws {
        var request = URLRequest(url: url)
        request.timeoutInterval = 2
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        do {
            _ = try await session.data(for: request)
        } catch let error as URLError where error.code == .cannotConnectToHost || error.code == .networkConnectionLost {
            emit("loopback_connection_refused code=\(error.code.rawValue)")
            return
        }
        throw qualificationFailure("Loopback listener remained reachable after cleanup.")
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

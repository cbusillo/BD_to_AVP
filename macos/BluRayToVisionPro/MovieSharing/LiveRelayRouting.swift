import Foundation

/// The relay routes a live stream serves under its derived session, and the one place the host
/// checks the stream's media capability.
///
/// `LiveRelayStreaming.relayResponse(for:)` never sees the session, so the host requires the
/// `x-bdtoavp-relay-media-capability` header to match the derived session's capability on every
/// live route, not only `media/*`. The player's request factory sends it on every request.
enum LiveRelayRouting {
    static func isStreamRoute(_ target: String) -> Bool {
        target == RelayWireContract.playlistPath
            || target == RelayWireContract.playlistSnapshotPath
            || target == RelayWireContract.positionPath
            || target == RelayWireContract.cancelPath
            || target.hasPrefix(RelayWireContract.mediaPathPrefix)
    }

    static func carriesCapability(_ request: RelayHTTPRequest, of session: RelayEstablishedSession) -> Bool {
        request.header(named: RelayWireContract.mediaCapabilityHeader).map(session.mediaCapability.matches) ?? false
    }

    /// The signed answer for a stream that is not the parent session's current one.
    static func notCurrent(_ ended: LiveStartAdmission.Ended?) -> RelayHTTPResponse {
        .liveStreamEnded(ended?.body ?? RelayLiveStreamEnded(
            error: .streamEnded,
            code: RelayLiveStreamEnded.notCurrentCode,
            message: nil
        ))
    }
}

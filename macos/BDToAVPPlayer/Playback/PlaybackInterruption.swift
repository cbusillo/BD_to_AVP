import AVFoundation
import Foundation
import os

/// Errors the system raises when it takes media resources away from the app, such as when the
/// app leaves the foreground. Playback recovers from these by rebuilding at the same position
/// instead of reporting them (#1165).
enum PlaybackInterruptionPolicy {
    /// `AVError` codes that mean the system interrupted playback rather than the movie failing.
    /// Matched by raw value: these `AVError` cases exist only on iOS-family SDKs.
    static let interruptionCodes: Set<Int> = [
        -11847, // AVErrorOperationInterrupted
        -11819, // AVErrorMediaServicesWereReset
        -11839, // AVErrorDecoderTemporarilyUnavailable
    ]

    /// Whether `error`, or any error under it, is a system interruption.
    static func isInterruption(_ error: Error?) -> Bool {
        var current = error.map { $0 as NSError }
        var depth = 0
        while let nsError = current, depth < 8 {
            if nsError.domain == AVFoundationErrorDomain, interruptionCodes.contains(nsError.code) {
                return true
            }
            current = nsError.userInfo[NSUnderlyingErrorKey] as? NSError
            depth += 1
        }
        return false
    }
}

/// Limits how often playback rebuilds itself after interruptions, so a source that keeps
/// failing ends with a message and Try Again instead of looping. Attempts are spaced out, since
/// the system can refuse the decoder for a moment after the app returns.
struct PlaybackRecoveryBudget: Equatable {
    enum Decision: Equatable {
        case recover
        /// Try again after this many seconds.
        case wait(TimeInterval)
        case giveUp
    }

    static let attemptLimit = 3
    static let window: TimeInterval = 60
    static let spacing: TimeInterval = 1

    private(set) var attempts: [TimeInterval] = []

    /// Whether to rebuild now; a `.recover` counts as an attempt made at `now`.
    mutating func decision(now: TimeInterval) -> Decision {
        attempts.removeAll { now - $0 >= Self.window }
        guard attempts.count < Self.attemptLimit else {
            return .giveUp
        }
        if let last = attempts.last, now - last < Self.spacing {
            return .wait(Self.spacing - (now - last))
        }
        attempts.append(now)
        return .recover
    }

    mutating func reset() {
        attempts = []
    }
}

/// The words the player shows for an error. Only the app's own errors, written for the wearer,
/// are shown as they are; a system error's domain, code and details never reach the screen.
enum PlaybackErrorText {
    private static let logger = Logger(subsystem: "com.shinycomputers.bd-to-avp.player", category: "playback")

    static let couldNotPlay = "This movie could not be played. Try again."
    static let unsupportedFormat = "This movie uses a format the headset can't play."
    static let unreadable = "This movie could not be read. The file may be damaged."
    static let sourceStoppedResponding = "The movie's source stopped responding. Check the connection, then try again."
    static let interruptedTooOften = "Playback was interrupted and could not restart. Try again."

    /// A plain message for `error`, falling back to `fallback` for anything not written for the
    /// wearer. The error itself goes to the log for diagnostics.
    static func message(for error: Error?, fallback: String = couldNotPlay) -> String {
        guard let error else {
            return fallback
        }
        log(error)
        if let wearerFacing = error as? WearerFacingError, let description = wearerFacing.errorDescription {
            return description
        }
        var current: NSError? = error as NSError
        var depth = 0
        while let nsError = current, depth < 8 {
            if let message = message(domain: nsError.domain, code: nsError.code) {
                return message
            }
            current = nsError.userInfo[NSUnderlyingErrorKey] as? NSError
            depth += 1
        }
        return fallback
    }

    static func log(_ error: Error, context: String = "playback") {
        let nsError = error as NSError
        logger.error("\(context, privacy: .public) error domain=\(nsError.domain, privacy: .public) code=\(nsError.code, privacy: .public)")
    }

    private static func message(domain: String, code: Int) -> String? {
        switch domain {
        case AVFoundationErrorDomain:
            switch code {
            // FileFormatNotRecognized, DecoderNotFound, FormatUnsupported, IncompatibleAsset,
            // NoSourceTrack
            case -11828, -11833, -11864, -11848, -11869:
                return unsupportedFormat
            // DecodeFailed, FileFailedToParse, FailedToParse, UndecodableMediaData
            case -11821, -11829, -11853, -11855:
                return unreadable
            // ContentIsUnavailable, NoLongerPlayable
            case -11863, -11867:
                return sourceStoppedResponding
            default:
                return nil
            }
        case NSURLErrorDomain, NSPOSIXErrorDomain:
            return sourceStoppedResponding
        default:
            return nil
        }
    }
}

/// An error whose description is written for the wearer and may be shown as it is.
protocol WearerFacingError: LocalizedError {}

extension Bluray3DSourceError: WearerFacingError {}
extension ShareConnectionMonitor.ReadError: WearerFacingError {}
extension PackedStereoComposition.Error: WearerFacingError {}

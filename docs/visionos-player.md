# Shiny 3D Cinema

**Shiny 3D Cinema** (`BDToAVPPlayer` in Xcode) is the standalone visionOS 26
application for browsing and playing finalized 3D movies on Apple Vision Pro. It is separate from the macOS
converter and from `SpatialPlaybackProbe`,
which remains the qualification-only validator.

## Product Scope

- One plain SwiftUI window contains a split-view library, modal movie details,
  and the player.
- The library opens **Mac Movies**, where the wearer chooses a Mac and a completed
  movie. **On My Vision Pro** opens the local **Your movies** collection with
  Posters and Files modes, format filtering, title or
  filename sorting, 16:9 source-frame thumbnails, and a typography-first
  fallback when a frame cannot be generated.
- Playable movies expose a visible direct Play action in both library modes.
  Selecting the movie content opens a compact modal Details view with source
  status, missing-source recovery, removal, technical metadata, and one
  prominent Play action that remains visible without scrolling.
- **Add Movie** imports one movie through the system Files picker.
- Supported `.mov`, `.mp4`, and `.m4v` files already present in the app's
  Documents directory are indexed on launch. File Sharing and opening documents
  in place are enabled for this source path.
- Library metadata and resume positions are stored as bounded JSON under the
  app's Application Support directory. Library media records omit source
  filesystem URLs, but bookmark blobs necessarily encode the source location so
  the app can regain security-scoped access.
- Qualification builds also expose a Live Relay panel that discovers protocol-v3
  Macs through Bonjour, fetches a
  short-lived challenge, compares a large six-digit code with the Mac app, and
  starts an authenticated MV-HEVC EVENT-HLS asset without requiring a hostname
  or cloud service.

## Completed movies shared by a Mac

1. Open **Movie Sharing** in the Mac app and **Add Folder…**. Select a folder of
   completed `.mp4`, `.mov`, or `.m4v` movies, then enable sharing.
2. Keep the Mac awake with the app open, on the same trusted local network as
   Vision Pro. Allow Local Network access on both devices.
3. In **Mac Movies** on Vision Pro, tap **Find Macs** and choose the Mac. Compare the six-digit code
   with the Mac's Movie Sharing screen and confirm on both devices.
4. Search and select a movie on Vision Pro. The existing player provides audio
   and subtitle selection, seeking, stereo presentation, and resume positions.

The folder is configuration, not the playback selection: no movie needs to be
opened or selected on the Mac. Only MV-HEVC and supported full SBS/OU HEVC stereo
formats play; other completed movie files show the existing unsupported-format
message. ISO/BDMV conversion and live decrypted-source streaming are separate work.

Movie sharing advertises `_bdtoavp-movies._tcp` independently of playback. Approved
folders use security-scoped bookmarks. Scans skip symlinks, hidden files, and package
contents, and stop after 500 movies, five directory levels, 10,000 entries, or a
five-second scan budget. An unavailable drive is reported per folder. Refresh
rescans the folders; folder changes or stopping sharing revoke active sessions.

The Mac identity, approved device public keys, and each Vision Pro connection's
private key and pinned Mac key live in a nonsynchronizing Keychain item. Access or
decoding errors never silently reset trust. Reconnection proves possession against
a fresh challenge; recognizing a public key alone grants no movie access. **Forget**
removes trust, including when the Mac is offline. Forget on the Mac revokes the
device's active sessions immediately. A session lasts at most 24 hours.

Each movie has an opaque ID and a metadata revision. Signed requests bind the exact
revision, 64-bit offset, and count; each response authenticates the request nonce,
status, and full body before AVFoundation receives bytes. Ranges are at most 1 MiB.
The host opens files relative to the approved directory descriptor with `openat`
and `O_NOFOLLOW`, validates regular-file identity and metadata before and after
reading, and refuses changed sources. Revision checks detect ordinary replacement
and editing; they are not immutable filesystem snapshots. Resume records include
the revision so changed files cannot inherit a stale position.

Completed movies use `AVAssetResourceLoaderDelegate` directly. That route supports
progressive movie files; the EVENT-HLS media-segment restriction described below
does not apply to these completed files. Loading cancels with playback and bounds
concurrency and buffered data. HTTP provides authenticated integrity, not media
confidentiality. This initial private beta is for a trusted LAN; it does not wake
the Mac or run a background sharing daemon.

### Local development folder

The local repository config can remember the operator's movie folder without
committing a machine-specific path:

```sh
git config --local bdtoavp.movieSharingRoot /absolute/path/to/completed-movies
git config --local --get bdtoavp.movieSharingRoot
```

This hint is shared by the repository's linked worktrees and is not pushed to
GitHub. Select that folder through **Add Folder…** in the Mac app to grant access;
the app retains its own bookmark. Do not copy personal movie files into test
fixtures or enable sharing silently from repository configuration.

## Private internal TestFlight delivery

The visionOS bundle is `com.shinycomputers.bd-to-avp.player`, using team
`MM5YXC7T6E`. The App Store Connect record is **Shiny 3D Cinema** (6811956508).

The public app name is separate from its established engineering identity. Keep
the bundle ID, Xcode target/module, repository and package names, saved-data paths,
Keychain identities, URL schemes and Bonjour service types stable when changing
public branding. Update the App Store name, `CFBundleDisplayName`, `CFBundleName`,
visible UI/permission strings, current beta notes and privacy policy together.
Changing bundled branding requires a new build number and upload; editing only
the App Store name does not update an installed binary. See
[Apple's display-name instructions](https://developer.apple.com/library/archive/qa/qa1823/_index.html).

**AVP Internal** is the owner-only group; automatic distribution remains off.
The player uses only Apple's built-in cryptographic implementations for pairing
and authentication; its plist declares no non-exempt encryption. Reassess that
configuration if cryptographic dependencies or capabilities change.

Use an Xcode release currently accepted by App Store Connect, verified against
[Apple's release notes](https://developer.apple.com/help/app-store-connect/release-notes/).
A locally successful archive does not establish upload eligibility. Select that
installation for these commands with `BD_TO_AVP_XCODE_DEVELOPER_DIR`; this does not
change the machine's global Xcode selection.

Set and commit `MARKETING_VERSION` and `CURRENT_PROJECT_VERSION` for `BDToAVPPlayer`
in `macos/project.yml`, then build from that clean checkout. Use a higher build
number than any previously accepted upload. Keep the source commit, Xcode build number, archive and dSYMs,
export log and IPA SHA256 together with the delivery record.

```sh
BD_TO_AVP_XCODE_DEVELOPER_DIR=/absolute/path/to/supported/Xcode.app/Contents/Developer
DEVELOPER_DIR="$BD_TO_AVP_XCODE_DEVELOPER_DIR" /usr/bin/xcodebuild -version
git rev-parse HEAD
uv run python scripts/native_app.py generate
DEVELOPER_DIR="$BD_TO_AVP_XCODE_DEVELOPER_DIR" /usr/bin/xcodebuild archive \
  -project macos/BluRayToVisionPro.xcodeproj \
  -scheme BDToAVPPlayer -configuration Release \
  -destination 'generic/platform=visionOS' \
  -derivedDataPath build/testflight/DerivedData \
  -archivePath build/testflight/BDToAVPPlayer.xcarchive \
  -allowProvisioningUpdates
DEVELOPER_DIR="$BD_TO_AVP_XCODE_DEVELOPER_DIR" \
  PATH=/usr/bin:/bin:/usr/sbin:/sbin /usr/bin/xcodebuild -exportArchive \
  -archivePath build/testflight/BDToAVPPlayer.xcarchive \
  -exportPath build/testflight/export-internal \
  -exportOptionsPlist macos/TestFlightInternalExportOptions.plist \
  -allowProvisioningUpdates
```

The export options restrict the build to internal testing. The command-local
system PATH keeps Apple's copy tools paired with Apple's rsync during export.
For an authorized upload, copy the export options to the ignored build directory
and change only `destination` to `upload`, then export the same reviewed archive:

```sh
cp macos/TestFlightInternalExportOptions.plist build/testflight/ExportOptions-Upload.plist
/usr/libexec/PlistBuddy -c 'Set :destination upload' build/testflight/ExportOptions-Upload.plist
DEVELOPER_DIR="$BD_TO_AVP_XCODE_DEVELOPER_DIR" \
  PATH=/usr/bin:/bin:/usr/sbin:/sbin /usr/bin/xcodebuild -exportArchive \
  -archivePath build/testflight/BDToAVPPlayer.xcarchive \
  -exportPath build/testflight/upload-internal \
  -exportOptionsPlist build/testflight/ExportOptions-Upload.plist \
  -allowProvisioningUpdates
```

Wait for App Store Connect processing and attach the exact build to **AVP
Internal**. An upload rejected for an unsupported SDK/Xcode requires a new archive
from an accepted toolchain; retrying the old archive cannot fix it. Do not dispatch
the Mac Stable/Prerelease workflows for this.

To withdraw a beta, open its build in App Store Connect's TestFlight tab and use
**Expire Build**, which prevents further tester installation. Record the affected
version/build and reason, and select a known-good unexpired build for the internal
group when available. See [Apple's withdrawal procedure](https://developer.apple.com/help/app-store-connect/test-a-beta-version/stop-testing-a-build/).

Publish the compatible Mac companion with the repository's `publish-current`
command. Keep its stable Current link and the commit-addressed build metadata;
do not replace the production-signed app in `/Applications`.

Use the internal build for wearer acceptance: install from TestFlight, pair,
select a completed movie on Vision Pro, verify both eyes and audio, seek forward
and backward, exercise audio/subtitles and eye order where supported, close and
resume, then test Mac restart and Forget. Automated builds and decoded-frame
checks do not establish physical stereo presentation or sustained playback.

## External TestFlight delivery

An internal-only upload cannot be converted to external testing. Increment the
player's build number, commit the change, and create a fresh archive using the
supported toolchain and archive command above. Export with
`macos/TestFlightExternalExportOptions.plist`, which permits external review:

```sh
DEVELOPER_DIR="$BD_TO_AVP_XCODE_DEVELOPER_DIR" \
  PATH=/usr/bin:/bin:/usr/sbin:/sbin /usr/bin/xcodebuild -exportArchive \
  -archivePath build/testflight/BDToAVPPlayer.xcarchive \
  -exportPath build/testflight/export-external \
  -exportOptionsPlist macos/TestFlightExternalExportOptions.plist \
  -allowProvisioningUpdates
cp macos/TestFlightExternalExportOptions.plist build/testflight/ExportOptions-External-Upload.plist
/usr/libexec/PlistBuddy -c 'Set :destination upload' build/testflight/ExportOptions-External-Upload.plist
DEVELOPER_DIR="$BD_TO_AVP_XCODE_DEVELOPER_DIR" \
  PATH=/usr/bin:/bin:/usr/sbin:/sbin /usr/bin/xcodebuild -exportArchive \
  -archivePath build/testflight/BDToAVPPlayer.xcarchive \
  -exportPath build/testflight/upload-external \
  -exportOptionsPlist build/testflight/ExportOptions-External-Upload.plist \
  -allowProvisioningUpdates
```

Verify the signed export and keep its archive, symbols, commit, toolchain and
checksum with the delivery record. In App Store Connect, add the processed build
to **AVP External**, supply beta description/feedback and App Review contact
details, privacy policy, and concrete testing notes. Submit it for TestFlight
App Review. The first external build requires Apple's approval; a successful
upload alone does not make the build installable by external testers. See
[Apple's external testing procedure](https://developer.apple.com/help/app-store-connect/test-a-beta-version/invite-external-testers).

Invite only the user-selected audience: specific email addresses or an explicitly
requested public link. The existing internal group stays separate. Record review
status, group access and the final invitation mechanism with the exact build.

Reviewers can test the standalone player without an account, Mac, or downloaded
movie: choose **On My Vision Pro** in the sidebar, then **Start SBS Check** and
**Start Over-Under Check**. Both bundled synthetic samples are 45 seconds long and
silent. **Add Movie** opens the system file picker for the reviewer's own supported
completed movies. Explain separately that **Mac Movies** requires a compatible
Mac companion, one-time folder approval and pairing on the same trusted LAN.
Identify how testers obtain that matching companion before advertising Mac
sharing as ready for their setup; a developer's local Current build is not a
public Mac release.

The sidebar's **Privacy Policy** link opens the policy applicable to the build.
Its URL is pinned to the policy's published commit so branch deletion cannot
break it. Use the same URL in TestFlight metadata. Update the
[policy](visionos-player-privacy.md) and its link together when data handling
changes. Apple requires an accessible policy in the app and its metadata under
[the App Review Guidelines](https://developer.apple.com/app-store/review/guidelines/#privacy).

## Live Relay qualification

Relay sessions are source-agnostic: the wire contract carries session,
playlist, media, and playback state without MakeMKV-, SSIF-, AACS-, BD+-, or
encryption-specific fields. Pairing derives directional request keys and a
separate media capability. Every protected request binds the actual HTTP method,
raw request target, body, timestamp, and fresh nonce; replayed, expired,
reflected, unpaired, and capability-free requests fail closed.

The pairing transcript commits the server nonce before the client contribution,
then derives the numeric-comparison code under a dedicated HKDF domain after
the nonce is revealed and verified. One provisional candidate is retained for
five minutes from the pairing request, independently of the discovery challenge;
Vision Pro's authenticated confirmation and the exact candidate's
Mac approval are both required before protected routes open. A competing client
cannot displace the displayed candidate, stale UI approvals are rejected by
candidate ID, and three rejected candidates require a new relay. The player
serves the playlist, initialization map, and media segments through a device-only
HTTP listener bound to `127.0.0.1`, with a random per-playback URL capability.
Every upstream request is signed and each complete response is authenticated
before the bridge serves any bytes, including byte-range responses. The bridge
restricts routes to playback media, bounds concurrent connections, and cancels
its listener and requests when playback ends. AVFoundation requires HTTP media
segment loading; supplying fMP4 bytes through a custom-scheme resource loader
fails with `CoreMediaErrorDomain -12881`.
An expired comparison clears automatically on Vision Pro. Discovery stops with
recovery instructions after 15 seconds without a Mac. The Mac offers **Restart
Relay** using the selected fixture after its ten-minute advertising window ends;
a comparison already in progress retains its full five minutes.
A previously established client can reconnect while its session remains
unexpired.

`uv run python scripts/create_event_hls_mv_hevc_fixture.py` generates the
deterministic six-second fixture and checks stereo metadata, AAC tracks, and
actual macOS AVFoundation playback through the production loopback bridge. The
acceptance helper requires decoded frames and playback through the final
segment before publishing the fixture. This local gate does not establish
physical Vision Pro stereo presentation, audio sync, or interaction qualification.

The current local-network transport provides authenticated integrity and replay
protection, not confidentiality: HTTP media bodies and the short-lived media
capability are visible to an on-path LAN observer. This is an explicit limit of
the synthetic/decrypted relay slice. Any adapter carrying content whose threat
model requires confidentiality must add a separately reviewed encrypted
transport without weakening the existing request authentication.

The fixture host preserves `#EXT-X-ENDLIST`, so a completed fixture stays
completed when relayed instead of waiting indefinitely for more live segments.
The Mac retains a ten-second request deadline, then sends responses in 64 KiB
chunks with a ten-second progress timeout and a sixty-second total response cap.
This lets active segment transfers outlive the request deadline while still
closing stalled clients. The player's loopback request cap remains thirty seconds.
The Mac listener uses BSD sockets and DNS-SD Bonjour registration. On the physical
reference setup, identical files transferred in 0.10–0.17 seconds through a
standard socket server but took 22–44 seconds through the Network-framework
server, including a minimal reproduction without relay authentication. Waiting
for peer close and changing TCP options did not eliminate the delay. Authentication,
LAN peer checks, the sixteen-connection cap, and cancellation remain above the
socket transport. Shutdown interrupts blocked I/O; descriptor closure is serialized
after that I/O returns.

Candidate lifetime validation permits a server clock up to five seconds ahead,
matching the default signed-request skew allowance. The server still expires the
candidate at five minutes. Without that tolerance, a fast pairing response and
even a 100 ms clock difference could reject a valid full-length candidate.

The authenticated playlist snapshot supplies the retained window. The player
refreshes that window during playback, moves the scrubber floor forward when
history is evicted, and explains when a requested seek is before retained
history or ahead of produced media. Session expiry and unpaired responses stop
remote playback and require a fresh pairing.

## Mac-driven relay qualification

A build compiled with `BD_TO_AVP_QUALIFICATION` can perform relay setup without
headset UI interaction. Build and install the qualification player as described
in `visionos-sustained-playback-qualification.md`, start a fixture relay on the
Mac, then launch the player with the exact Bonjour display name:

```sh
xcrun devicectl device process launch --device "$DEVICE_ID" \
  --terminate-existing --console \
  --environment-variables '{"BD_TO_AVP_RELAY_QUALIFICATION_SERVER":"YOUR_MAC_NAME"}' \
  com.shinycomputers.bd-to-avp.player
```

The driver uses the production Bonjour browser, pairing coordinator, and relay
player. It prints `RELAY_QUALIFICATION comparison_code=...` and confirms on
Vision Pro. Compare the printed code with the Mac's visible code, then approve
**Codes Match** on the Mac. Only matching confirmation on both sides opens
media access. The driver waits at most four minutes for Mac approval, prepares
playback, and observes the complete finalized fixture (two to thirty seconds).
If readiness misses thirty seconds, it records a failed startup target and
continues observing for up to ninety more seconds. It also samples decoded
pixel buffers once ready and requires a sample in each two-second interval plus
a sample within 250 ms of the end. Readiness and first-decoded-frame timing are
reported separately against the thirty-second startup target. These are sampled
decode checks, not proof that every frame or both eyes were rendered. Later
readiness does not waive the startup target; physical presentation still needs
the wearer report.
It does not run unless both the compilation condition and explicit server-name
environment variable are present. Normal builds contain no driver.

Add `BD_TO_AVP_RELAY_TRANSFER_PROBE=1` to the launch environment to download up
to three retained segments through the authenticated client without AVPlayer.
This probe disables transient retries and reports transfer and verification
durations separately. It does not establish playback acceptance.

Add `BD_TO_AVP_RELAY_CONTROL_PROBE=1` instead to check pause, retained backward
seek, resume, and same-session reconnect after playback observation. The reconnect
probe injects the network-path notification but performs a real authenticated
request; it does not physically disconnect Wi-Fi. The probe finishes playback,
disconnects the coordinator, and checks that the loopback listener is unreachable.
It requires a fixture at least five seconds long and returns to the library.

This is programmatic device qualification, not UI acceptance or proof of stereo
presentation or audio sync. The ordinary pairing UI and physical
presentation still need their own checks. Physical native visionOS XCTest UI
startup timed out while enabling automation on September 7; simulator UI success
must not be treated as physical-device automation support.

For a longer wearer acceptance run, generate a separate fixture:

```sh
BD_TO_AVP_FFMPEG_PATH=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg \
BD_TO_AVP_FFPROBE_PATH=/opt/homebrew/opt/ffmpeg-full/bin/ffprobe \
uv run python scripts/create_event_hls_mv_hevc_fixture.py \
  ~/Downloads/BDToAVP-Relay-Acceptance --acceptance
```

Use an FFmpeg build with `drawtext` support; the example selects Homebrew
`ffmpeg-full`, when installed.

This opt-in profile lasts 24 seconds, fitting the existing twelve-segment relay
retention limit. The default six-second fixture remains unchanged. The acceptance
profile adds an elapsed clock, per-eye identity labels, and a small white SYNC
patch with a simultaneous 100 ms beep every two seconds starting at two seconds.
The depth geometry stays the same: blue behind, green at screen depth, red in
front. Check each eye separately for its label, then both eyes for comfortable
depth; both labels visible together alone do not prove correct eye routing.

First let the automatic timeline probe finish without pausing or seeking. For
the wearer pass, replay and confirm flash/beep alignment before and after using
the ordinary pause/resume and backward-seek controls. Record the app commit,
fixture hashes, device, first-frame time, and the wearer's actual observations.
Generation decodes all eleven flash/beep pairs and rejects offsets greater than
one 30 fps frame. Video composition offsets are normalized before fragmentation
to preserve the source timeline. This file check covers base-view cues; it does
not establish headset presentation timing or correct eye routing.

Keep actual interruption evidence separate from injected events: test active
Mac cancellation and app quit, then device-side network interruption/reconnect
when the wearer is ready. Record listener and player cleanup. Do not change the
Mac's network settings to run this check. Fixture playback does not prove live
source child-process cleanup, producer throughput, or full-title playback; those
remain in #713 and #719.

Set `BD_TO_AVP_RELAY_INTERRUPTION_PROBE=1` alongside the qualification server
variable to wait after the first decoded frame for an actual external
interruption. The probe checks failed playback, removal of the player item,
retained relay retry identity, and an unreachable former loopback URL. It does
not itself toggle networking or cancel the Mac session.

Relay interruption handling keeps playback ownership separate from pairing.
Terminal session/authentication failures release the player item, loopback
listener, observers, and refresh task before showing the error. Three consecutive
failed snapshot refreshes also release playback resources; successful refreshes
reset that count. This is a request-count bound, with transport timeouts still
applying, rather than a promise of three seconds.

A device network outage releases playback resources while retaining the unexpired
pairing. Once connectivity returns, the coordinator verifies the same session
with a signed request; use Retry to resume relay playback. If the Mac cancels or
quits, bounded reconnect attempts end with an honest failure. A session or
authentication rejection requires pairing again. Failed relay playback keeps its
relay identity so Retry cannot accidentally enter the local-file flow.

## Source Access

Imported files receive persistent bookmark data. Playback resolves the bookmark
and keeps one balanced security-scoped access lease open for the complete player
session. The lease closes when playback finishes, preparation is replaced, or
the session is destroyed. Bookmark resolution, provider access, and existence
probing run outside the main actor, so a slow Files or SMB-backed provider cannot
freeze the window before playback's loading state appears.

Files in the app's Documents directory use the same library and bookmark path as
Files-picker imports. If a source moves or disappears, the details view reports
that state and offers retry or locate recovery instead of treating a temporary
provider outage as deletion. A successful locate preserves the stable library
identity while refreshing its filename, detected format, and bookmark only after
inspection succeeds.

Playback preparation distinguishes opening the source from preparing the media.
The source-opening state is indeterminate and cancellable because third-party
File Providers do not guarantee portable download percentages. Recoverable
failures expose **Try Again**, **Locate**, and **Done** in both the RealityKit
ornament and packed-stereo AVKit actions. Retry starts a fresh bounded session
generation; Locate updates the existing item and then prepares it again. Done
invalidates the active generation, closes any acquired lease, and prevents late
provider completion from installing an item or starting playback.

## Playback Contract

The player supports MV-HEVC plus explicitly identified HEVC side-by-side and
over-under movies. MV-HEVC detection uses AVFoundation stereo multiview playback
characteristics; an HEVC codec tag alone is not accepted as proof of MV-HEVC.
Packed stereo detection prefers embedded packing metadata and otherwise accepts
conservative, separator-delimited filename tokens such as `SBS`, `FSBS`, `OU`,
or `FOU`. These tokens are treated as full-resolution packing; half-resolution
markers such as `HSBS`, `HOU`, `Half-SBS`, and `H-OU` remain unsupported because
they require anamorphic aspect reconstruction. The packed-stereo compositor is currently
qualified for SDR HEVC only. It does not infer stereo from an unusually wide or
tall frame alone.

`MVHEVCPlayerSession` owns one `AVPlayer`, one active `AVPlayerItem`, one
RealityKit entity for MV-HEVC, and one source lease. MV-HEVC playback uses a
`VideoPlayerComponent` configured for stereo viewing, screen spatial video mode,
and portal immersive viewing mode. Packed stereo uses `AVPlayerViewController`,
matching AVFoundation's custom spatial-compositor presentation path. AVKit
supplies play/pause, seeking, the scrubber and time display, audio selection,
and subtitle selection. Eye Order and Done use AVKit's visionOS-native
`contextualActions`, so they remain part of the same supported playback control
surface instead of relying on an overlay beneath AVKit's interaction layer.
Together the controls provide:

- play and pause;
- 10-second backward and 30-second forward seeks;
- a position slider and elapsed/duration display;
- audio-track selection;
- subtitle selection, including Off;
- eye-order swapping for side-by-side and over-under playback;
- retry and locate recovery when Files-backed preparation fails; and
- Done, which persists progress and releases the session.

The MV-HEVC ornament remains visible while playback is paused, loading, failed,
or being scrubbed, and automatic hiding is disabled while VoiceOver or Switch
Control is active. During uninterrupted playback it hides after three seconds.
Pinching the video surface reveals the controls again. Packed-stereo controls
use AVKit's native visibility behavior; revealing the playback controls also
reveals actions labeled **Eye Order: Normal** or **Eye Order: Reversed** and
**Done**.

AVKit owns the packed-stereo audio menu and may present multiple same-language
tracks with the same localized label. The app explicitly asserts the active
packed-stereo audio option after the item becomes ready, but it does not replace
or claim to relabel AVKit's native menu. The app-owned MV-HEVC audio menu leaves
unique names unchanged and adds role and stable track-number details only when
labels collide.

The app saves in-progress playback positions and restores them when the same
library item is opened again. Any non-active scene phase cancels pending
autoplay and pauses playback, saving current progress when a player item is
available. Initial preparation and eye-order replacement remain paused if they
complete while the scene is inactive. Returning active restores scene
permission but never infers a new play request; playback remains paused until
the user starts it. Loading or failed preparations never overwrite an existing
resume point because resume writes begin only after a player item has reached
ready state. Completed or near-completed playback is cleared instead of resuming
at the end.

Packed stereo playback uses a visionOS 26 custom video composition with one
stable two-buffer output contract: output zero is always tagged as the left eye
and output one as the right eye. The composition supplies and attaches one
rectangular `AVSpatialVideoConfiguration` to both outputs. Source spatial
metadata is retained when present; the synthetic checks use their known 90°
field of view and 64 mm baseline, while imported packed media is not assigned
invented camera geometry. A custom composition instruction carries the packed
layout and requested eye order, and the compositor swaps source regions into
those fixed semantic outputs. Rendering copies native biplanar YUV planes and
uses an explicit SDR BT.709 output contract instead of mis-tagging converted HDR
frames or converting through device RGB. Eye-order changes replace only the
immutable `AVPlayerItem` while retaining
the `AVPlayer`, presentation surface, source lease, playback time and intent, audio
selection, and subtitle selection. Composition-backed seeks wait for the newly
rendered frame before restoration completes.

The Library always exposes a **Built-in stereo checks** panel with bundled,
reproducibly generated HEVC fixtures for side-by-side and over-under playback.
No import or filename preparation is required. Each eye image carries an
exclusive `LEFT EYE ONLY` or `RIGHT EYE ONLY` label, an instruction to cover the
other eye, and asymmetric depth markers. The app copies the fixtures into
Application Support using a versioned, size-checked installation and refreshes
their stable library records and bookmarks on every launch so app updates cannot
leave stale bundle-path references. During a check, AVKit exposes Eye Order as a
native playback action labeled Normal or Reversed.

Files-provider sources may be backed by SMB or other network storage, but the app
does not implement direct `smb://` transport, offline pinning,
provider-specific progress percentages, raw MVC playback, or source-side live
conversion. Live Relay is limited to the authenticated EVENT-HLS contract; disc
reading and just-in-time source production remain separate Mac-side work.

## Build And Test

Generate the Xcode project from the checked-in specification:

```sh
uv run python scripts/native_app.py generate
```

Run the visionOS simulator unit suite when a compatible runtime and Apple Vision
Pro simulator device type are installed:

```sh
xcodebuild test \
  -project macos/BluRayToVisionPro.xcodeproj \
  -scheme BDToAVPPlayer \
  -configuration Debug \
  -destination 'platform=visionOS Simulator,name=Apple Vision Pro' \
  -derivedDataPath macos/build/BDToAVPPlayerDerivedData \
  CODE_SIGNING_ALLOWED=NO
```

The same scheme also contains `BDToAVPPlayerUITests`. Its built-in check test is
unconditional: it verifies that the stereo-check panel is visible from launch,
starts the SBS check, and confirms that AVKit exposes native Eye Order and Done
actions. XRSimulator does not dispatch those actions while it displays the
tagged-stereo unsupported-content placeholder, so the unit suite separately
drives the production eye-order rebuild from ready through reversed and ready
again.
Its seeded-media flow is skipped when `PlayerLongFixture.mov` is absent from the
simulator app Documents directory; when present, it verifies direct Library →
Play → Library, Library → Details → Play, Details Done → Library, player Done →
Details, replay, and ornament auto-hide behavior.

CI pins Xcode 26.5, regenerates the project, and always runs
`build-for-testing` against the generic visionOS Simulator destination. When an
available visionOS 26-or-newer runtime and Apple Vision Pro device type exist,
CI creates, boots, tests, and deletes a temporary simulator with
`test-without-building`.
If either prerequisite is absent, CI logs a clear successful skip instead of
assuming a pre-existing simulator.

For a connected headset, build with automatic development provisioning:

```sh
xcodebuild build \
  -project macos/BluRayToVisionPro.xcodeproj \
  -scheme BDToAVPPlayer \
  -configuration Debug \
  -destination 'platform=visionOS,id=<device-id>' \
  -derivedDataPath macos/build/BDToAVPPlayerDeviceDerivedData \
  -allowProvisioningUpdates
```

Simulator tests and screenshots do not prove stereoscopic depth, eye order,
comfort, long-session thermals, or headset-visible interaction. The accepted
physical layout keeps the RealityKit video surface at the playback probe's
conservative scale and depth offset so foreground stereo content remains behind
the native ornament; changes to that geometry still require physical Vision Pro
validation. The visionOS Simulator can expose the AVKit surface and contextual
actions but displays an unsupported-content placeholder for tagged stereo
output. The automated suite therefore also prepares each bundled fixture through
the real `AVPlayer` composition path and exercises the production rendering core
directly. It asserts normal and reversed eye pixels, fixed semantic eye tags,
native pixel format, chroma routing, color attachment propagation, fixture
installation, format detection, and UI discoverability. Physical Vision Pro
remains the only way to prove that AVKit routes those tagged outputs to the
intended eyes; that qualification is a single bounded gate after local tests
pass.

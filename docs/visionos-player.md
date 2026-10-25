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

For installation and a first playback check, use the
[Movie Sharing beta quick start](movie-sharing-beta.md).

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
message. Blu-ray 3D ISOs in a shared folder appear separately as live titles; see
[Live Blu-ray 3D titles](#live-blu-ray-3d-titles).

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

### Live Blu-ray 3D titles

A shared folder's `.iso` files are listed under **Blu-ray 3D Discs (Live)**. They come
from the catalog's additive `liveTitles` list, which older players ignore, and never
appear among completed movies. A row shows **Checking on the Mac…** while the Mac
inspects the disc in the background; the player refreshes the catalog every 3 seconds
for up to 2 minutes while any title is checking, and stops on disconnect, rediscovery,
or leaving the library. An ineligible title shows the Mac's reason and cannot start.

Choosing an eligible title starts it with the first audio track in the headset's most
preferred language that the title has, or the disc's first track when none matches.
The Mac carries the title's other supported tracks too, so the audio list offers every
track and switching keeps the playhead; see [Live HLS Service](live-hls-service.md#alternate-audio).
Live titles carry no subtitles yet, and the subtitle list says so:

1. The player creates a start ID and sends `POST /movies/v1/live/start`, signed with
   the movie session at `issuedAt`, the signed time of that first request. A retry
   after a lost answer repeats the same body with a fresh signature; the Mac answers a
   repeated start ID with its recorded outcome.
2. The signed 202 carries the stream ID. The player polls the parent-signed live
   status once a second until the stream is `live`, showing **The Mac is busy
   converting; playback may pause.** while a conversion runs. A Mac failure such as
   its 45-second ready deadline shows the Mac's reason. The player gives up 120 seconds
   after sending the start.
3. The player derives the stream session with `liveStreamSession(streamID:)` and plays
   the stream's relay routes at the Mac's address. Every playlist, snapshot, segment,
   position, and cancel request is signed with that stream session and carries its
   media capability; the parent session signs only `/movies/v1/...` routes. The
   one-second snapshot refresh also reports the playhead to
   `POST /relay/v1/control/position`, and a pause or seek reports it at once. Live
   items use a 20-second forward buffer. A live title cannot resume yet, because the
   Mac can only start the stream from the beginning. The player still records how far
   the wearer got, and when they open the title again it says that playback starts
   from the beginning and where they stopped, instead of silently starting over.

Cleanup is owned by the start until the player installs the stream, then by the
playback. It runs detached, with a fresh signature on every attempt and retries after
0.5, 1, 2, and 4 seconds, then every 4 seconds. The route depends on what the player
knows:

- With the stream ID: the stream's own `POST /relay/v1/control/cancel`. It works after
  any length of playback; a signed 204 or a signed `stream_ended` both mean done. Done,
  teardown, a failed or superseded preparation, a stream failure, and the client
  timeout after a status all use it.
- Without the stream ID, within 40 seconds of `issuedAt`: `POST /movies/v1/live/cancel`
  by start ID. An acknowledged cancel means that start is never admitted.
- Without the stream ID after that, or when the start-ID cancel answers `expired`:
  `POST /movies/v1/live/release`, repeated until `issuedAt + 75 s` or until the Mac says
  it drained that exact start. No start for the identity can be admitted after 70 s.

The Mac's idle lease, 120 seconds before the first relay request and 60 seconds after,
stops anything every cleanup attempt missed.

A signed 410 or 503 carrying `RelayLiveStreamEnded` ends playback with "Live playback
stopped on the Mac: <reason>", using the reason from the Mac's live status when it has
one; it never asks to pair again. Only an unsigned, empty 401, meaning the Mac no longer
knows the session, shows the pair-again message. The loopback bridge turns every
upstream failure into a bare 503, so termination is decided by the verified snapshot
refresh, not by AVPlayer's media errors.

#### Live qualification on device

A `BD_TO_AVP_QUALIFICATION` build runs a scripted live session when launched with
`BD_TO_AVP_LIVE_QUALIFICATION_MAC` (the Mac's Bonjour name) and
`BD_TO_AVP_LIVE_QUALIFICATION_TITLE` (the ISO file name or title). It connects with the
remembered pairing, waits for the title to become eligible, plays it with its default
audio for 600 seconds (override with `BD_TO_AVP_LIVE_QUALIFICATION_PLAY_SECONDS`), then
pauses and resumes, seeks back to a retained position, seeks to an evicted position, and
finishes with Done. It writes `RELAY_QUALIFICATION LIVE t=<seconds>` lines with a
monotonic clock: `live_start_sent`, `status_live`, `first_frame`, `rebuffer_start`,
`rebuffer_end`, periodic `sample` lines with the playhead and live edge,
`seek_backward_result`, `seek_evicted_result`, and `cancel_acknowledged`. Release builds
do not compile the driver.

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
completed movies. **Mac Movies** requires the full **3D Blu-ray to Vision Pro**
Mac app with Movie Sharing, one-time folder approval and pairing on the same
trusted LAN. The [Movie Sharing quick start](movie-sharing-beta.md) explains installation,
the Stable and optional Beta updater routes and the initial stereo checks. Include the matching
signed Mac release link in TestFlight's beta description and review notes once
it is published; a developer's local Current build is not a public Mac release.

### Point the TestFlight text at each new Mac Beta

After every published Mac Beta, update the player's TestFlight text so it names
the new Mac build. Use the App Store Connect record identified in
[Private internal TestFlight delivery](#private-internal-testflight-delivery).
Two fields carry the Mac version, release link and guide link:

- the en-US `betaAppLocalizations` `description`
- the `betaAppReviewDetails` `notes`

The change is two substitutions in each field: the Mac version and build in the
form `0.3.3 beta N (build B)`, and the `v0.3.3-beta.N` tag in the release and
guide URLs.

Use the App Store Connect API. Sign an ES256 JWT with the team API key
(`aud: appstoreconnect-v1`, lifetime at most 20 minutes). Read the current values
with `GET /v1/apps/{appId}/betaAppLocalizations` and
`GET /v1/apps/{appId}/betaAppReviewDetail`. Write them with
`PATCH /v1/betaAppLocalizations/{localizationId}` and
`PATCH /v1/betaAppReviewDetails/{appId}`, then read both fields back. The API
key is not in this repository; its location is recorded in the owner's private
infrastructure documentation. Never print the key or its identifiers into logs,
issues or pull requests.

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
The served playlist has no `EXT-X-PLAYLIST-TYPE` tag even though the fixture
file is an EVENT playlist, because the relay's retained window evicts older
segments and an EVENT playlist must never remove any.
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
- a scrub bar with elapsed and total time, described below;
- audio-track selection;
- subtitle selection, including Off;
- eye-order swapping for side-by-side and over-under playback;
- retry and locate recovery when Files-backed preparation fails; and
- Done, which persists progress and releases the session.

The MV-HEVC ornament remains fully visible while playback is paused, loading,
failed, or being scrubbed, while a pointer hovers over it, and while its audio
or subtitle list is open. Automatic fading is disabled while VoiceOver or Switch
Control is active. During uninterrupted playback the ornament fades to nearly
invisible three seconds after the last of those ends; it is never removed. The
faded controls stay in place and interactive, and a system hover effect brings
them back to full opacity while the wearer looks at them, so a look and pinch
works without a separate show-controls button. The reveal waits for a short
dwell, about a third of a second, so a passing glance does not pop the controls
up, and then fades in gently; after the wearer looks away the controls hold for about 1.5 seconds and then fade more
slowly. The timings live in `PlaybackHUDPresentation`. visionOS renders that effect out
of process and never reports where the wearer looks, so the app's own state
stays faded until an interaction or pinching the video surface reveals the
controls again.

The MV-HEVC scrub bar spans the title's full length from the first frame and
shows the range that can be played right now as a lighter band. A completed
movie's length comes from the movie itself, and the band is the player's
buffered range around the playhead. A live title's length comes from the Mac's
live title index (`durationSeconds`), and the band is the retained live window.
Its end time stays fixed while the stream loads, and the real length replaces it
once the stream is finalized. When the Mac has not reported a length, the bar
grows with the stream as before. A seek outside the band keeps the existing
notices: before the window, playback continues at its start; past the live edge,
playback moves to the latest available time and says how far the relay reaches.

The MV-HEVC audio language and subtitle lists open at the bottom of the window,
just in front of the video and directly above the controls, so they read as part of
the controls panel. Both buttons are enabled once the movie is ready. A list with
nothing to choose between says so: **This title has one audio track.** or **This
title has no subtitles.** The list is a RealityView attachment placed in the
video's own space. The alternatives each failed: growing the controls ornament
pushed the list below the window; a second ornament, or a SwiftUI view in the
window, sat behind the window's surface and was hidden; a plain SwiftUI overlay
floated at the front of the window's depth and loomed over the controls; and a
system menu presented from the ornament opened behind the video. The list is also
outside the controls' hover group, so only the row the wearer looks at highlights.
It shows up to five rows and scrolls beyond that, starting at the current
selection, so a disc with a dozen or more tracks stays usable. While a list is
open, the video surface does not take pinches, so they reach the list.

Packed-stereo controls use AVKit's native visibility behavior; revealing the
playback controls also reveals actions labeled **Eye Order: Normal** or
**Eye Order: Reversed** and **Done**.

AVKit owns the packed-stereo audio menu and may present multiple same-language
tracks with the same localized label. The app explicitly asserts the active
packed-stereo audio option after the item becomes ready, but it does not replace
or claim to relabel AVKit's native menu. The app-owned MV-HEVC audio list leaves
unique names unchanged and adds role and stable track-number details only when
labels collide.

The app saves in-progress playback positions and restores them when the same
library item is opened again. An inactive scene keeps playing: on visionOS that
phase covers Control Center, looking at another window, and other ordinary focus
changes, so the player only saves current progress then. Moving to the background
cancels pending autoplay and pauses playback, saving current progress when a
player item is available. Initial preparation and eye-order replacement remain
paused if they complete while the app is in the background. Returning active
restores scene permission but never infers a new play request; playback remains
paused until the user starts it. Loading or failed preparations never overwrite
an existing resume point because resume writes begin only after a player item
has reached ready state. While playing, the position is also saved every 10
seconds, so a forced quit or a closed window that runs no end path loses only
seconds. Completed or near-completed playback is cleared instead of resuming at
the end.

A shared movie's position is keyed by the Mac's `resumeKey`: a digest of the
file's name, size, and modification time. Unlike the movie's `id` (which digests
the folder approval's ID and path) and its `revision` (which includes change time
and generation), it survives removing and re-adding the folder, a Mac app rebuild,
and metadata churn on a network share; editing or replacing the file changes it.
A position saved under the older `shared:<id>:<revision>` key moves to the new key
the first time the movie is opened while that key still matches. A Mac that does
not send `resumeKey` keeps the older key.

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
exclusive `LEFT EYE ONLY` or `RIGHT EYE ONLY` label, an instruction to close the
other eye, and asymmetric depth markers. With **Eye Order: Normal**, blue should
appear behind the screen, green on the screen plane, and red in front. In SBS,
the left half is the left eye; in over-under, the top half is the left eye.
Close the right eye to see only `LEFT EYE ONLY`, then close the left eye to see
only `RIGHT EYE ONLY`. Reversed eye order should invert the depth and swap those
labels; return to Normal afterward. The depth-marker disparities are checked from decoded
bundled pixels, but the corrected fixtures still need hands-on Vision Pro
confirmation. The checks are silent; use the audio validation matrix for sound.
The app copies the fixtures into
Application Support using a versioned, size-checked installation and refreshes
their stable library records and bookmarks on every launch so app updates cannot
leave stale bundle-path references. During a check, AVKit exposes Eye Order as a
native playback action labeled Normal or Reversed.

Files-provider sources may be backed by SMB or other network storage, but the app
does not implement direct `smb://` transport, offline pinning,
provider-specific progress percentages, raw MVC playback, or source-side live
conversion. Live Relay is limited to the authenticated EVENT-HLS contract; disc
reading and just-in-time source production remain separate Mac-side work.

## Blu-ray 3D Sources

Native Blu-ray playback (#711) reads Blu-ray 3D sources on the headset itself, with
no Mac (#1110). **Add Movie** accepts an unencrypted 3D ISO, a disc folder (the folder
that holds `BDMV`, or `BDMV` itself), or an MKV that keeps the MVC 3D track, such as
a MakeMKV rip, from the headset or a Files share such as SMB. ISOs, MKVs and disc
folders copied into the app's Documents folder are listed on launch. Adding a source
opens it, chooses its title and stores that title with the library item; the
details view shows the title's length, audio and subtitles, and **Play** plays it on
the headset (see [Native Blu-ray Video](#native-blu-ray-video)). Encrypted discs still need the Mac and the user's
own MakeMKV.

The code is in `macos/BDToAVPPlayer/BluraySource`:

- **Disc images and folders** open through libbluray. The title rules match the Mac's
  `ssif_probe titles`: a title must be at least 60 seconds, have one angle and one
  clip that it plays completely, and have the clip's SSIF stereo file. Playable
  titles come first, then the longest, then the disc's main feature, then the lower
  playlist number. The first playable title is the one the player uses.
- **The SSIF demuxer** (`bd3d_source.c`, adapted from `native/ssif_probe`) pairs each
  base-view access unit (PID 0x1011) with the dependent view (0x1012) that has the
  same DTS. It delivers whole PES packets for the chosen audio and PG subtitle PIDs.
  `TransportStreamPESReader.parse` builds the `BluRayPESPacket` values, so every
  source reads PES headers the same way.
- **Matroska** is read by a small EBML parser. The video track must be
  `V_MPEG4/ISO/AVC` with an `mvcC` block addition mapping. Each block already holds a
  frame's base and dependent NAL units, so the reader only converts them to Annex B
  and puts the `mvcC` parameter sets (SPS, subset SPS and both PPS) before the first
  frame. Audio and subtitle blocks become `BluRayPESPacket` values with stream ID
  0xBD and the block's PTS. Matroska TrueHD has no AC-3 core and Matroska PCM has no
  Blu-ray LPCM header, so neither is offered to the audio player.

Every source delivers, in stream order, to a `Bluray3DPacketSink`:
`MVCAccessUnit` values (one stereo frame as Annex B for edge264, with its 33-bit PTS
and DTS) and `BluRayPESPacket` values for audio and subtitles. `Bluray3DPacketRouter`
sends audio to `HeadsetAudioPlayer`. Returning false from the sink stops the reader,
and the sink paces it by blocking. All timestamps are raw 33-bit 90 kHz values;
`BluRayPresentationTimeline` maps them to playback time with the title's first video
PTS as zero.

Titles made of several clips are not played yet. When they are, the reader will
rewrite each clip's timestamps onto one playlist timeline, so audio and video keep a
single clock. `discontinuity()` stays reserved for seeks.

libbluray 1.4.1 and libudfread 1.2.0, both LGPL-2.1-or-later, are linked as separate
dynamic frameworks in `macos/Vendor/libbluray.xcframework` and
`macos/Vendor/libudfread.xcframework`. Each framework carries its `COPYING` text. They
are built from the same pinned source archives as the Mac's `ssif_probe`, recorded in
`vendor/ssif-probe-macos-arm64.toml` and kept in `bd_to_avp/resources/notices/ssif-probe`.
Rebuild or verify them with:

```sh
uv run python -m scripts.build_bluray_visionos
uv run python -m scripts.build_bluray_visionos --verify-only
```

A qualification build (`BD_TO_AVP_QUALIFICATION`) measures read speed when launched
with `BD_TO_AVP_SOURCE_READ_ITEM` set to a library item's file name. It reads the
title as fast as the source allows for `BD_TO_AVP_SOURCE_READ_SECONDS` (default 120),
prints `SOURCE_READ` lines, and writes `source-read-<file name>.json` to Documents.

## Native Blu-ray Video

Shiny 3D Cinema decodes Blu-ray 3D video (H.264 MVC) on the headset with
[edge264-mvc](https://github.com/jens-duttke/edge264-mvc), BSD-3-Clause, at the
revision the Mac app pins in `bd_to_avp/resources/notices/edge264-mvc-build.json`.
It is linked as the separate dynamic framework `macos/Vendor/Edge264MVC.xcframework`,
which also holds the small wrapper in `native/edge264_mvc` and exports only the
wrapper's `bdavp_mvc_*` functions. The license text ships inside the framework and
in `macos/Vendor/Edge264MVC-notices`, next to the build's provenance. Rebuild it
from the pin, byte for byte, with:

```sh
uv run python scripts/build_edge264_visionos.py
```

`MVCDecoder` feeds each `MVCAccessUnit` to the decoder with its timeline ticks, and
frames come back in display order carrying those ticks. `StereoSampleBufferFactory`
copies each view into an NV12 pixel buffer, tags them `.stereoView(.leftEye)` and
`.stereoView(.rightEye)` (the base view is the left eye unless the title says
otherwise), and wraps both in one sample buffer for `AVSampleBufferVideoRenderer`,
the display path proven in #1103.

`NativeBlurayPlayback` plays a title with three threads. The reader fills a
`ReadAheadBuffer` up to 15 seconds ahead of the decoder (256 MB at most), because
SMB reads of the Rainforest ISO averaged 11.9 MB/s but had a slowest second of
0.81 MB/s against about 5 MB/s needed (#1110). The video thread decodes up to half
a second ahead of the clock and enqueues to `AVSampleBufferVideoRenderer`; the
renderer takes frames only once RealityKit shows it, so decoding waits for the
player view. The audio thread hands `HeadsetAudioPlayer` its packets 2.5 seconds
ahead of the clock. Both renderers join one `AVSampleBufferRenderSynchronizer`, so
pause, buffering and seeking move audio and video together.

The clock starts once a few frames are queued and two seconds are buffered. If the
renderer is about to run dry before the title ends, the clock stops, the player
shows that it is loading, and playback continues once four seconds are buffered
again. The buffer keeps 20 seconds behind the decoder, so a seek inside that
history or the loaded part ahead restarts at the nearest earlier IDR and shows
frames from the target on. Accepting a seek reserves that IDR, so trimming history
cannot drop it before the video thread gets there. A seek outside the loaded part is
refused until #1113 adds seeking anywhere. Every clock change (start, pause, buffering,
seek, stop) takes one lock from the state it depends on through the synchronizer
write, so a pause can never be undone by a rate computed just before it.

If the chosen audio cannot play, because its decoder cannot start, 20 packets in a
row fail to decode, or the audio renderer fails, `HeadsetAudioPlayer.failure` says so
in plain words and the engine reports it while the video plays on. A failed renderer
stops queuing audio (decoded audio waiting for it is capped at 1,024 chunks anyway),
and a seek or another track tries again.

In the player, `MVHEVCPlayerSession` routes a Blu-ray 3D item to
`NativeBlurayPlayback` instead of `AVPlayer`, and the existing controls, look to
reveal and track lists drive it. The scrub bar spans the title's length, and its
lighter band is the loaded part. While the title loads more, the controls stay up
with a **Loading…** line. The audio list names what plays, for example
**English (DTS core)**, and switches without stopping. If the chosen audio cannot play, the controls
say so while the video plays on. The subtitle list offers
only Off and says the title's subtitles come later (#1114). The resume position is
saved, but a title starts from the beginning and says where the wearer stopped
until resuming anywhere arrives (#1113).

The video is a RealityKit `VideoPlayerComponent(videoRenderer:)` sized inside the
window, and the controls are an ornament below the window. Until RealityKit reports
the video's size, the video is kept tiny, so it can never draw at its natural size
over the controls, the layout bug #1103 hit.

A qualification build (`BD_TO_AVP_QUALIFICATION`) plays a library item on its own
when launched with `BD_TO_AVP_NATIVE_PLAY_ITEM` set to its file name, for
`BD_TO_AVP_NATIVE_PLAY_SECONDS` (default 660). It pauses at
`BD_TO_AVP_NATIVE_PAUSE_AT` (120) for `BD_TO_AVP_NATIVE_PAUSE_SECONDS` (10),
switches audio at `BD_TO_AVP_NATIVE_AUDIO_SWITCH_AT` (240) and seeks 10 seconds
back at `BD_TO_AVP_NATIVE_SEEK_BACK_AT` (300). It prints `NATIVE_PLAY` lines and
writes `native-playback-<file name>.json` to Documents, with position, buffering,
frames, CPU, thermal state and memory each second, the renderer's dropped-frame
count, and the window and video sizes.

## Native Blu-ray Audio

Native Blu-ray playback (#711) decodes the disc's own audio on the headset. The
components live in `macos/BDToAVPPlayer/NativeAudio`, and native playback feeds them
through `NativeBlurayPlayback`. No licensing fees are paid, so
lossless Dolby and DTS layers are not decoded
([decision](https://github.com/cbusillo/BD_to_AVP/issues/1112#issuecomment-6391365574)).
The audio list names what actually plays, for example **English (DTS core)**.

| Disc format | What plays | Decoder | List label |
|---|---|---|---|
| LPCM | All channels | Built in | PCM |
| Dolby Digital (AC-3) | All channels | Apple AudioToolbox | Dolby Digital |
| Dolby Digital Plus (E-AC-3) | Independent and dependent substreams, up to 7.1 | Apple AudioToolbox | Dolby Digital Plus |
| Dolby TrueHD | The AC-3 core every Blu-ray TrueHD track carries; the lossless layer is skipped | Apple AudioToolbox | Dolby Digital core |
| DTS | The DTS core | FFmpeg `dca` | DTS |
| DTS-HD High Resolution, DTS-HD Master Audio | The DTS core; the extension substream is skipped | FFmpeg `dca` with `core_only` | DTS core |
| DTS-HD secondary (DTS Express) | Not listed: it has no core | None | None |

FFmpeg is linked as `macos/Vendor/FFmpegDCA.xcframework`, a dynamic framework
holding FFmpeg 9.0.2's `libavcodec` and `libavutil` with only the `dca` decoder,
plus the wrapper in `native/ffmpeg_dca`. It is built without `--enable-gpl`,
`--enable-version3` or `--enable-nonfree`, so it is LGPL-2.1-or-later; the build
script checks FFmpeg's own license string, and its license text ships inside the
framework and in `macos/Vendor/FFmpegDCA-notices`. Rebuild it from the pinned
source in `vendor/ffmpeg-dca-visionos.toml` with:

```sh
uv run python scripts/build_ffmpeg_dca_visionos.py
```

The source reader hands audio to `HeadsetAudioPlayer` as complete PES packets
(`BluRayPESPacket`: PID, stream ID, `stream_id_extension`, 33-bit PTS, payload),
through `BluRayPESPacketSink`, with the title's audio streams described by
`BluRayAudioStreamDescriptor` (PID, coding type and language from the playlist).
`TransportStreamPESReader` builds those packets from M2TS bytes. On Blu-ray the
DTS core and the AC-3 part of E-AC-3 use PES extension 0x71, DTS-HD and E-AC-3
extension data use 0x72, and a TrueHD track's AC-3 core uses 0x76.

`HeadsetAudioPlayer` decodes the selected track to 32-bit float PCM and enqueues
it to an `AVSampleBufferAudioRenderer` that it adds to the caller's
`AVSampleBufferRenderSynchronizer`; the native video renderer joins the same
synchronizer. `BluRayPresentationTimeline` maps PTS to synchronizer time with the
title's first video PTS as zero, unwrapping the 33-bit counter, and both
renderers use it. AudioToolbox trims its decoder delay from its first output, so
Dolby output is timed by its own sample count rather than by each input frame.
The player keeps about a second of every other audio track behind the playhead.
Selecting another track flushes the renderer from about 100 ms ahead and
continues with the new track on the next frame boundary, without stopping the
clock. `wantsMoreData` tells the reader when about two seconds are queued.

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

CI pins the release Xcode, regenerates the project, and always runs
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

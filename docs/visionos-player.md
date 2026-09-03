# visionOS Player

`BDToAVPPlayer` is the standalone visionOS 26 application for browsing and
playing finalized 3D movies or an explicitly paired live relay on Apple Vision
Pro. It is separate from the macOS converter and from `SpatialPlaybackProbe`,
which remains the qualification-only validator.

## Product Scope

- One plain SwiftUI window contains a split-view library, modal movie details,
  and the player.
- The library presents an **On My Vision Pro** source sidebar and a **Your
  movies** collection with Posters and Files modes, format filtering, title or
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
- The Live Relay panel discovers Macs through Bonjour, fetches a short-lived
  challenge, accepts the single-use code shown by the Mac app, and starts an
  authenticated MV-HEVC EVENT-HLS asset without requiring a hostname or cloud
  service.

## Live Relay

Relay sessions are source-agnostic: the wire contract carries session,
playlist, media, and playback state without MakeMKV-, SSIF-, AACS-, BD+-, or
encryption-specific fields. Pairing derives directional request keys and a
separate media capability. Every protected request binds the actual HTTP method,
raw request target, body, timestamp, and fresh nonce; replayed, expired,
reflected, unpaired, and capability-free requests fail closed.

The player rewrites the playlist, initialization map, and media segment URLs to
an app-owned resource-loader scheme so AVFoundation cannot bypass authenticated
loading. A previously paired client can reconnect while the session remains
unexpired. Wrong codes preserve the current challenge until the Mac exhausts
its bounded attempt budget, while the text field clears the submitted code
before the network attempt completes.

The current local-network transport provides authenticated integrity and replay
protection, not confidentiality: HTTP media bodies and the short-lived media
capability are visible to an on-path LAN observer. This is an explicit limit of
the synthetic/decrypted relay slice. Any adapter carrying content whose threat
model requires confidentiality must add a separately reviewed encrypted
transport without weakening the existing request authentication.

The authenticated playlist snapshot supplies the retained window. The player
refreshes that window during playback, moves the scrubber floor forward when
history is evicted, and explains when a requested seek is before retained
history or ahead of produced media. Session expiry and unpaired responses stop
remote playback and require a fresh pairing.

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

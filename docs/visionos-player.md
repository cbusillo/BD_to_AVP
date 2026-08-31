# visionOS Player

`BDToAVPPlayer` is the standalone visionOS 26 application for browsing and
playing finalized 3D movies on Apple Vision Pro. It is separate from the macOS
converter and from `SpatialPlaybackProbe`, which remains the qualification-only
validator.

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

## Source Access

Imported files receive persistent bookmark data. Playback resolves the bookmark
and keeps one balanced security-scoped access lease open for the complete player
session. The lease closes when playback finishes, preparation is replaced, or
the session is destroyed.

Files in the app's Documents directory use the same library and bookmark path as
Files-picker imports. If a source moves or disappears, the details view reports
that state and offers a locate flow instead of attempting playback.

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
RealityKit entity, and one source lease. Its `VideoPlayerComponent` requests stereo viewing, screen spatial
video mode, and portal immersive viewing mode. A native glass ornament attached
below the same window keeps controls off the stereo image and provides:

- play and pause;
- 10-second backward and 30-second forward seeks;
- a position slider and elapsed/duration display;
- audio-track selection;
- subtitle selection, including Off;
- eye-order swapping for side-by-side and over-under playback; and
- Done, which persists progress and releases the session.

The ornament remains visible while playback is paused, loading, failed, or being
scrubbed, and automatic hiding is disabled while VoiceOver or Switch Control is
active. During uninterrupted playback it hides after three seconds. Pinching the
video surface reveals the controls again.

The app saves in-progress playback positions and restores them when the same
library item is opened again. Any non-active scene phase pauses playback and
saves progress. Completed or near-completed playback is cleared instead of
resuming at the end.

Packed stereo playback uses a visionOS 26 custom video composition with one
stable two-buffer output contract: output zero is always tagged as the left eye
and output one as the right eye. A custom composition instruction carries the
packed layout and requested eye order, and the compositor swaps source regions
into those fixed semantic outputs. Rendering copies native biplanar YUV planes
and propagates source color attachments instead of converting through device
RGB. Eye-order changes replace only the immutable `AVPlayerItem` while retaining
the `AVPlayer`, RealityKit entity, source lease, playback time and intent, audio
selection, and subtitle selection. Composition-backed seeks wait for the newly
rendered frame before restoration completes.

MVC, network shares, relay playback, and live conversion are not implemented in
this slice.

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

The same scheme also contains `BDToAVPPlayerUITests`. Its seeded-media flow is
skipped when `PlayerLongFixture.mov` is absent from the simulator app Documents
directory; when present, it verifies direct Library → Play → Library, Library →
Details → Play, Details Done → Library, player Done → Details, replay, and
ornament auto-hide behavior.

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
validation. Tagged custom-compositor playback may also be rejected while the
same build is otherwise testable in the visionOS Simulator. The automated suite
therefore exercises the production rendering core directly with synthetic
side-by-side and over-under biplanar fixtures. It asserts normal and reversed
eye pixels, fixed output tags, native pixel format, chroma routing, and color
attachment propagation without `AVPlayer`. Physical Vision Pro remains the only
way to prove that RealityKit routes those tagged outputs to the intended eyes;
that qualification is a single instrumented gate after local tests pass.

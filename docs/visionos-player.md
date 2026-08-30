# visionOS Player

`BDToAVPPlayer` is the standalone visionOS 26 application for browsing and
playing finalized 3D movies on Apple Vision Pro. It is separate from the macOS
converter and from `SpatialPlaybackProbe`, which remains the qualification-only
validator.

## Product Scope

- One plain SwiftUI window contains the library, movie details, and player.
- The library supports poster and file views, format filtering, title or
  filename sorting, missing-source recovery, and removal.
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

The first slice plays MV-HEVC only. Format detection uses AVFoundation stereo
multiview playback characteristics; an HEVC codec tag alone is not accepted as
proof of MV-HEVC.

`MVHEVCPlayerSession` owns one `AVPlayer`, `AVPlayerItem`, RealityKit entity, and
source lease. Its `VideoPlayerComponent` requests stereo viewing, screen spatial
video mode, and portal immersive viewing mode. A glass HUD rendered inside the
same window provides:

- play and pause;
- 10-second backward and 30-second forward seeks;
- a position slider and elapsed/duration display;
- audio-track selection;
- subtitle selection, including Off; and
- Done, which persists progress and releases the session.

The app saves in-progress playback positions and restores them when the same
library item is opened again. Any non-active scene phase pauses playback and
saves progress. Completed or near-completed playback is cleared instead of
resuming at the end.

SBS, over-under, MVC, network shares, relay playback, and live conversion are
not implemented in this slice. SBS and over-under files may be classified and
shown in the library, but their details views identify playback as planned rather
than pretending they are playable.

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
comfort, long-session thermals, or headset-visible interaction. Those remain
physical Vision Pro validation boundaries.

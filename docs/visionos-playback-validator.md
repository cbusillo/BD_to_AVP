# visionOS Playback Validator

`SpatialPlaybackProbe` is the internal Xcode target for **BD to AVP Playback Check**, a guided visionOS validator for finalized preview and output movies. It contains no Python runtime, ripping tools, conversion engine, network client, or release controls.

## Operator Contract

The validator asks the person wearing Apple Vision Pro to do four things:

1. Choose one finished movie.
2. Select **Run Playback Check**.
3. Watch three short playback sections and answer two plain-language questions.
4. Share the generated report when evidence is needed.

Nothing in the app publishes a build, approves a GitHub deployment, merges code, or authorizes a release. A passing report is playback evidence for the named movie and device; it is not a release approval by itself.

Movies selected through Files are copied into the app's cache so playback can continue after the picker closes. The temporary copy is removed when replaced or when the app next launches. Nothing is uploaded.

## What It Checks Automatically

- `VTIsStereoMVHEVCDecodeSupported()` on the current device.
- `AVPlayerItem.status == .readyToPlay` for the selected movie.
- `VideoPlayerComponent.currentRenderingStatus == .ready`.
- Actual stereo, spatial, portal presentation instead of flat-screen fallback.
- Beginning, middle, and end seeks, including preservation of spatial presentation after every seek.
- Available audio and subtitle choices for manual review under **Technical details**.

After the automatic sequence, the validator asks only:

- Did the picture stay visible during the entire check?
- Did the scene look three-dimensional rather than flat?

**Not sure** is a valid answer and produces **One result needs review** rather than a false pass.

## One-Window Design

The movie, playback controls, guided instructions, observations, result, and collapsed diagnostics remain in one volumetric window. The RealityKit player entity is never moved between windows or into an immersive space. This avoids the blank-video and duplicate-window lifecycle failures found in the earlier experimental probe.

Technical terms such as `Stereo · Spatial · Portal` appear only under **Technical details**. The primary flow uses plain language and exposes one next action at a time.

## Result Meanings

- **Playback check passed**: every automatic check passed and both visible observations were **Yes**.
- **One result needs review**: the automatic checks did not fail, but at least one observation was **Not sure** or a check did not reach a final pass state.
- **Playback check found a problem**: an automatic check failed or either visible observation was **No**.

The shared JSON report contains a schema version, validator version and build, visionOS version, filename, full-file SHA-256 fingerprint, file size, duration, media-option counts, automatic check details, observations, and result. It intentionally omits the source file path. The fingerprint binds release evidence to the exact movie even when an automated device transfer names it `Probe.mov`.

## Build

Create a six-second finalized MV-HEVC fixture with English audio and subtitles:

```bash
scripts/create_spatial_playback_fixture.sh
```

Generate the project and build for the simulator:

```bash
uv run python scripts/native_app.py generate
xcodebuild build \
  -project macos/BluRayToVisionPro.xcodeproj \
  -scheme SpatialPlaybackProbe \
  -destination 'platform=visionOS Simulator,name=Apple Vision Pro' \
  -derivedDataPath macos/build/SpatialPlaybackProbeDerivedData \
  CODE_SIGNING_ALLOWED=NO
```

Run unit and simulator UI tests:

```bash
xcodebuild test \
  -project macos/BluRayToVisionPro.xcodeproj \
  -scheme SpatialPlaybackProbe \
  -destination 'platform=visionOS Simulator,name=Apple Vision Pro' \
  -derivedDataPath macos/build/SpatialPlaybackProbeDerivedData \
  CODE_SIGNING_ALLOWED=NO
```

The simulator proves the app builds, launches as one guided window, and explains the first action. It cannot prove stereoscopic presentation. A physical Apple Vision Pro remains required for release playback evidence.

For a physical device, supply a development team and the connected device identifier:

```bash
xcodebuild build \
  -project macos/BluRayToVisionPro.xcodeproj \
  -scheme SpatialPlaybackProbe \
  -destination 'platform=visionOS,id=<device-id>' \
  -derivedDataPath macos/build/SpatialPlaybackProbeDerivedData \
  -allowProvisioningUpdates \
  DEVELOPMENT_TEAM=<team-id>
```

## Automated Physical-Device Setup

Install the signed app, copy a finalized movie into its Documents container as `Probe.mov`, and launch the UI test or app with `BD_TO_AVP_PROBE_AUTORUN=1`:

```bash
xcrun devicectl device copy to \
  --device <device-id> \
  --source /absolute/path/to/finalized.mov \
  --destination Documents/Probe.mov \
  --domain-type appDataContainer \
  --domain-identifier com.shinycomputers.bd-to-avp.spatial-playback-probe
```

Autorun starts the automatic sequence after the player is ready. It stops at the two human observations; automation does not fabricate visible playback answers. The physical UI test requires all automatic checks to pass and verifies that the observation screen is presented.

## Audio Validation Matrix

Create the four representative finalized movies used by the physical audio gate:

```bash
uv run python scripts/create_spatial_audio_validation_fixtures.py \
  --output "$HOME/Movies/BD to AVP Audio Validation" \
  --force
```

The generator exercises production audio preparation and final muxing. It produces Automatic AAC copy, Automatic AAC fallback, Convert AAC, and PCM cases with English 5.1 audio, French stereo audio, English subtitles, synchronized flashes, and a machine-readable `manifest.json`.

Run **Playback Check** for each fixture. After the guided result, expand **Technical details** to switch audio and subtitle choices using the generated `CHECKLIST.md`.

## Structured Evidence

Structured events use the `BD_TO_AVP_PLAYBACK_PROBE` prefix. `automated_probe_complete` records the automatic result after all three seeks. `guided_validation_complete` records the final result and the two human observations.

Physical acceptance requires:

- `automated_probe_complete` with `result=pass`.
- Visible video throughout beginning, middle, and end playback.
- Comfortable, non-inverted depth.
- A shared report whose result matches the operator's observations.

Live playback while the conversion pipeline is still writing a movie remains out of scope. The validator consumes immutable finalized artifacts only.

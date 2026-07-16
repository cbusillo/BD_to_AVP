# visionOS Spatial Playback Probe

`SpatialPlaybackProbe` is an isolated visionOS companion target for validating finalized BD to AVP preview and output movies with native Apple playback APIs. It contains no Python runtime, ripping tools, or conversion engine.

## What It Proves

- `VTIsStereoMVHEVCDecodeSupported()` on the target device.
- `AVPlayerItem.status == .readyToPlay` for the transferred movie.
- `VideoPlayerComponent.currentRenderingStatus == .ready`.
- Requested stereo viewing, `.spatial` presentation, and portal rendering versus the actual RealityKit modes.
- Beginning, middle, and end seeks while preserving the reported spatial mode.
- Available audio and subtitle choices.

The simulator is useful for builds and flat playback behavior, but it is not acceptance evidence for stereoscopic presentation. A physical Apple Vision Pro is required.

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

The `SpatialPlaybackProbeUITests` target performs the user-initiated **Open Spatial View** action required by visionOS and verifies the reported stereo, spatial, portal presentation on a physical headset. Simulator runs skip this acceptance test. The headset must allow Xcode to enter UI automation mode; a timeout while enabling automation is an environment blocker, not spatial-playback evidence.

## Generate the Audio Validation Matrix

Create the four representative finalized movies used by the physical audio gate:

```bash
uv run python scripts/create_spatial_audio_validation_fixtures.py \
  --output "$HOME/Movies/BD to AVP Audio Validation" \
  --force
```

The generator exercises the production audio preparation and final-mux functions. It produces:

- `01-Automatic-AAC-Copy.mov`: two qualified AAC tracks whose packet payloads must remain unchanged.
- `02-Automatic-AAC-Fallback.mov`: AC-3 plus AAC input that must emit the structured fallback warning and convert the whole selected set.
- `03-Convert-AAC.mov`: E-AC-3 plus FLAC input that must convert both tracks to AAC.
- `04-PCM.mov`: E-AC-3 plus FLAC input that must extract both tracks as 24-bit PCM.

Each movie is 24 seconds long with a default English 5.1 track, a French alternate stereo track, selectable English subtitles, and a white flash synchronized to each channel beep. The generator fails unless codec, language, channel-layout, packet/sample integrity, warning, subtitle, and left/right MV-HEVC split checks pass. `manifest.json` records the machine-verifiable evidence, and `CHECKLIST.md` contains the headset operator gate.

After installing the signed app, copy the matrix into its Documents container and place the first fixture at the autorun path:

```bash
xcrun devicectl device copy to \
  --device <device-id> \
  --source "$HOME/Movies/BD to AVP Audio Validation" \
  --destination "Documents/Audio Validation" \
  --domain-type appDataContainer \
  --domain-identifier com.shinycomputers.bd-to-avp.spatial-playback-probe

xcrun devicectl device copy to \
  --device <device-id> \
  --source "$HOME/Movies/BD to AVP Audio Validation/01-Automatic-AAC-Copy.mov" \
  --destination Documents/Probe.mov \
  --domain-type appDataContainer \
  --domain-identifier com.shinycomputers.bd-to-avp.spatial-playback-probe
```

## Open a Finalized Movie

Launch the app on Apple Vision Pro and choose **Open Preview…**. The file importer accepts a movie from Files, copies it into the app container, and keeps playback independent from the source file's security-scoped lifetime. The default volumetric window renders through `VideoPlayerComponent`; **Open Spatial View** requests a mixed immersive-space portal for focused review.

For automated development-device evidence, install the built app, copy a finalized movie into its data container as `Documents/Probe.mov`, and launch with `BD_TO_AVP_PROBE_AUTORUN=1`. The visible **Open Spatial View** action—or the physical-device UI test—must enter the immersive portal before the automated seek sequence begins and `.spatial` can be accepted.

Structured events are printed with the `BD_TO_AVP_PLAYBACK_PROBE` prefix. The terminal event is `automated_probe_complete`; acceptance requires `result=pass`, `rendering_status=ready`, a stereo/spatial actual presentation, and all three seeks to finish.

## Interpretation

- `.spatial` is acceptance for stereoscopic presentation.
- `.screen` is a useful diagnostic fallback but does not satisfy the spatial-mode criterion.
- Missing audio or subtitle choices are fixture limitations, not decode failures. Use a representative final mux before closing the media-selection criteria.
- Live playback of the MOV while the conversion pipeline is still writing it remains out of scope. This target consumes immutable finalized artifacts only.

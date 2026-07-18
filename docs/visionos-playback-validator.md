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
- Actual stereoscopic playback and whether the presentation matches the selected validation expectation.
- Beginning, middle, and end seeks, including preservation of stereoscopic playback and required spatial treatment.
- Available audio and subtitle choices for manual review under **Technical details**.

After the automatic sequence, the validator asks only:

- Did the picture stay visible during the entire check?
- Did the scene look three-dimensional rather than flat?

**Not sure** is a valid answer and produces **One result needs review** rather than a false pass.

## One-Window Design

The movie, playback controls, guided instructions, observations, result, and collapsed diagnostics remain in one volumetric window. The RealityKit player entity is never moved between windows or into an immersive space. This avoids the blank-video and duplicate-window lifecycle failures found in the earlier experimental probe.

Technical terms such as `Stereo · Spatial · Portal` appear only under **Technical details**. The primary flow uses plain language and exposes one next action at a time.

## Presentation Expectations

The validator has two intentional expectations:

- **Stereo release movie** is the default. A converted Blu-ray movie should report `Stereo · Screen` because its capture baseline, field of view, and disparity can vary between shots.
- **Spatial calibration fixture** is enabled with `BD_TO_AVP_PROBE_EXPECTED_PRESENTATION=spatial`. The controlled fixture should report `Stereo · Spatial · Portal` and proves that the validator can detect RealityKit spatial treatment.

Apple requires spatial metadata to describe the true, constant properties of the cameras that captured the media. Its guidance specifically notes that a movie or TV show captured with changing camera geometry may not be appropriate for spatial presentation and should remain stereo. BD to AVP therefore does not invent a camera baseline for normal Blu-ray output. See [Creating spatial photos and videos with spatial metadata](https://developer.apple.com/documentation/imageio/creating-spatial-photos-and-videos-with-spatial-metadata) and [Converting side-by-side 3D video to multiview HEVC and spatial video](https://developer.apple.com/documentation/avfoundation/converting-side-by-side-3d-video-to-multiview-hevc-and-spatial-video).

## Result Meanings

- **Playback check passed**: every automatic check passed and both visible observations were **Yes**.
- **One result needs review**: the automatic checks did not fail, but at least one observation was **Not sure** or a check did not reach a final pass state.
- **Playback check found a problem**: an automatic check failed or either visible observation was **No**.

The app automatically writes a named JSON report plus `Latest-Playback-Report.json` under its Documents directory. **Share JSON Report** sends that file as a `.json` attachment instead of untyped text. The schema-3 report contains the expected presentation, validator version and build, visionOS version, filename, full-file SHA-256 fingerprint, file size, duration, media-option counts, actual viewing/spatial/immersive modes, automatic check details, observations, and result. It intentionally omits the source file path. The fingerprint binds release evidence to the exact movie even when an automated device transfer names it `Probe.mov`.

## Build

Create a six-second finalized MV-HEVC fixture with English audio and subtitles:

```bash
scripts/create_spatial_playback_fixture.sh
```

The fixture contains three labeled depth markers: blue should appear behind the screen plane, green on the screen plane, and red in front. It uses controlled 64 mm, rectilinear camera metadata so RealityKit recognizes it as spatial media. This gives the operator an unambiguous depth-order check and a deterministic `Stereo · Spatial · Portal` calibration asset without changing production Blu-ray metadata.

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

Install the signed app and copy a finalized movie into its Documents container as `Probe.mov`:

```bash
xcrun devicectl device copy to \
  --device <device-id> \
  --source /absolute/path/to/finalized.mov \
  --destination Documents/Probe.mov \
  --domain-type appDataContainer \
  --domain-identifier com.shinycomputers.bd-to-avp.spatial-playback-probe
```

For a normal Blu-ray release movie, launch the app with the default stereo expectation:

```bash
xcrun devicectl device process launch \
  --device <device-id> \
  --terminate-existing \
  --environment-variables '{"BD_TO_AVP_PROBE_ASSET":"Probe.mov","BD_TO_AVP_PROBE_AUTORUN":"1"}' \
  com.shinycomputers.bd-to-avp.spatial-playback-probe
```

For the generated calibration fixture, require spatial portal presentation:

```bash
xcrun devicectl device process launch \
  --device <device-id> \
  --terminate-existing \
  --environment-variables '{"BD_TO_AVP_PROBE_ASSET":"Probe.mov","BD_TO_AVP_PROBE_AUTORUN":"1","BD_TO_AVP_PROBE_EXPECTED_PRESENTATION":"spatial"}' \
  com.shinycomputers.bd-to-avp.spatial-playback-probe
```

Autorun starts the automatic sequence after the player is ready. It stops at the two human observations; automation does not fabricate visible playback answers. The physical UI test uses the spatial calibration expectation, requires all automatic checks to pass, and verifies that the observation screen is presented.

After the operator selects **Finish Check**, retrieve the report directly without asking them to save share-sheet text manually:

```bash
xcrun devicectl device copy from \
  --device <device-id> \
  --source Documents/PlaybackValidatorReports/Latest-Playback-Report.json \
  --destination /absolute/path/to/playback-report.json \
  --domain-type appDataContainer \
  --domain-identifier com.shinycomputers.bd-to-avp.spatial-playback-probe
```

Use both expectations before release. A normal finalized movie proves the production stereo, audio, subtitle, and seek path. The synthetic calibration fixture separately proves that the app and device can enter RealityKit spatial portal presentation. Keeping those contracts separate prevents either missing spatial treatment or fabricated camera metadata from producing misleading release evidence.

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
- `expected_presentation=stereo` for normal finalized Blu-ray movies, or `expected_presentation=spatial` for the generated calibration fixture.
- Visible video throughout beginning, middle, and end playback.
- Comfortable, non-inverted depth.
- A shared report whose result matches the operator's observations.

Live playback while the conversion pipeline is still writing a movie remains out of scope. The validator consumes immutable finalized artifacts only.

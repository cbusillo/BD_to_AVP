# Segmented MV-HEVC Live-Relay Feasibility

## Format Decision

Issue #716 adds a development-only segmented output mode to the existing native
MV-HEVC encoder:

- Input remains progressive 8-bit 4:2:0 side-by-side Y4M and remains video-only.
- `--hls-directory DIR` is exclusive with the existing `--output FILE` MOV mode.
- The writer is constructed with `AVAssetWriter(contentType: .mpeg4Movie)`,
  `AVFileTypeProfileMPEG4AppleHLS`, a positive preferred segment interval, and
  an `AVAssetWriterDelegate`.
- The delegate atomically publishes `init.mp4`, numbered `segment-*.m4s` media
  segments, and `media.m3u8`. The playlist is first published when the first
  media segment is durable, remains HLS `EVENT`, and gains `#EXT-X-ENDLIST`
  after successful writer completion.
- `#EXT-X-TARGETDURATION` is latched before the first playlist publication and
  cannot change while clients reload the EVENT playlist. A segment exceeding
  that fixed target fails the encode instead of silently changing the contract.

The Apple HLS profile is the selected format rather than a separately branded
CMAF profile because the AVFoundation SDK explicitly documents this profile as
streaming-suitable and documents its initialization plus separable output as
fMP4 `moov` and `moof`/`mdat` segments. This is an HLS/CMAF-compatible segment
shape, not a network relay implementation.

## Fallback Ladder

1. Use segmented HLS only when `--capability-probe` reports
   `segmented_hls_mv_hevc_encode_supported: true` and the target playback path
   accepts the generated MV-HEVC representation.
2. Use the existing direct `--output FILE` MOV path when segmented capability,
   compatibility, or throughput is insufficient. Its behavior is unchanged.
3. Use the established file conversion and packaging route when direct
   MV-HEVC itself is unavailable.

The encoder deletes a failed new HLS directory. With `--overwrite`, it
atomically moves an existing directory aside, restores it if encoding fails,
and removes the previous directory only after successful finalization. There
is intentionally no resumption protocol, network upload, audio muxing, UI, or
source-specific behavior in this capability.

## Commands

Build the encoder:

```sh
uv run python scripts/build_mv_hevc_encoder_macos.py \
  --output build/mv-hevc-encoder/mv-hevc-encoder
```

Encode a bounded local Y4M fixture to segmented HLS:

```sh
build/mv-hevc-encoder/mv-hevc-encoder \
  --hls-directory /tmp/mv-hevc-hls \
  --segment-duration 2 \
  --expected-frames 240 < stereo.y4m
```

Measure a bounded Y4M throughput run and receive one JSON object on stdout:

```sh
uv run python scripts/measure_segmented_mv_hevc_throughput.py \
  --y4m stereo.y4m \
  --output-directory /tmp/mv-hevc-hls-throughput \
  --max-frames 240
```

To exercise the optional real-media chain, first build `ssif_probe`, set the
private ISO location, and run the bounded Rainforest path. It composes
`ssif_probe stream-mvc -> edge264_test -> FFmpeg normalizer -> encoder`.

```sh
uv run python scripts/build_ssif_probe_macos.py
BD_TO_AVP_RAINFOREST_ISO=/absolute/path/to/Rainforest.iso \
uv run python scripts/measure_segmented_mv_hevc_throughput.py \
  --rainforest \
  --output-directory /tmp/rainforest-mv-hevc-hls \
  --max-frames 240
```

The JSON record names hardware and storage fingerprints, hashes every invoked
tool, includes the encoder summary, measures media duration and wall time, and
reports `realtime_ratio` as media seconds divided by elapsed seconds.

## Evidence Status

The implementation has focused compile, CLI-contract, source-contract, and
hardware-gated synthetic segmented-output coverage. The HLS capability probe
uses the same stereo output settings and segmented writer configuration before
the runtime test is attempted.

On September 3, 2026, the bounded Rainforest path processed 240 frames (10.01
seconds) end to end on an Apple M4 Max in 3.8897 seconds, for a `2.5735x`
realtime ratio at 20 Mbps. The resulting five-segment HLS representation was
accepted as a ready `AVPlayerItem` with a 10.01-second duration. That headless
probe did not decode or render a visible frame (`video_tracks=0`, current time
`0.0`), so it is readiness evidence rather than physical playback evidence.
Packaged FFprobe provides the stronger format proof: HEVC view IDs `0,1`, view
positions `1,2`, multilayer disposition, and stereo side data with the left eye
primary.

Evidence is stored outside the repository:

- `/Volumes/Docker-External/BD_to_AVP_artifacts/issue-716/rainforest-240-fixed-20260903.json`
  (`ea6300b7cda1dde49b3b8b6af7c863a215991828b2e002c543727059ccc0c59a`)
- `/Volumes/Docker-External/BD_to_AVP_artifacts/issue-716/rainforest-240-fixed-20260903.ffprobe.json`
  (`7017ad9dbbd0dcc740113cb61121adb4c0e7a05489f90321283c0e521320fedd`)
- `/Volumes/Docker-External/BD_to_AVP_artifacts/issue-716/rainforest-240-fixed-20260903.avplayer.txt`
  (`18e7081c0bc9f55ac1abaefd61349378bce94a9a71f63e676ed2603424c1d1b3`)

This proves the bounded format and throughput gate on the named reference Mac;
it does not replace #712's Vision Pro session proof, #713's ten-minute relay
slice, or #719's full-length physical qualification.

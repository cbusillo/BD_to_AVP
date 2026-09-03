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
  segments, and `media.m3u8`. The active playlist is HLS `EVENT`; after a
  successful writer completion it is rewritten as `VOD` with `#EXT-X-ENDLIST`.

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
uv run python scripts/build_ssif_probe_macos.py \
  --output build/ssif-probe/ssif_probe
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

No real-media Rainforest throughput run or playback qualification is recorded
in this document. Therefore this capability does **not** claim that any
real-media live-relay threshold has passed. Run the bounded probe above and
preserve its JSON output with the relevant hardware and playback evidence
before changing that disposition.

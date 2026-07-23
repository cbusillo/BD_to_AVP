# Direct decoded-stereo to MV-HEVC feasibility

Issue #347 asks whether the decoded stereo stream can become a final Apple-compatible MV-HEVC movie without first
writing left and right HEVC eye movies. The bounded prototype proves that this boundary is viable on Apple Silicon,
but it is not yet qualified as the production default.

## Decision

Use a native Swift helper built on `AVAssetWriter` and tagged pixel-buffer groups. The helper accepts normalized,
progressive, side-by-side 8-bit 4:2:0 Y4M on standard input and writes one MV-HEVC MOV. Keep FFmpeg immediately
upstream for the existing crop, deinterlace, eye-swap, resolution, and frame-rate transforms.

The proposed normal path is:

```text
source FFmpeg -> edge264_test -> FFmpeg geometry normalizer -> mv-hevc-encoder -> <name>_MV-HEVC.mov
```

This removes the two eye HEVC movies and their merge encode. It does not replace the later optional upscale or final
MP4Box audio/subtitle mux.

## Encoder boundary inventory

| Candidate | Result | Reason |
| --- | --- | --- |
| `AVAssetWriter` tagged pixel-buffer groups | Selected | Accepts synchronized left/right pixel buffers, emits MV-HEVC layer/view metadata, owns MOV finalization, and requires no seekable input. |
| Direct `VTCompressionSession` plus a custom MOV writer | Rejected for the first implementation | The codec boundary is viable, but recreating multiview sample grouping and Apple spatial container metadata adds avoidable muxing risk. |
| FFmpeg as the final MV-HEVC writer | Rejected | The bundled FFmpeg remains useful for normalization but does not expose the required Apple tagged multiview writer boundary. |
| `spatial-media-kit-tool merge` | Retained as fallback | It is proven in the current product, but it requires two materialized eye movies and therefore cannot satisfy the direct-streaming objective. |
| File-backed side-by-side input through `AVAssetReader` | Rejected for the normal path | It follows Apple's sample topology but adds a large decoded or packed intermediate that the existing Y4M pipe already avoids. |

The selected API is available from macOS 26. The prototype builds as an arm64 executable targeting macOS 26.0 and
links only Apple AVFoundation, CoreMedia, CoreVideo, and VideoToolbox frameworks plus system Swift libraries.

## Prototype contract

`native/mv_hevc_encoder/MVHEVCEncoder.swift` implements the bounded encoder.

- Input is progressive `C420`, `C420jpeg`, `C420mpeg2`, or `C420paldv` Y4M. Unknown interlace and high-bit-depth
  formats fail before output replacement.
- The left and right halves become IOSurface-backed, video-range NV12 pixel buffers tagged with video layer IDs `0`
  and `1` and left/right stereo-view tags.
- The writer declares both stereo eyes, a left hero eye, rectilinear projection, horizontal field of view, disparity,
  and optional camera baseline.
- BT.709 primaries, transfer function, matrix, limited range, source frame rate, per-eye dimensions, and eye order are
  preserved by the bounded fixture.
- The final file is written under a private partial name and moved into place only after `AVAssetWriter` completes.
  A failed `--overwrite` attempt preserves the prior destination.
- Standard error carries bounded JSONL `encoder.ready` and `encoder.progress` records. The first accepted frame and
  each subsequent 120-frame boundary produce progress without depending on MOV file growth.
- Standard output contains one completion summary. `SIGINT` and `SIGTERM` cancel with exit status 130.

The synthetic fixture supplies a 64 mm baseline only to prove metadata serialization. A product route must not invent
a camera baseline for Blu-ray content; omit it unless a calibrated source value exists. Existing FOV and zero-disparity
policy can be passed directly.

## Bounded comparison

The qualification command generated one two-second, 48-frame, 24 fps stereo source with a 16-pixel disparity. Both
paths received the same per-eye images and a 4 Mbps aggregate bitrate budget. The current path used two 2 Mbps
VideoToolbox eye encodes followed by merge quality 75; the direct path used one 4 Mbps MV-HEVC encode.

| Measurement | Current path | Direct path |
| --- | ---: | ---: |
| Elapsed time | 0.830280 s | 0.378381 s |
| Child user CPU | 0.435242 s | 0.216164 s |
| Child system CPU | 0.113633 s | 0.063091 s |
| Final movie size | 165,650 bytes | 399,675 bytes |
| Peak eye-intermediate bytes | 534,869 bytes | 0 bytes |
| Left-eye matched SSIM | 0.912566 | 0.914648 |
| Right-eye matched SSIM | 0.909273 | 0.915010 |

The direct run took 45.6% of the current path's elapsed time and eliminated both eye intermediates. Its final file was
234,025 bytes larger under these non-equivalent quality controls. That size delta is descriptive only: the current
merge performs a second lossy encode governed by quality 75, so this fixture cannot establish a like-for-like size
regression threshold. The direct result slightly exceeded the current path's decoded per-eye SSIM, and each same-eye
score remained clearly above its crossed-eye score.

Both paths use VideoToolbox hardware HEVC encoders, and the host reports stereo MV-HEVC encode support. The bounded
CLI probe does not have a trustworthy per-process GPU counter, so numeric GPU utilization remains unproven. A release
qualification should add an approved Instruments or equivalent measurement rather than infer GPU load from codec
selection.

## Container and playback validation

The direct fixture passed all locally automatable checks:

- FFprobe reports one `hvc1` HEVC stream, 320x180 per eye, 24 fps, 48 decoded frames, limited-range BT.709 signaling.
- MP4Box reports `hvcC`, `lhvC`, `vexu`, `eyes`, `proj`, and `hfov`. The current toolkit baseline does not emit a
  separate `proj` box; the direct candidate does.
- `spatial-media-kit-tool split` produces one 48-frame left movie and one 48-frame right movie with the expected eye
  order and dimensions.
- `scripts/verify_apple_media.py` passes its AVFoundation/`avconvert` compatibility check.
- FFmpeg decodes frames after beginning, middle, and near-end seeks.

These checks do not prove stereoscopic presentation in the headset. The physical Apple Vision Pro workflow in
`docs/visionos-playback-validator.md` remains mandatory before the route can be declared production-ready.

## Failure, cancellation, and backpressure

The receiver uses nonblocking append attempts and waits asynchronously when `AVAssetWriter` applies backpressure.
POSIX reads consume pipe bytes as soon as they arrive instead of waiting for a large Foundation read buffer. Tests
cover successful encoding, unsupported chroma, truncated input, excess frames, destination preservation, signal
cancellation, and partial-file removal.

The qualification harness waits for both producer and consumer, kills and reaps both on timeout, and distinguishes an
upstream truncation from a consumer rejection. When a consumer rejection causes upstream SIGPIPE, the consumer error
wins. Product integration should continue using the existing `ProcessPipelineRunner`, process groups, cancellation
token, and splitter-signal prioritization rather than introducing a second supervision model.

## Restart and fallback

The direct route is eligible only for a normal MV-HEVC run that starts no later than `create_left_right_files`, does not
request `--keep-files`, does not request the software encoder, and does not require an external eye-file workflow.

- Keep the existing left/right-plus-merge path for explicit fallback and compatibility.
- Keep `combine_to_mv_hevc` restart behavior file-backed; it requires durable eye movies and cannot enter the direct
  path.
- A missing helper or failed capability preflight may select the legacy path before input is consumed. A mid-encode
  failure must remain a visible failure; silently restarting through another lossy path would hide cost and quality
  changes.
- Do not reuse `left_right_bitrate` for the new route. Product integration needs one visible final MV-HEVC bitrate or
  quality control, while the legacy route retains its existing per-eye bitrate and merge-quality controls.
- Keep existing native splitter retry behavior. A direct-encoder failure is not evidence that the splitter should be
  retried in single-threaded mode.

The existing stage enum should not be renumbered. A product implementation can execute the direct helper during
`create_left_right_files`, produce the existing `_MV-HEVC.mov` boundary, skip `combine_to_mv_hevc` for that run, and
continue through upscale and final mux without changing later artifact names.

## Packaging and release implications

- Build the helper with the release Xcode toolchain for arm64 and minimum macOS 26.0.
- Bundle it as a nested executable, sign it before signing the containing app, and include it in notarization and
  bundled-tool deployment-target checks.
- No third-party runtime, source archive, or additional license notice is required; the implementation uses Apple SDK
  frameworks.
- No production routing or app-bundle change is included in this prototype. Those changes should occur only after the
  remaining device and GPU evidence is recorded.

## Reproduction

```bash
uv run python scripts/build_mv_hevc_encoder_macos.py \
  --output build/mv-hevc-encoder/mv-hevc-encoder

uv run python scripts/qualify_direct_mv_hevc.py \
  --encoder build/mv-hevc-encoder/mv-hevc-encoder \
  --output build/direct-mv-hevc/direct.mov \
  --json-output build/direct-mv-hevc/qualification.json

uv run python -m unittest tests.test_mv_hevc_encoder -v
```

## Acceptance status

| Issue #347 criterion | Status |
| --- | --- |
| Encoder-boundary inventory | Passed |
| Final MV-HEVC fixture without eye HEVC movies | Passed |
| Eye order, dimensions, timing, color, FOV, disparity, and multiview metadata | Passed for the bounded fixture |
| Quality, size, elapsed time, CPU, GPU, and peak disk comparison | Partial: numeric GPU utilization and a like-for-like final-size threshold remain unproven |
| AVFoundation and beginning/middle/end seeks | Passed |
| Physical Apple Vision Pro validation | Pending |
| Cancellation, backpressure, failure attribution, and cleanup | Passed for the bounded prototype |
| Restart and fallback behavior | Defined |
| Runtime, license, macOS, architecture, signing, notarization, and bundle implications | Recorded |

The prototype establishes a viable architecture. It must remain non-default until numeric GPU evidence and physical
Apple Vision Pro playback evidence close the two remaining qualification gaps.

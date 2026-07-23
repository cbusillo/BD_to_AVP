# Direct decoded-stereo to MV-HEVC feasibility

Issue #347 asks whether the decoded stereo stream can become a final Apple-compatible MV-HEVC movie without first
writing left and right HEVC eye movies. The bounded prototype proves that this boundary is viable on Apple Silicon,
and the completed qualification clears it for product integration. The current product remains on the legacy path
until that integration is implemented and shipped.

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

### Quality-matched size

`scripts/qualify_mv_hevc_quality_match.py` removes the target-setting mismatch by searching for a direct bitrate whose
minimum decoded same-eye SSIM exceeds the current path's median by at least 0.002. It then repeats both paths three
times with the same source fixture and requires every direct run to preserve that margin.

| Three-run median | Current path | Quality-matched direct path |
| --- | ---: | ---: |
| Target bitrate | 4.0 Mbps aggregate eye target plus merge quality 75 | 0.543750 Mbps final target |
| Effective final bitrate | 0.662600 Mbps | 0.599912 Mbps |
| Final movie size | 165,650 bytes | 149,978 bytes |
| Minimum same-eye SSIM | 0.909273 | 0.911418 |
| Minimum eye-order SSIM margin | 0.338076 | 0.347851 |

Every direct run exceeded the required quality floor of 0.911273. At that matched-quality point, the direct movie was
9.46% smaller and its worst-eye SSIM was 0.002145 higher. All three runs produced identical sizes and decoded quality
metrics. Their file hashes differed, so the result makes no byte-for-byte determinism claim about container metadata.
The like-for-like size gate therefore passes for the bounded fixture without inferring equivalence from encoder input
controls.

### GPU time

`scripts/profile_mv_hevc_gpu.py` records the bounded workloads with Xcode's Metal System Trace, captures each client
PID when it is created, exports the `metal-gpu-intervals` table, and sums exact AGX GPU interval durations per PID. A
known Metal compute workload must produce nonzero intervals in the same run before an encoder value of zero is
accepted.

On a separate two-second, 640x360-per-eye, 24 fps profiling fixture:

| GPU measurement | Current path | Direct path |
| --- | ---: | ---: |
| Client-process AGX GPU intervals | 97 | 0 |
| Client-process AGX GPU time | 1,665,914 ns | 0 ns |
| Client phase-average AGX GPU utilization | 0.156107% | 0.000000% |
| VideoToolbox-service AGX GPU time visible in the trace | 1,024,587 ns | 0 ns |
| Elapsed time | 1.067165 s | 0.446034 s |
| Child user CPU | 0.567417 s | 0.240963 s |
| Child system CPU | 0.140102 s | 0.083963 s |

The positive control recorded 22,257 intervals, 177,631,319 ns of non-overlapping AGX work, and 2.168082%
phase-average utilization. All client-process AGX time in the current window was attributed to
`spatial-media-kit-tool`; its FFmpeg process used none. One `VTDecoderXPCService` visible in that window contributed
1,024,587 ns of non-overlapping AGX work and is reported separately because Metal System Trace does not expose a
supported client-PID linkage. The direct FFmpeg and `mv-hevc-encoder` PIDs used no AGX GPU time, and no VideoToolbox
service visible in the direct window reported AGX intervals.

This measures general-purpose AGX work, not VideoToolbox's dedicated Apple media engine. macOS does not expose a
supported per-process utilization API for that engine, so the profiler records that limitation rather than relabeling
media-engine activity as GPU use. Raw trace bundles, compressed interval exports, source and encoder hashes, command
arguments, host/tool versions, and artifact checksums are retained in a local evidence manifest. The manifest
fingerprints the canonical measurement summary, and a detached SHA-256 file authenticates the manifest itself. The
summary, manifest, and profiler hashes were independently rechecked after capture. The positive-controlled numeric
CPU/GPU comparison gate is complete on the qualifying host.

## Container and playback validation

The direct fixture passed all locally automatable checks:

- FFprobe reports one `hvc1` HEVC stream, 320x180 per eye, 24 fps, 48 decoded frames, limited-range BT.709 signaling.
- MP4Box reports `hvcC`, `lhvC`, `vexu`, `eyes`, `proj`, and `hfov`. The current toolkit baseline does not emit a
  separate `proj` box; the direct candidate does.
- `spatial-media-kit-tool split` produces one 48-frame left movie and one 48-frame right movie with the expected eye
  order and dimensions.
- `scripts/verify_apple_media.py` passes its AVFoundation/`avconvert` compatibility check.
- FFmpeg decodes frames after beginning, middle, and near-end seeks.

`scripts/create_direct_mv_hevc_playback_fixture.sh` creates the exact six-second direct-helper calibration movie with
English audio and subtitles, runs the Apple media compatibility check, and prepares it for the existing spatial
autorun workflow.

### Physical Apple Vision Pro result

On July 23, 2026, the exact direct-helper fixture passed the physical workflow in
`docs/visionos-playback-validator.md`. The fresh schema-3 report was bound to the local fixture by matching its full
fingerprint and file size: SHA-256 `0c13e6e65f13d6d852ef37904445a0cfe95995c40ce186b36ef9ae81a0b160fb`
and 3,317,639 bytes. The movie was copied back from the app container after the run and independently rechecked.

- All eight automatic checks passed, including stereo decode, player readiness, RealityKit rendering readiness,
  stereo presentation, spatial portal presentation, and beginning/middle/end seeks.
- The reported modes were Stereo · Spatial · Portal throughout the guided run.
- One audio option and two subtitle options were discovered.
- The wearer confirmed that the picture remained visible and that the scene appeared three-dimensional rather than
  flat.
- A signed visionOS build installed and launched on the paired physical headset without changing the validator or
  fixture after local qualification.

This completes the physical playback criterion for the direct encoder boundary. It does not change the separate
product rule that ordinary Blu-ray output must omit invented camera-baseline metadata and use the Stereo · Screen
presentation contract already qualified by the playback-validator workstream.

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
- No production routing or app-bundle change is included in this prototype. The completed qualification now allows
  those changes to proceed in the generated-intermediate and mode-specific-control integration workstream.

## Reproduction

```bash
uv run python scripts/build_mv_hevc_encoder_macos.py \
  --output build/mv-hevc-encoder/mv-hevc-encoder

uv run python scripts/qualify_direct_mv_hevc.py \
  --encoder build/mv-hevc-encoder/mv-hevc-encoder \
  --output build/direct-mv-hevc/direct.mov \
  --json-output build/direct-mv-hevc/qualification.json

uv run python -m scripts.qualify_mv_hevc_quality_match \
  --encoder build/mv-hevc-encoder/mv-hevc-encoder \
  --runs 3 \
  --json-output build/direct-mv-hevc-quality-match/quality-match.json

uv run python -m scripts.profile_mv_hevc_gpu \
  --encoder build/mv-hevc-encoder/mv-hevc-encoder \
  --eye-width 640 \
  --eye-height 360 \
  --frame-rate 24 \
  --duration 2 \
  --bitrate-mbps 4 \
  --trace-limit-seconds 30 \
  --json-output build/direct-mv-hevc-gpu/gpu-profile.json

scripts/create_direct_mv_hevc_playback_fixture.sh

uv run python -m unittest \
  tests.test_mv_hevc_encoder \
  tests.test_mv_hevc_quality_match \
  tests.test_mv_hevc_gpu_profile \
  -v
```

## Acceptance status

| Issue #347 criterion | Status |
| --- | --- |
| Encoder-boundary inventory | Passed |
| Final MV-HEVC fixture without eye HEVC movies | Passed |
| Eye order, dimensions, timing, color, FOV, disparity, and multiview metadata | Passed for the bounded fixture |
| Quality, size, elapsed time, CPU, GPU, and peak disk comparison | Passed on the bounded fixtures |
| AVFoundation and beginning/middle/end seeks | Passed |
| Physical Apple Vision Pro validation | Passed |
| Cancellation, backpressure, failure attribution, and cleanup | Passed for the bounded prototype |
| Restart and fallback behavior | Defined |
| Runtime, license, macOS, architecture, signing, notarization, and bundle implications | Recorded |

The prototype is fully qualified for product integration. It remains non-default only because the runtime, UI,
profile, protocol, fallback, packaging, and migration changes belong to the follow-on implementation plan.

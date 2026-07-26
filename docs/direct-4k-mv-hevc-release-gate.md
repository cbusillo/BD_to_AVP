# Direct 4K MetalFX MV-HEVC release gate

Issue #359 qualifies the in-process 2× MetalFX route integrated by #358. The July 24–26, 2026 local gate selects a
separate Automatic compression quality of `0.6` for direct 4K output; the source-resolution direct route remains at
`0.7`, and Custom remains an exact user-owned average-bitrate target.

The local deterministic, resource, Developer ID package, packaged-helper, Apple media, seek, device-transfer,
physical Vision Pro playback, representative real-MVC package-route, and feature-length gates pass. Final promotion
is now blocked only on the notarization and release-publication work tracked by #377.

## Policy decision

- Eligible Automatic 1080p-to-4K jobs use VideoToolbox compression quality `0.6` with
  `--upscale-mode metalfx`.
- Ordinary Automatic direct MV-HEVC remains quality `0.7`.
- Custom direct bitrate is unchanged and applies to the final 4K MV-HEVC output.
- Exact 1920×1080 eyes are required. Other dimensions fail before the first frame is appended and leave no partial
  output; crop and incompatible resolution overrides remain on the generated file-backed route.
- Capability fallback remains visible and pre-input. A direct failure after input starts is never silently replayed.

The initial `0.7` 4K smoke preserved quality and speed but produced a file 1.78 times the file-based route on the
48-frame motion control. Quality `0.6` preserves the numerical quality gate while restoring size headroom, so the
source-resolution constant is not reused blindly at four times the output pixels.

## Deterministic quality gate

`scripts/qualify_mv_hevc_metalfx.py` now records both the architecture's size-matched fixed-bitrate comparison and the
exact product Automatic quality path. Every Automatic repeat is compared with the file-based median, and the gate
requires it to:

- remain within `0.002` minimum decoded same-eye SSIM of the file-based FX Upscale median;
- preserve the same-eye-versus-cross-eye margin;
- stay within 105% of the file-route median size; and
- finish no slower than the complete file-based route.

The motion fixture uses three 120-frame runs. Every additional control uses one 48-frame run:

| Fixture | Worst-eye SSIM delta | Automatic/file size | Automatic/file time |
| --- | ---: | ---: | ---: |
| Motion | -0.000875 | 69.51% | 75.28% |
| Dark | -0.000041 | 76.40% | 73.65% |
| Grain | +0.001271 | 47.25% | 73.29% |
| Animation | -0.000600 | 33.01% | 73.92% |
| Crop control | -0.000263 | 92.20% | 73.51% |
| Disparity | -0.000577 | 96.81% | 73.93% |

All six controls pass. The crop fixture is a scaler-content control whose input is normalized back to exact 1080p;
product jobs requesting automatic crop still use the generated route.

Every synthetic quality JSON records the exact issue-#359 Developer ID helper SHA-256
`f5326193a580aa10a2645a7126a0555cfd34e2382f301c9737a9f962f1374879`:

| Evidence | SHA-256 |
| --- | --- |
| Motion | `39e9a5ec8c18a863548b4dcb198d12ff11d136afa7b30496cc7e03e43764e66e` |
| Dark | `17a5f4810e34c3d637ea0d97bc33660a2ab1def7f6465b3c0adbef5f706b62ba` |
| Grain | `03b8ffc785e0063da602fa0f06123dda948e2488f1c1f3fc413eb9b7b89022ee` |
| Animation | `6770046ec71f4621162e0c9d837480384ef3cf6ff89b8099bd37a6df1ec98bb2` |
| Crop control | `9c4cc5ce26034131928705813f5852a6f48f072feffa7bf479ba77db1a5e139e` |
| Disparity | `b338d09e6f96aad96e58100a411799fe6fd62c43a7961c232dc867a4e3f3337b` |

## Representative real-MVC quality gate

`scripts/qualify_real_mvc_4k_quality.py` repeats both production 4K routes three times against the deterministic
65.649-second MVC segment. The direct route runs through the signed package at Automatic quality `0.6`; the
file-based comparison uses the same package with a controlled pre-input MetalFX-unavailable capability response and
then the production FX Upscale stage. Subtitles are disabled only for this visual comparison; the eight-route package
matrix independently verifies audio and subtitle preservation.

The gate creates lossless FFV1 left/right references with the packaged FFmpeg and edge264 tools, splits each MV-HEVC
output with the packaged Spatial Media Toolkit, verifies exactly 1,573 frames at 3840×2160 per eye, downsamples each
eye to source resolution, and measures both same-eye and cross-eye SSIM. Every run must remain within `0.002` of the
file-based median, stay within 105% of its median size, and preserve at least a `0.001` eye-order margin.

- Direct median worst-eye SSIM is `0.952509`, `0.001647` above the file-based median of `0.950862`.
- Every direct output is `101,009,167` bytes. The largest direct/file-median ratio is `0.374800`, well below `1.05`.
- The minimum eye-order margin is `0.037974` for direct output and `0.034867` for file-based output.
- Hardware-encoder container hashes vary across repeats, while decoded measurements and direct output sizes remain
  stable; the gate intentionally evaluates every run rather than requiring byte-identical hardware output.

The representative real-MVC quality evidence SHA-256 is
`ac0dd829b06d6d6dd214a5ffd1a8389eb93f79ab75197bc9730789ee7463565a`. It binds the package app-tree, helper,
worker, controlled fallback helper, and packaged tool hashes without recording source paths or titles.

## Resource gate

`scripts/benchmark_mv_hevc_metalfx.py --quality 0.6` compares 24-frame and 2,400-frame runs. The 100-second MetalFX
run establishes a useful synthetic allocation diagnostic:

- Metal device peak delta is `419,086,336` bytes for the short run and `452,280,320` bytes for the long run.
- The `33,193,984`-byte difference is exactly one aligned 3840×2160 BGRA texture. The same helper also produced
  repeated 180-, 480-, and 2,400-frame plateaus at `419,086,336`, so the upper plateau is allocation variance rather
  than monotonic duration growth.
- Declared Metal-compatible pools and private textures are `364,953,600` bytes. The gate permits up to three aligned
  4K texture-equivalents (`99,581,952` bytes) for MetalFX-owned internal allocation but no more than one texture of
  short-to-long variance. The observed synthetic peak remains below the modeled `464,535,552`-byte ceiling.
- Baseline-normalized excess process-RSS growth is zero.
- Decoded frame count, 3840×2160 per-eye geometry, and limited-range BT.709 signaling pass.

The benchmark evidence SHA-256 is
`ee78581ec5bb5bc83669f99dd7ca431b00fd6b8bf8fa25f4b1241ee118f11778`.

A separate 25-sample, one-second `powermetrics` trace remained at `Nominal` thermal pressure throughout a 480-frame
MetalFX workload. Global GPU active time averaged 50.86% while the helper was visible and 39.87% outside that window,
with a 54.75% maximum. macOS did not emit per-process GPU fields despite `--show-process-gpu`, so those global values
are informational rather than exact helper attribution. The raw trace SHA-256 is
`3dd47c2ad85bbb64bc33d7ec40d1a50e79e783d086321d8f39f8e4d9694a1557`; its bound workload evidence SHA-256 is
`10b2753fe6ee0419f423f9e2cc5459cabcf2b9c28295a25c6fbe8bd19580961e`.

The synthetic modeled ceiling is not used as an absolute production limit. Real workloads include the MVC splitter,
two FFmpeg processes, the direct encoder, framework warm-up, and MetalFX allocation classes that are absent from the
standalone benchmark. `scripts/qualify_real_mvc_feature.py` therefore applies the production gate to a deterministic
65.649-second segment and its verified 5,737.760-second parent source:

- the complete source contributes exactly `137,568` packets at `24000/1001`, and the direct 4K encoder reports exactly
  `137,568` output frames;
- the resulting 3840×2160-per-eye artifact is `5,737.731667` seconds and `6,546,871,801` bytes, passes Apple media
  compatibility and beginning/middle/end seeks, and has SHA-256
  `d2f46d8a929a7e0b060f6dec2fd57f901dafd6101ddb4580310eebd51f568867`;
- aggregate process RSS peaks at `1,516,781,568` bytes, below the prerelease safety ceiling of `2,147,483,648` bytes;
  its diagnostic quartile peaks are `1,386,217,472`, `1,441,972,224`, `1,446,772,736`, and `1,516,781,568` bytes;
- plateau acceptance is evaluated per process because aggregate quartile peaks combine non-coincident maxima. The
  largest Q3-to-Q4 process increase is `50,937,856` bytes, below the 64 MiB allowance. A separate fixed 128 MiB
  process-tree cap prevents the allowance from scaling with process count; the aggregate increase is `70,008,832`
  bytes and passes that cap;
- the normalizer FFmpeg process peaks at `793,739,264` bytes and the direct encoder at `474,562,560` bytes. Their
  per-process timelines distinguish finite warm-up from monotonic duration growth;
- Metal device peak delta is `485,474,304` bytes and final allocated size is `485,556,224` bytes, below the
  prerelease safety ceiling of `1,073,741,824` bytes. Its `33,193,984`-byte short-to-feature increase is exactly one
  aligned 4K texture and passes the duration-growth allowance;
- all `9,017` in-process thermal samples and all `1,046` `powermetrics` thermal sections remain nominal;
- `2,580` of `2,581` recorded artifact samples increase, with a maximum growth gap of `7.190` seconds and no
  reported failure codes; and
- production-style cancellation after direct output begins emits `job.cancelled` with exit 130, preserves an existing
  destination sentinel, leaves no partial files or surviving descendants, removes its private work directory, and
  remains at nominal thermal pressure.

The complete feature evidence SHA-256 is
`84f18a732b303a2032e6a4db5ba98e14dc7a1042f11903b31b131cfa45919d02`. It transparently reassesses retained raw
telemetry with SHA-256 `0d34d59cf819aaf66b81339c2b34e361f13ec4651702f7c392bf89426e028dfd` under the reviewed
`per-process-and-aggregate-q3-q4-plateau-v3` policy. The public-safe aggregate is committed at
`docs/qualification/direct-4k-real-mvc-v1.json`; private source paths, source titles, media, and raw traces remain
outside the repository.

The public aggregate also pins SHA-256 values for the nine qualification scripts and shared policy helpers used by
these gates. `tests/test_real_mvc_public_evidence.py` recomputes those hashes so later harness edits cannot silently
reuse this evidence without an explicit reviewed update.

## Package and rollback gate

An arm64 Release app with the approved diagnostics endpoint builds, assembles, signs with `Developer ID Application:
Shiny Computers Leasing LLC (MM5YXC7T6E)`, deep-verifies, passes Gatekeeper assessment, and passes the packaged
native-app/helper/worker smokes. The app and helper both declare the required macOS 26.0 deployment target. The exact
packaged helper SHA-256 is `4245452e4c5dbc05479c48c5a37caa077d5c95f18a93b78272e6ac583fbd5394`, and its
capability probe reports ordinary direct MV-HEVC, MetalFX 2×, MetalFX spatial scaling, and pixel-transfer 2× support.
The app does not yet have a stapled ticket; notarization remains a release-workflow gate.

`scripts/verify_packaged_mv_hevc_routes.py` verifies both route families against the deterministic representative
segment (SHA-256 `da31e6ae9749897ca199f4a37a781b2be9a2d82076885efea4d0c156673bbcec`):

- ordinary direct plus the existing stereo-capability-unavailable fallback;
- direct 4K MetalFX plus a controlled helper that supports ordinary stereo MV-HEVC but reports MetalFX unavailable;
- full/finalized-preview route parity, exact quality reports, stage contracts, 1080p/4K dimensions, spatial boxes,
  audio/subtitle preservation, Apple passthrough, and beginning/middle/end seeks.

The controlled MetalFX-unavailable app is cloned, helper-replaced, ad-hoc re-signed, and deep-verified. Its helper may
only answer the capability probe, so any accidental attempt to encode through it fails immediately. All eight signed-
package combinations pass: ordinary direct and generated fallback plus direct 4K MetalFX and MetalFX-unavailable
generated fallback, each for full and finalized-preview output. The matrix records exact route decisions, stage
contracts, artifact hashes, dimensions, spatial boxes, audio/subtitle preservation, Apple passthrough, and seeks.

The package under test is version `0.3.0b6`, with app-tree SHA-256
`7294f205d9d95d53f72d7fa30d977b2551c6d6ab8c0bdeddc444c354060fc801`, helper SHA-256
`4245452e4c5dbc05479c48c5a37caa077d5c95f18a93b78272e6ac583fbd5394`, and worker SHA-256
`1a9f2edeabc5341f15d804d805511a35fecca05ce9e7db2ba7aa29d80b59f109`. The final matrix evidence SHA-256 is
`e182cc91b890f3177a00ad32db0c8e1039d314c79976f0ab2c56f631f4862f6e`.

## Physical playback gate

`scripts/create_direct_mv_hevc_playback_fixture.sh` creates a six-second 3840×2160-per-eye calibration movie with the
packaged helper, Automatic quality `0.6`, English AAC, English subtitles, required spatial boxes, Apple compatibility,
and three local seeks. The point-in-time Developer ID package fixture is `7,974,482` bytes with SHA-256
`9e6a06d2782d2c9bd5d23cc8a6955aed0051d483f36d926cbddfb5563fa4679a`.

The signed visionOS validator is installed on the paired physical Vision Pro. That exact Developer ID package fixture
was copied into the app container and read back byte-for-byte with the same SHA-256. Because this synthetic fixture
contains fixed calibration camera metadata, it is launched with
`BD_TO_AVP_PROBE_EXPECTED_PRESENTATION=spatial`, not the normal Blu-ray `stereo` expectation.

The July 25, 2026 physical report passes all eight automatic checks: stereo MV-HEVC decode, player readiness,
RealityKit readiness, stereoscopic playback, `Stereo · Spatial · Portal` presentation, and beginning/middle/end seeks.
The wearer reported **Yes** for continuous visible video and **Yes** for three-dimensional presentation. The schema-3
report binds those observations to the exact fixture hash above and has SHA-256
`6681cacb1e286b02af5f7b5d04e609817db9f466a9a314bae6dbd43b346bf90c`.

## Reproduction

```bash
uv run python -m scripts.qualify_mv_hevc_metalfx \
  --encoder build/mv-hevc-encoder/mv-hevc-encoder \
  --output build/issue-359/quality-motion.json \
  --output-movie build/issue-359/quality-motion.mov \
  --artifacts-directory build/issue-359/quality-motion \
  --fixture motion \
  --frames 120 \
  --runs 3

uv run python -m scripts.benchmark_mv_hevc_metalfx \
  --encoder build/mv-hevc-encoder/mv-hevc-encoder \
  --output build/issue-359/benchmark.json \
  --artifacts-directory build/issue-359/benchmark \
  --short-frames 24 \
  --long-frames 2400 \
  --quality 0.6

BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT=https://diagnostics.shinycomputers.com \
  uv run python scripts/native_app.py package

scripts/create_direct_mv_hevc_playback_fixture.sh \
  build/issue-359/Probe-4K.mov \
  "macos/build/package/3D Blu-ray to Vision Pro.app/Contents/Resources/app/bd_to_avp/bin/mv-hevc-encoder"

export BD_TO_AVP_QUALIFICATION_ROOT=/private/path/issue-376
export BD_TO_AVP_SIGNED_APP=/private/path/3D-Blu-ray-to-Vision-Pro.app

uv run python -m scripts.create_real_mvc_qualification_segment \
  --source "$BD_TO_AVP_RELEASE_MVC_SOURCE" \
  --output "$BD_TO_AVP_QUALIFICATION_ROOT/segment/real-mvc-segment-v1.mkv" \
  --evidence "$BD_TO_AVP_QUALIFICATION_ROOT/evidence/real-mvc-segment-v1.json" \
  --start-seconds 4500 \
  --duration-seconds 65.648 \
  --video-track 0 \
  --audio-track 1 \
  --subtitle-track 41 \
  --expected-source-sha256 a1e3adf85f64a5a667d16ef4aa5a982183acece5af1cf7142e49da620982f97c \
  --deterministic-seed bd-to-avp-issue-376-segment-v1

uv run python -m scripts.verify_packaged_mv_hevc_routes \
  --app "$BD_TO_AVP_SIGNED_APP" \
  --source "$BD_TO_AVP_QUALIFICATION_ROOT/segment/real-mvc-segment-v1.mkv" \
  --output "$BD_TO_AVP_QUALIFICATION_ROOT/evidence/packaged-real-mvc-routes.json" \
  --fixture-output "$BD_TO_AVP_QUALIFICATION_ROOT/artifacts/real-mvc-direct-4k.mov" \
  --expected-source-sha256 da31e6ae9749897ca199f4a37a781b2be9a2d82076885efea4d0c156673bbcec

uv run python -m scripts.qualify_real_mvc_4k_quality \
  --app "$BD_TO_AVP_SIGNED_APP" \
  --source "$BD_TO_AVP_QUALIFICATION_ROOT/segment/real-mvc-segment-v1.mkv" \
  --output "$BD_TO_AVP_QUALIFICATION_ROOT/evidence/quality-real-mvc.json" \
  --work-directory "$BD_TO_AVP_QUALIFICATION_ROOT/quality-real-mvc" \
  --expected-source-sha256 da31e6ae9749897ca199f4a37a781b2be9a2d82076885efea4d0c156673bbcec

sudo -v
uv run python -m scripts.qualify_real_mvc_feature \
  --app "$BD_TO_AVP_SIGNED_APP" \
  --source "$BD_TO_AVP_RELEASE_MVC_SOURCE" \
  --segment "$BD_TO_AVP_QUALIFICATION_ROOT/segment/real-mvc-segment-v1.mkv" \
  --output "$BD_TO_AVP_QUALIFICATION_ROOT/evidence/feature-real-mvc.json" \
  --work-directory "$BD_TO_AVP_QUALIFICATION_ROOT/feature-real-mvc" \
  --expected-source-sha256 a1e3adf85f64a5a667d16ef4aa5a982183acece5af1cf7142e49da620982f97c \
  --expected-segment-sha256 da31e6ae9749897ca199f4a37a781b2be9a2d82076885efea4d0c156673bbcec

uv run python -m scripts.reassess_real_mvc_feature \
  --input "$BD_TO_AVP_QUALIFICATION_ROOT/evidence/feature-real-mvc.json" \
  --output "$BD_TO_AVP_QUALIFICATION_ROOT/evidence/feature-real-mvc-reassessed.json"
```

The operator supplies `BD_TO_AVP_RELEASE_MVC_SOURCE`, `BD_TO_AVP_SIGNED_APP`, and a private qualification root.
Source paths are never written to evidence. The replacement segment is mode `0600`; only aggregate hashes, public
media properties, route decisions, and bounded resource summaries are committed.

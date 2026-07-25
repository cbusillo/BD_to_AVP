# Direct 4K MetalFX MV-HEVC release gate

Issue #359 qualifies the in-process 2× MetalFX route integrated by #358. The July 24–25, 2026 local gate selects a
separate Automatic compression quality of `0.6` for direct 4K output; the source-resolution direct route remains at
`0.7`, and Custom remains an exact user-owned average-bitrate target.

The local deterministic, resource, Developer ID package, packaged-helper, Apple media, seek, device-transfer, and
physical Vision Pro playback gates pass. Final promotion remains blocked on the private representative-MVC corpus,
the complete packaged worker direct/fallback matrix using that source, and notarization evidence.

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

Every quality JSON records the exact Developer ID helper SHA-256
`f5326193a580aa10a2645a7126a0555cfd34e2382f301c9737a9f962f1374879`:

| Evidence | SHA-256 |
| --- | --- |
| Motion | `39e9a5ec8c18a863548b4dcb198d12ff11d136afa7b30496cc7e03e43764e66e` |
| Dark | `17a5f4810e34c3d637ea0d97bc33660a2ab1def7f6465b3c0adbef5f706b62ba` |
| Grain | `03b8ffc785e0063da602fa0f06123dda948e2488f1c1f3fc413eb9b7b89022ee` |
| Animation | `6770046ec71f4621162e0c9d837480384ef3cf6ff89b8099bd37a6df1ec98bb2` |
| Crop control | `9c4cc5ce26034131928705813f5852a6f48f072feffa7bf479ba77db1a5e139e` |
| Disparity | `b338d09e6f96aad96e58100a411799fe6fd62c43a7961c232dc867a4e3f3337b` |

## Resource gate

`scripts/benchmark_mv_hevc_metalfx.py --quality 0.6` compares 24-frame and 2,400-frame runs. The 100-second MetalFX
run passes the bounded allocation model:

- Metal device peak delta is `419,086,336` bytes for the short run and `452,280,320` bytes for the long run.
- The `33,193,984`-byte difference is exactly one aligned 3840×2160 BGRA texture. The same helper also produced
  repeated 180-, 480-, and 2,400-frame plateaus at `419,086,336`, so the upper plateau is allocation variance rather
  than monotonic duration growth.
- Declared Metal-compatible pools and private textures are `364,953,600` bytes. The gate permits up to three aligned
  4K texture-equivalents (`99,581,952` bytes) for MetalFX-owned internal allocation but no more than one texture of
  short-to-long variance. The observed peak remains below the `464,535,552`-byte ceiling.
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

The private representative source is still required for feature-length thermal and real-MVC backpressure evidence.

## Package and rollback gate

An arm64 Release app with the approved diagnostics endpoint builds, assembles, signs with `Developer ID Application:
Shiny Computers Leasing LLC (MM5YXC7T6E)`, deep-verifies, passes Gatekeeper assessment, and passes the packaged
native-app/helper/worker smokes. The app and helper both declare the required macOS 26.0 deployment target. The exact
packaged helper SHA-256 is `f5326193a580aa10a2645a7126a0555cfd34e2382f301c9737a9f962f1374879`, and its
capability probe reports ordinary direct MV-HEVC, MetalFX 2×, MetalFX spatial scaling, and pixel-transfer 2× support.
The app does not yet have a stapled ticket; notarization remains a release-workflow gate.

`scripts/verify_packaged_mv_hevc_routes.py` now verifies both route families:

- ordinary direct plus the existing stereo-capability-unavailable fallback;
- direct 4K MetalFX plus a controlled helper that supports ordinary stereo MV-HEVC but reports MetalFX unavailable;
- full/finalized-preview route parity, exact quality reports, stage contracts, 1080p/4K dimensions, spatial boxes,
  audio/subtitle preservation, Apple passthrough, and beginning/middle/end seeks.

The controlled MetalFX-unavailable app is cloned, helper-replaced, ad-hoc re-signed, and deep-verified. Its helper may
only answer the capability probe, so any accidental attempt to encode through it fails immediately. Executing the full
matrix remains blocked on the private 65-second representative MVC source.

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
```

The private corpus and package-route reproduction continues to use `BD_TO_AVP_RELEASE_MVC_SOURCE` and
`BD_TO_AVP_ITU_MVC_VECTOR`; source paths are never written to evidence.

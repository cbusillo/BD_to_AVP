# In-process MetalFX MV-HEVC prototype

Issue #357 adds an explicit native-only 2x upscale prototype to `mv-hevc-encoder`. It does not activate the Python
worker, GUI, route resolver, profiles, packaging policy, or other user-visible routing owned by #358.

## CLI contract

Build and inspect capability:

```bash
uv run python -m scripts.build_mv_hevc_encoder_macos \
  --output build/mv-hevc-encoder/mv-hevc-encoder

build/mv-hevc-encoder/mv-hevc-encoder --capability-probe
```

The probe reports ordinary stereo MV-HEVC support plus `metalfx_2x_mv_hevc_supported` and
`pixel_transfer_2x_mv_hevc_supported`.

Run the intended scaler with `--upscale-mode metalfx`. Use `--upscale-mode pixel-transfer` only as the non-MetalFX
quality and conversion control. Standard input remains progressive, 8-bit, 4:2:0 side-by-side Y4M. Both modes require
a `3840x1080` SBS frame: each separated eye is exactly `1920x1080`, and each appended MV-HEVC eye is `3840x2160`.
Unsupported dimensions or hardware fail before the first frame is appended; neither mode substitutes an original
1080p frame.

The ordinary CLI path is unchanged when `--upscale-mode` is absent.

## Bounded frame path

The MetalFX path performs one synchronized eye pair at a time:

1. Split Y4M planes into a shared four-buffer, video-range NV12 source pool.
2. Convert each eye with its own `VTPixelTransferSession` into a Metal-compatible BGRA pool capped at two buffers.
3. Encode each eye on one shared Metal device with an independent `MTLFXSpatialScaler`, command queue, and fixed
   private 4K output texture.
4. Blit into an eye-specific BGRA output pool capped at four buffers.
5. Append left and right tagged buffers together at one rational presentation timestamp.

The VideoToolbox control transfers NV12 directly into the same 4K BGRA output shape without MetalFX. Output-pool
exhaustion is treated as backpressure and retried while checking cancellation. Other pool, transfer, texture, MetalFX,
or command-buffer failures are deterministic errors. SIGINT or SIGTERM exits `130`, cancels the writer, and removes the
partial file.

MetalFX summaries report the device's initial, peak, and final `currentAllocatedSize` values plus the peak delta. This
accounts for Metal resources that process RSS cannot see and gives the benchmark a duration-independent device-memory
gate in addition to its CPU-side RSS gate. The samples capture retained allocation before and after each synchronous
eye dispatch; they are not a profiler for sub-command transient allocation spikes.

Input buffers, output settings, and propagated output attachments explicitly carry limited-range BT.709 primaries,
transfer, matrix, and chroma location. The exact 2x scale preserves eye aspect ratio without crop or padding.

## Upstream provenance

The scaler boundary adapts concepts from [`fx-upscale` 1.3.2](https://github.com/finnvoor/fx-upscale/tree/1.3.2),
commit [`46601bdcd5c8c62f9735210e9245efab2da4a5c9`](https://github.com/finnvoor/fx-upscale/commit/46601bdcd5c8c62f9735210e9245efab2da4a5c9),
whose source is dedicated under CC0-1.0. The adapted concepts are the `MTLFXSpatialScalerDescriptor`, private scaler
output texture, Core Video/Metal texture-cache bridge, and pooled output buffers.

This implementation intentionally differs from upstream by using independent per-eye scaler state, explicit
VideoToolbox color conversion, fixed allocation thresholds, cancellation-aware backpressure, and throwing failures.
Upstream's convenience API returns the original frame on setup or GPU failure; that fallback is unsafe at a fixed-size
4K writer boundary. No SwiftPM or network dependency is added.

## Evidence helpers

Resource and geometry qualification:

```bash
uv run python -m scripts.benchmark_mv_hevc_metalfx \
  --output build/evidence/metalfx-benchmark.json \
  --artifacts-directory build/evidence/metalfx-benchmark \
  --short-frames 24 \
  --long-frames 480
```

Quality and file-route comparison:

```bash
uv run python -m scripts.qualify_mv_hevc_metalfx \
  --output build/evidence/metalfx-quality.json \
  --output-movie build/evidence/metalfx-qualified.mov \
  --artifacts-directory build/evidence/metalfx-quality \
  --fixture motion \
  --frames 120 \
  --runs 3
```

Run the additional deterministic coverage controls with `--fixture dark`, `grain`, `crop`, and `disparity`. The
fixture name and every search probe are recorded in the JSON evidence.

The benchmark compares unchanged 1080p, VideoToolbox 2x, and MetalFX 2x runs at short and long durations. It records
dimensions, decoded frame count, color signaling, elapsed time, process-tree peak RSS, Metal device allocation, file
size, source and encoder hashes, hardware, OS, and git SHA. The RSS gate subtracts ordinary encoder growth observed in
the unchanged 1080p baseline. The Metal gate compares 24- and 480-frame peak allocation deltas against the declared
Metal-compatible pools and private textures plus a bounded allowance for MetalFX-owned resources.

The quality helper creates a deterministic native-4K reference, downscales it once to the shared 1080p input, and
compares direct MetalFX, direct pixel transfer, and the bundled file-based FX Upscale route. Before measuring direct
quality, it records a bounded binary search for the highest direct bitrate whose output remains within 99% of the
file-route median size. It then requires every repeated direct result to retain that size headroom, preserve eye order
and spatial boxes, match decoded quality within the configured tolerance, and finish faster than the two-pass route.
`--direct-bitrate-mbps` is an explicit diagnostic override; the default gate has no fixture-tuned magic bitrate.

These deterministic controls qualify the prototype architecture. Real representative-disc corpus and physical Vision
Pro release qualification remain part of #359 after #358 wires the product route.

## Recorded prototype result

The July 24, 2026 run on `Mac16,9` with macOS 27.0 passes. Metal device allocation reaches `419,086,336` bytes above
its initial value for both the 24- and 480-frame MetalFX runs, so measured duration growth is zero. The declared
Metal-compatible pool and private-texture payload is `364,953,600` bytes; the remaining approximately 52 MiB stays
inside the 64 MiB allowance for MetalFX-owned resources. Baseline-normalized excess process-RSS growth is also zero.

The 120-frame motion gate uses three measured runs. The additional controls use one 48-frame run each:

| Fixture | Selected direct Mbps | Worst-eye SSIM delta | Direct/file size | Direct/file time |
| --- | ---: | ---: | ---: | ---: |
| Motion | 20.0957 | -0.000399 | 92.30% | 76.51% |
| Dark | 11.7637 | +0.000151 | 97.99% | 75.14% |
| Grain | 30.2021 | -0.001477 | 98.70% | 74.24% |
| Crop | 17.9355 | -0.000235 | 98.22% | 75.44% |
| Disparity | 13.7696 | -0.000752 | 98.56% | 74.79% |

Every fixture preserves eye order, spatial boxes, 4K dimensions, decoded frame count, and limited-range BT.709
signaling. Every quality delta remains within `0.002`, every direct output retains the configured size headroom, and
every direct run is faster than the complete two-pass file route.

## Prototype limits

- SDR, progressive, 8-bit 4:2:0 Y4M only.
- Exact `1920x1080` input eyes and `3840x2160` output eyes.
- BGRA writer input, with VideoToolbox performing the final encoder-native conversion.
- Spatial scaling only; no temporal scaling, HDR/10-bit work, product routing, fallback routing, or default changes.

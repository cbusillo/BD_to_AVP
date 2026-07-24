# In-process MetalFX MV-HEVC prototype

Issue #357 adds an explicit native-only prototype path to `mv-hevc-encoder`. It does not activate the Python worker,
GUI, route resolver, profiles, packaging policy, or any other user-visible routing owned by #358.

## CLI contract

Build and inspect capability:

```bash
uv run python -m scripts.build_mv_hevc_encoder_macos --output build/mv-hevc-encoder/mv-hevc-encoder
build/mv-hevc-encoder/mv-hevc-encoder --capability-probe --prototype-metalfx-upscale
```

Run the prototype by adding `--prototype-metalfx-upscale` to the existing encoder command. Standard input remains a
progressive, 8-bit, 4:2:0 side-by-side Y4M stream. The prototype requires a `3840x1080` SBS frame: each separated eye
is exactly `1920x1080`, and each appended MV-HEVC eye is `3840x2160`. Unsupported dimensions or hardware fail before
the first frame is appended; the encoder never substitutes the original 1080p frame.

The ordinary CLI path and the ordinary `--capability-probe` JSON are unchanged.

## Bounded frame path

Each frame uses one serialized pair operation:

1. Split Y4M planes into a two-buffer, video-range NV12 source pool.
2. Convert both eyes through Core Image into a two-buffer BGRA Metal-compatible pool using BT.709.
3. Encode two independent `MTLFXSpatialScaler` instances into two fixed private 4K textures on one command buffer.
4. Blit into the writer's BGRA pool, capped at eight buffers with `CVPixelBufferPool` allocation thresholds.
5. Append left and right tagged buffers together at one rational presentation timestamp.

The eight-buffer writer ceiling permits four retained eye pairs while VideoToolbox reorders frames. Pool exhaustion is
treated as backpressure and retried only while the writer remains healthy. Other pool, texture, conversion, MetalFX,
or command-buffer failures throw deterministic errors. SIGINT/SIGTERM is checked before and after scaling and while
waiting for pool or writer capacity; cancellation exits `130`, cancels the writer, and removes the partial file.

Both the writer settings and pixel-buffer attachments signal limited-range BT.709 primaries, transfer, and matrix.
The 2x scale preserves the source eye aspect ratio without crop or padding.

## Upstream provenance

The scaler boundary adapts concepts from [`fx-upscale` 1.3.2](https://github.com/finnvoor/fx-upscale/tree/1.3.2),
commit [`46601bdcd5c8c62f9735210e9245efab2da4a5c9`](https://github.com/finnvoor/fx-upscale/commit/46601bdcd5c8c62f9735210e9245efab2da4a5c9),
whose source is dedicated under CC0-1.0. The adapted concepts are the `MTLFXSpatialScalerDescriptor`, private scaler
output textures, Core Video/Metal texture-cache bridge, and pooled pixel buffers.

This implementation intentionally differs from upstream by using independent per-eye scaler state, validating every
dimension and pixel format, enforcing allocation thresholds, and throwing on every failure. Upstream's convenience API
returns the original frame when setup or GPU work fails; that fallback is not safe at this writer boundary. No SwiftPM
package or network dependency is added.

## Evidence helper

The benchmark helper compares the unchanged 1080p encoder, a short MetalFX run, and a longer MetalFX run:

```bash
uv run python -m scripts.benchmark_mv_hevc_metalfx \
  --output build/evidence/metalfx-prototype.json \
  --artifacts-directory build/evidence/metalfx-prototype \
  --short-frames 24 \
  --long-frames 240
```

It fails on dimension, decoded frame-count, color-signaling, pool-limit, capability, or pool-aware peak-RSS-ceiling
mismatches. The `--max-rss-growth-mib` value is an allowance beyond the declared pool payload, not a replacement for
that expected warm-up. Evidence includes output stream metadata, encoder summaries, elapsed time, encoder process-tree
peak RSS, media sizes, hardware/OS identity, encoder hash, and git SHA. `declared_pool_payload_bytes` is the unaligned
pixel payload represented by the explicit pools and two private scaler outputs; driver, MetalFX, VideoToolbox,
row-alignment, and codec memory are measured in RSS where visible rather than included in that payload estimate.

## Prototype limits

- SDR, progressive, 8-bit 4:2:0 Y4M only.
- Exact `1920x1080` input eyes and `3840x2160` output eyes.
- BGRA writer input, with VideoToolbox performing the final encoder-native conversion.
- Spatial scaling only; no temporal scaling, HDR/10-bit work, product routing, fallback routing, or default changes.

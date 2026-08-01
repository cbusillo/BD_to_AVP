# Native Worker Protocol v12

Protocol v12 carries quality mapping version 2 and the exact concrete direct
quality selected by the native app. The native app and bundled Python worker
ship atomically and both require version 12.

Lifecycle events, ownership, heartbeats, request limits, audio/subtitle policy,
stage numbering, cancellation, and terminal behavior are unchanged from
[Native Worker Protocol v11](native-worker-protocol-v11.md).

The candidate route table is
[`video-quality-route-table-v2.json`](qualification/video-quality-route-table-v2.json).
It is frozen objective input to #422, not release qualification.

## Quality Intent

Guided quality carries a stable step and mapping version 2:

```json
{
  "mode": "ladder",
  "step": "detailed",
  "mapping_version": 2
}
```

Exact expert settings remain:

```json
{ "mode": "custom" }
```

The worker validates intent against concrete route controls. It never aliases an
unsupported step to `balanced`.

## Automatic Direct MV-HEVC

Every Automatic direct request carries exactly one direct rate-control policy.
When that policy is Automatic, `direct_quality` is required and contains the
resolved VideoToolbox quality value.

Balanced source-resolution direct output carries its checked generated fallback:

```json
{
  "mode": "mv_hevc",
  "route_intent": "automatic",
  "quality_intent": {
    "mode": "ladder",
    "step": "balanced",
    "mapping_version": 2
  },
  "direct_bitrate": { "mode": "automatic" },
  "direct_quality": 0.7,
  "generated_fallback": {
    "eye_bitrate": { "mode": "automatic" },
    "merge_quality": 75
  }
}
```

For direct MetalFX 2×, Balanced uses `direct_quality: 0.6`; the sibling
`encoding.upscale` object includes `quality: 75` for the file-based fallback.

The six non-Balanced direct steps omit `generated_fallback`. A Detailed
source-resolution request is therefore:

```json
{
  "mode": "mv_hevc",
  "route_intent": "automatic",
  "quality_intent": {
    "mode": "ladder",
    "step": "detailed",
    "mapping_version": 2
  },
  "direct_bitrate": { "mode": "automatic" },
  "direct_quality": 0.75
}
```

If capability preflight cannot provide the requested direct route, a
non-Balanced job fails before reading conversion input. It is never converted as
generated Balanced. For MetalFX non-Balanced requests, `encoding.upscale` may be
`{"enabled": true}` without a file-upscale quality because no file-based
fallback exists.

Custom Automatic direct rate control also carries a concrete quality: `0.7` at
source resolution or `0.6` with direct MetalFX 2×. Custom fixed bitrate omits
`direct_quality`. Custom continues to carry exact generated fallback controls.

## Direct Mapping Version 2

| Step | Direct | Direct MetalFX 2× |
|---|---:|---:|
| Space Saver | 0.40 | 0.30 |
| Compact | 0.50 | 0.40 |
| Efficient | 0.60 | 0.50 |
| Balanced | 0.70 | 0.60 |
| Detailed | 0.75 | 0.65 |
| High Detail | 0.80 | 0.70 |
| Maximum Detail | 0.85 | 0.75 |

## Generated MV-HEVC

Generated MV-HEVC accepts only guided Balanced: Automatic 20 Mbps per eye and
merge quality 75. Its six other guided positions are unavailable. Custom keeps
the v11 exact bitrate and merge-quality controls.

When file upscale is active on a generated route, guided Balanced additionally
requires file-upscale quality 75.

## Existing-Artifact File Upscale

Stage-6 existing-artifact upscale accepts guided Balanced at quality 75 and
Detailed at quality 100. The other five guided positions are unavailable.
Custom accepts an exact quality from 0 through 100.

Later existing-artifact stages continue to omit quality intent and upscale
settings because video quality is inactive.

## AV1

AV1 remains Custom-only with exact CRF. Mapping version 2 defines no AV1 guided
position.

## Reporting

Requested and selected route reporting retains the v11 shape. Direct reports
include `rate_control: "quality"` and the exact selected `quality`. Balanced
fallback reports retain requested direct values, selected generated values,
`fallback_reason`, and `fallback_timing: "pre_input"`.

## Compatibility

Protocol v11 requests and events are rejected with `protocol_mismatch` by v12
peers. Historical v11 fixtures remain rejection evidence; v12 fixtures are the
current shared Swift/Python contract.

Profile document version 5 is unchanged. Profiles already store route-relative
guided intent and retain exact Custom values independently; v12 changes runtime
projection, route validation, and reporting only.

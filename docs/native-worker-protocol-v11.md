# Native Worker Protocol v11

Protocol v11 carries route-aware video quality intent and the concrete settings
needed for deterministic pre-input fallback. The native app and bundled Python
worker ship atomically and both require version 11.

Lifecycle events, ownership, heartbeats, request limits, audio/subtitle policy,
stage numbering, cancellation, and terminal behavior are unchanged from
[Native Worker Protocol v10](native-worker-protocol-v10.md).

## Quality Intent

Every active video encode includes one strict `quality_intent` object. Guided
quality includes the stable step identifier and checked mapping version:

```json
{
  "mode": "ladder",
  "step": "balanced",
  "mapping_version": 1
}
```

Exact expert settings use:

```json
{ "mode": "custom" }
```

Mapping version 1 currently accepts only `balanced`. The worker rejects the six
unqualified step identifiers rather than aliasing them. `balanced` is valid only
when every concrete route field matches the checked mapping: direct Automatic,
generated Automatic with merge quality 75, and file upscale quality 75 when
enabled. AV1 always requires `custom`. Existing-artifact routes omit quality
intent unless stage 6 is actively applying file upscale; that restart carries
the same checked intent plus concrete upscale quality.

The intent object is descriptive and integrity-checked; it is never the sole
source of worker truth. Every route still carries concrete route controls.

## Automatic Direct MV-HEVC

An eligible direct request carries the active direct policy plus the generated
settings to use only if capability preflight selects fallback:

```json
{
  "mode": "mv_hevc",
  "route_intent": "automatic",
  "quality_intent": {
    "mode": "ladder",
    "step": "balanced",
    "mapping_version": 1
  },
  "direct_bitrate": {
    "mode": "automatic"
  },
  "generated_fallback": {
    "eye_bitrate": {
      "mode": "automatic"
    },
    "merge_quality": 75
  }
}
```

`direct_bitrate` and `generated_fallback.eye_bitrate` retain the v10 strict
bitrate object: Automatic omits `mbps`; Custom requires one integer from 1
through 500. `generated_fallback.merge_quality` is an integer from 0 through
100.

For `balanced`, direct Automatic resolves to VideoToolbox compression quality
0.7 at source resolution or 0.6 for the checked direct MetalFX 2× route.
Generated fallback resolves to 20 Mbps per eye and merge quality 75. For
`custom`, each active policy is preserved independently. An Automatic fallback
policy resolves to 20 Mbps per eye even if the profile retains a dormant Custom
number; a Custom fallback policy uses its exact retained number.

The fallback object is not applied to an eligible direct encode. It is the
complete concrete alternative for capability-unavailable preflight and does not
expose unrelated AV1 values or dormant profile state.

## Explicit Generated MV-HEVC

Deterministic generated constraints are projected by the app as an explicit
generated request:

```json
{
  "mode": "mv_hevc",
  "route_intent": "generated",
  "quality_intent": {
    "mode": "custom"
  },
  "generated_eye_bitrate": {
    "mode": "custom",
    "mbps": 42
  },
  "generated_merge_quality": 88
}
```

Stage-4/5 restart, reusable intermediates, software HEVC, incompatible upscale
geometry, and out-of-range field of view continue to use this shape. These are
requested generated routes, not capability fallbacks, and never probe direct
encoding.

## AV1 And Existing Artifacts

AV1 remains exact CRF Custom:

```json
{
  "mode": "av1_sbs",
  "route_intent": "encode",
  "quality_intent": {
    "mode": "custom"
  },
  "crf": 32
}
```

A job starting after stage 5 normally carries no video quality fields:

```json
{
  "mode": "mv_hevc",
  "route_intent": "existing_artifact"
}
```

Stage 6 is the exception when file upscale remains active. It includes intent
without reintroducing dormant encoder controls:

```json
{
  "mode": "mv_hevc",
  "route_intent": "existing_artifact",
  "quality_intent": {
    "mode": "custom"
  }
}
```

In that shape, `encoding.upscale` must be enabled with its concrete quality.
Later existing-artifact stages force upscale disabled and omit intent, so stale
or invalid retained encoder values cannot block unrelated audio, mux, or move
restarts.

## Upscale Projection

`encoding.upscale` retains the v10 shape. Disabled requests contain only
`enabled: false`. Enabled requests include integer `quality` from 0 through 100.
For direct MetalFX, that number is the file-upscale setting to apply only if
preflight selects generated fallback. Generated routes apply it directly.
Selected generated reports include `upscale_quality`; selected direct reports
continue to use `upscale_mode: "metalfx"`. A stage-6 existing-artifact upscale
also reports its intent and `upscale_quality`; later restart stages report
neither because the setting is inactive.

## Requested And Selected Reporting

Every route report preserves the existing top-level `selected` route and
selected concrete fields. Protocol v11 adds checked quality metadata and one
nested `requested` settings object:

```json
{
  "intent": "automatic",
  "selected": "generated_mv_hevc",
  "reason": "direct_capability_unavailable",
  "quality_intent": {
    "mode": "custom"
  },
  "requested": {
    "route": "direct_mv_hevc",
    "rate_control": "average_bitrate",
    "bitrate_mbps": 37
  },
  "eye_bitrate_mbps": 42,
  "merge_quality": 88,
  "fallback_reason": "stereo_mv_hevc_encode_unavailable",
  "fallback_timing": "pre_input"
}
```

Requested route settings and selected route settings are therefore both
auditable without changing existing selected-field consumers. Preview child
jobs and full conversions use the same job projection, resolver, warning, result,
and artifact report. Capability fallback remains pre-input only; failures after
direct processing starts never replay through another lossy route.

## Compatibility

Protocol v10 requests and events are rejected with `protocol_mismatch` by v11
peers. Historical v10 fixtures remain checked rejection evidence; v11 fixtures
are the current shared Swift/Python contract. Profile document version 5 is
unchanged because the route-aware intent and retained Custom values already live
in the profile layer; protocol v11 changes only runtime projection and reporting.

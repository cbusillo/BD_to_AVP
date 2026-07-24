# Native Worker Protocol v10

Protocol v10 activates deterministic direct MV-HEVC routing. The native app
projects one route intent and only the video controls that intent can use; the
worker resolves one immutable execution route before conversion input is
consumed. The native app and bundled Python worker ship atomically and both
require version 10.

Observability events, lifecycle rules, ownership, heartbeats, request size
limits, audio-language selection, and terminal behavior are unchanged from
[Native Worker Protocol v9](native-worker-protocol-v9.md).

## Video Object

Every conversion and preview request contains one strict `encoding.video`
object. `mode` is `mv_hevc` or `av1_sbs`. `route_intent` determines the exact
remaining keys.

An automatic MV-HEVC request carries only its direct final-bitrate policy:

```json
{
  "mode": "mv_hevc",
  "route_intent": "automatic",
  "direct_bitrate": {
    "mode": "automatic"
  }
}
```

Custom bitrate mode adds one integer `mbps` value from 1 through 500. Automatic
mode omits `mbps`; it does not send a numeric sentinel. The initial
worker-owned automatic direct target is 40 Mbps, matching the aggregate budget
of the generated route's two default 20 Mbps eye encodes. A 1080p24 synthetic
check put direct output within 0.00045 minimum same-eye SSIM of the generated
path at that target, but did not satisfy the qualification harness's positive
quality-margin gate. Issue #364 completed the representative-corpus gate and
rejected one fixed Automatic target: matched direct targets span 0.5 to 16
Mbps, while a fixed 16 Mbps policy makes simpler cases 1.55 to 3.24 times
larger. The direct route is not approved as the stable default until #366 adds
content-adaptive Automatic rate control and reruns the gate. Native or streamed
4K upscaling remains a separate route and is not inferred from this policy.

A generated MV-HEVC request carries only generated-route controls:

```json
{
  "mode": "mv_hevc",
  "route_intent": "generated",
  "generated_eye_bitrate": {
    "mode": "custom",
    "mbps": 24
  },
  "generated_merge_quality": 75
}
```

The eye bitrate uses the same `automatic` or `custom` object contract. Its
automatic value remains 20 Mbps per eye. Merge quality is an integer from 0
through 100. A generated request never contains `direct_bitrate`.

AV1 carries only its active CRF:

```json
{
  "mode": "av1_sbs",
  "route_intent": "encode",
  "crf": 32
}
```

A resume that starts after video encoding carries no encode controls:

```json
{
  "mode": "mv_hevc",
  "route_intent": "existing_artifact"
}
```

The worker rejects unknown keys, invalid mode/intent combinations, automatic
bitrate objects containing `mbps`, and custom bitrate objects omitting `mbps`.
Protocol v9 fields such as `video_mode`, `av1_crf`, `left_right_bitrate`,
`link_quality`, and `mv_hevc_quality` are not part of the v10 encoding object.

## Upscale Object

`encoding.upscale` also omits inactive numeric values:

```json
{ "enabled": false }
```

An enabled request adds `quality` from 0 through 100. AV1 rejects enabled
upscaling. MV-HEVC upscaling selects the generated/file-backed route.

## Route Resolution

The worker resolves exactly one of `direct_mv_hevc`, `generated_mv_hevc`,
`av1`, or `existing_artifact` before source inspection or conversion input.
The resolved value is immutable for the job and is passed to stage planning,
preflight, and execution.

| Request or constraint | Resolved route |
| --- | --- |
| AV1 encode through stage 5 | `av1` |
| Restart at stage 6 or later | `existing_artifact` |
| Explicit generated MV-HEVC | `generated_mv_hevc` |
| MV-HEVC stage 4 or 5 restart | `generated_mv_hevc` |
| Reusable intermediates | `generated_mv_hevc` |
| Software HEVC | `generated_mv_hevc` |
| FX upscale | `generated_mv_hevc` |
| FOV outside the direct helper's 1° through 180° range | `generated_mv_hevc` |
| Eligible automatic MV-HEVC with supported helper | `direct_mv_hevc` |
| Eligible automatic MV-HEVC with missing or valid unavailable helper | `generated_mv_hevc` |

Capability-unavailable fallback uses automatic generated settings: 20 Mbps per
eye and merge quality 75. It never applies inactive generated custom values
from the profile. A malformed, contradictory, crashed, or timed-out capability
probe fails preflight rather than disguising a broken helper as unsupported.

Once the direct pipeline starts, encoder, normalizer, splitter, cancellation,
or input failures remain direct-route failures. The worker never silently
replays the job through another lossy route. The existing single-threaded
splitter retry may replay the same direct route once.

## Route Reporting

Every job emits a `video_route_selected` log or a visible
`video_route_fallback` warning. The same `video_route` object is included in
the conversion result and preview artifact:

```json
{
  "intent": "automatic",
  "selected": "generated_mv_hevc",
  "reason": "direct_capability_unavailable",
  "eye_bitrate_mbps": 20,
  "merge_quality": 75,
  "fallback_reason": "stereo_mv_hevc_encode_unavailable",
  "fallback_timing": "pre_input"
}
```

Direct reports `bitrate_mbps`; generated reports `eye_bitrate_mbps` and
`merge_quality`; AV1 reports `crf`; existing-artifact reports have no encode
controls. Preview child jobs resolve capability before their duration
inspection and use the same resolver and fallback report as full conversions.

## Direct Stage Contract

Direct MV-HEVC executes during existing stage 4,
`create_left_right_files`, with this pipeline:

```text
optional source FFmpeg -> edge264_test -> FFmpeg geometry normalizer -> mv-hevc-encoder
```

It writes the existing `<name>_MV-HEVC.mov` artifact and omits stage 5,
`combine_to_mv_hevc`. Generated MV-HEVC retains both existing eye filenames and
stage 5. Stage numbers, restart boundaries, later audio/subtitle muxing, and
final filenames remain unchanged. Final AAC preparation normalizes source
`5.1(side)` to standard `5.1` before encoding because Program Config Element
AAC is not exposed as an audio track by AVFoundation; `5.1(side)` is therefore
not eligible for Automatic AAC copy.

## Compatibility

Protocol v9 requests and events are rejected with `protocol_mismatch` by v10
peers. Historical v9 fixtures remain in the repository; v10 fixtures are the
current shared Swift/Python contract. Persisted profile version 4 remains
rollback-compatible and continues to retain inactive custom values locally;
only the active route projection crosses the worker boundary.

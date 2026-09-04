# Live Source Service

## Scope

Issue #717 promotes the verified direct-SSIF prototype into a source-only worker
service for unencrypted, single-clip, single-angle Blu-ray ISO and BDMV sources.
It does not select network peers, create relay sessions, invoke MakeMKV, or
handle AACS or BD+.

The worker operation is `start_live_source`. Its request contains:

- a `disc_image` or `blu_ray_folder` source;
- an empty destination workspace outside the source;
- an explicit playlist and selected audio PID;
- replay duration and byte limits; and
- an optional paired-MVC sample bound for deterministic qualification.

The service verifies `ssif_probe contract 2`, performs a strict source
inspection, validates the selected audio PID against the single eligible clip,
then starts one worker-owned helper process. Existing conversion and MakeMKV
paths are unchanged.

## Native Stream Contract

`ssif_probe stream-service` reads the selected SSIF once and writes framed
records to standard output:

```text
ssif_probe stream-service SOURCE PLAYLIST AUDIO_PID [MAXIMUM_PAIRS]
```

Each record begins with a 40-byte big-endian header:

| Offset | Size | Field |
| --- | ---: | --- |
| 0 | 4 | `SSFS` magic |
| 4 | 1 | frame contract version (`1`) |
| 5 | 1 | record kind: MVC (`1`), audio (`2`), completion (`3`) |
| 6 | 2 | flags; bit zero marks an MVC random-access boundary |
| 8 | 8 | monotonically increasing record sequence |
| 16 | 8 | raw 90 kHz PTS |
| 24 | 8 | raw 90 kHz DTS |
| 32 | 4 | primary payload length |
| 36 | 4 | secondary payload length |

An MVC record contains base-view Annex B followed by the matched dependent-view
Annex B payload. An audio record contains the selected PES payload. The helper
marks random access only when the base access unit contains an IDR NAL. It
rejects unavailable audio, non-monotonic MVC DTS, unmatched stereo PES data,
oversized pending data, unsupported source shapes, and malformed transport
records instead of emitting partial success.

`demux-service-file` applies the same framed contract to a synthetic M2TS file
for deterministic tests.

## Replay Contract

The Python worker consumes framed records and publishes keyframe-led GOP units:

```text
live-source.json
gop-<record-sequence>.mvc
gop-<record-sequence>.audio
gop-<record-sequence>.index.jsonl
```

The JSONL index binds each MVC and audio sample to its source sequence, normalized
90 kHz PTS/DTS, byte offset, payload lengths, and random-access state. The
manifest contains stable `mvc` and `audio:<pid>` identities, the exact earliest
and latest retained positions, and only finalized files.

Selected-audio PES that begins before the initial MVC replay keyframe is retained
as pre-roll and clamped to tick zero, so the replay index never advertises a
negative position before its first restart boundary.

Retention is bounded by both elapsed media time and total bytes. Eviction removes
whole GOP units, so every advertised restart point remains a produced keyframe.
If history is evicted, `earliest_available_ticks` advances honestly rather than
leaving a stale seek target.

The worker emits `artifact.ready` only after the first replay unit and manifest
are atomically durable. The payload names the manifest and stable stream IDs; it
contains no network, pairing, or session fields.

## Lifecycle

The app worker remains the process-group owner. Cancellation, app quit, or
worker termination stops the helper through the existing process-group path.
The source service also watches the worker cancellation token while blocked on
media reads and escalates an unresponsive helper from termination to kill.
Frame reads poll cancellation and enforce a 120-second no-progress deadline, so
a helper that starts but stops producing records cannot deadlock the worker.

Cancellation, malformed framing, source failure, and producer failure close all
pipes and remove the complete live-source workspace. A successful end of source
atomically finalizes the last GOP and marks the manifest complete.

## Remaining Boundaries

- #713 joins this source manifest to decode, MV-HEVC segmented encoding, selected
  audio delivery, and the authenticated relay.
- #718 implements the hermetic libbluray/libudfread builds, LGPL relinking
  materials, bundle-relative install names, signed-probe smoke, and package
  checks. It does not change MakeMKV conversion or this worker protocol.
- #712 retains the physical Vision Pro and paired-LAN qualification gate.
- Multi-clip, multi-angle, encrypted, and physical-disc sources remain explicit
  unsupported results for this service.

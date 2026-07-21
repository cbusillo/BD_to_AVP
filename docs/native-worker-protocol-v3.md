# Native Worker Protocol v3

> Historical protocol. Current title-aware behavior is documented in
> [Native Worker Protocol v9](native-worker-protocol-v9.md).

Protocol v3 adds immutable conversion-preview child jobs while preserving the
single-request, single-process JSON Lines transport from v2. The native app and
bundled Python worker ship atomically and both require version 3.

## Inspection

`inspect_source` keeps the v2 request shape. Successful inspection may now add
`duration_seconds` to `payload.result`; preview creation requires a positive
duration so beginning, middle, and end ranges can be resolved safely.

## Full Conversion

`convert_source` keeps explicit `source`, `destination`, `encoding`, and `job`
objects. The unused v2 `job.output_length` field is removed. Full conversion
always represents the complete source and remains independent from preview.

## Preview Child Job

`preview_source` uses the same resolved encoding snapshot as its planned full
conversion, but has its own job UUID, destination, lifecycle, and cleanup root:

```json
{
  "protocol_version": 3,
  "type": "job.start",
  "job_id": "22222222-2222-4222-8222-222222222222",
  "operation": "preview_source",
  "source": {
    "kind": "direct_file",
    "path": "/absolute/path/movie.mkv"
  },
  "destination": {
    "path": "/absolute/cache/22222222-2222-4222-8222-222222222222"
  },
  "encoding": {
    "transcode_audio": false,
    "audio_bitrate": 384,
    "left_right_bitrate": 20,
    "link_quality": true,
    "mv_hevc_quality": 75,
    "upscale_quality": 75,
    "fov": 90,
    "frame_rate": "",
    "resolution": "",
    "skip_subtitles": false,
    "crop_black_bars": false,
    "swap_eyes": false,
    "fx_upscale": false,
    "language_code": "eng",
    "remove_extra_languages": false
  },
  "job": {
    "start_stage": 1,
    "keep_files": false,
    "overwrite": true,
    "remove_original": false,
    "continue_on_error": false,
    "software_encoder": false,
    "output_commands": false,
    "keep_awake": true
  },
  "preview": {
    "parent_job_id": "11111111-1111-4111-8111-111111111111",
    "position": "middle",
    "duration_seconds": 60
  }
}
```

The first preview slice accepts MKV, MTS, M2TS, and ISO sources. Preview jobs
must start at stage 1, overwrite only their isolated cache, retain no stage
files, never remove source media, and never continue from partial output.

The worker resolves the requested position against source duration, clamps
short sources, aligns the start to the preceding decodable keyframe, and creates
one bounded MKV that still covers the requested interval before MVC splitting. Video, audio,
subtitle extraction, crop detection, and both-eye encoding therefore consume
the same bounded source. ISO preview still materializes its selected title with
MakeMKV before applying the range; physical-disc and Blu-ray-folder preview are
outside this first slice.

## Artifact Event

Once the finalized preview exists, the worker emits nonterminal
`artifact.ready` with `payload.artifact`. The artifact includes its output path,
size, parent job UUID, selected position, keyframe-aligned start and duration, and total
source duration. The following terminal `job.completed` repeats the same object
under `payload.preview_result`.

The app accepts artifacts only inside the preview job's owned cache directory.
That directory remains leased while AVPlayer uses the file, is removed when the
preview is discarded, and is pruned after 24 hours if abandoned.

## Progress Hints

Conversion and preview jobs may add an optional `payload.progress` object to
`stage.started` and `heartbeat` events:

```json
{
  "current_stage": 4,
  "total_stages": 13,
  "stage_fraction": 0.42
}
```

`current_stage` and `total_stages` describe the exact stages the current job
will execute after applying its restart stage and optional features. The
optional `stage_fraction` is a zero-to-one fraction reported by the tool running
the current stage; it is not overall conversion completion or an ETA. Workers
omit `stage_fraction` when the tool has no trustworthy denominator, while the
heartbeat and elapsed time continue to indicate activity.

The progress object is an additive optional v3 field. Existing v3 decoders may
ignore it, and newer decoders must preserve the indeterminate fallback when it
is absent or invalid.

## Compatibility

Mixed v1, v2, and v3 processes fail before media inspection or destination
mutation. Adding another required field or changing event meaning requires a
new protocol version.

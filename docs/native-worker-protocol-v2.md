# Native Worker Protocol v2

The native macOS app communicates with its bundled Python engine over a bounded
JSON Lines (JSONL) protocol. Version 2 makes the source kind explicit and adds
single Blu-ray folder inspection and conversion. Physical-disc and batch-folder
conversion remain outside this contract.

## Transport

- One worker process handles exactly one immutable job.
- The app writes one UTF-8 JSON object followed by `\n`, then closes standard input.
- The worker writes and flushes one UTF-8 JSON event per standard-output line.
- Standard output is protocol-only; helper output and tracebacks use standard error.
- Requests are limited to 64 KiB and individual events to 1 MiB.

## Source Contract

Every request contains exactly one absolute source path and one source kind:

| Kind | Required path |
| --- | --- |
| `direct_file` | Existing `.mkv`, `.mts`, or `.m2ts` file. |
| `disc_image` | Existing `.iso` file. |
| `blu_ray_folder` | Folder containing `BDMV`, or the `BDMV` folder itself. |

The worker validates that kind and path agree before launching tools. A selected
`BDMV` folder is normalized to its parent disc root. Blu-ray folders are passed
to MakeMKV as `file:<absolute path>`; ISO files are passed as
`iso:<absolute path>`. A conversion destination may not be inside a Blu-ray
source folder.

## Inspection Request

```json
{
  "protocol_version": 2,
  "type": "job.start",
  "job_id": "d870b63d-939e-4584-a2a4-cd441f628cbe",
  "operation": "inspect_source",
  "source": {
    "kind": "blu_ray_folder",
    "path": "/absolute/path/to/Disc"
  }
}
```

Inspection completion places `name`, `resolution`, `frame_rate`, `interlaced`,
and optional `size_bytes` under `payload.result`. Directory size is intentionally
not reported because filesystem directory metadata is not the media size.

## Conversion Request

```json
{
  "protocol_version": 2,
  "type": "job.start",
  "job_id": "d870b63d-939e-4584-a2a4-cd441f628cbe",
  "operation": "convert_source",
  "source": {
    "kind": "direct_file",
    "path": "/absolute/path/to/movie.mkv"
  },
  "destination": {
    "path": "/absolute/path/to/output-folder"
  },
  "encoding": {
    "transcode_audio": true,
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
    "overwrite": false,
    "remove_original": false,
    "continue_on_error": false,
    "software_encoder": false,
    "output_commands": false,
    "keep_awake": true,
    "output_length": "full_movie"
  }
}
```

All conversion fields are required and unknown fields are rejected. Protocol v2
accepts only `job.output_length = "full_movie"`. Completion places
`source_path`, `destination_path`, `output_path`, and `size_bytes` under
`payload.conversion_result` after the final `.mov` exists.

## Events And Recovery

Every event repeats `protocol_version`, `job_id`, and a gap-free `sequence`.
`worker.ready` is sequence zero and reports the worker-owned process group.
Supported event types are `worker.ready`, `job.started`, `stage.started`,
`heartbeat`, `log`, `warning`, `job.decision_required`, `job.completed`,
`job.failed`, and `job.cancelled`.

MakeMKV errors that may leave a usable intermediate MKV terminate with
`mkv_creation_decision_required`. A retry is a new immutable job with
`continue_on_error=true` and the requested resume stage. The worker never treats
a partial MKV as safe without that explicit decision.

## Cancellation And Ownership

The worker owns a dedicated process group and reports it in `worker.ready`. The
native app verifies the protocol version, job UUID, launched PID, and live group
before signaling. Cancellation sends `SIGTERM` to that group, waits two seconds,
and uses `SIGKILL` only as a bounded fallback. `job.cancelled` is emitted only
after descendant cleanup.

## Compatibility

The app and bundled worker ship atomically and both require protocol version 2.
Mixed v1/v2 processes fail before media inspection or destination mutation.
Changes to required fields or event meaning require another protocol version.

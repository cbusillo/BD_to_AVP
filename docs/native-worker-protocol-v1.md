# Native Worker Protocol v1

> Historical contract. The native app and bundled worker now use
> [Native Worker Protocol v9](native-worker-protocol-v9.md).

The native macOS prototype communicates with the existing Python engine over a
bounded JSON Lines (JSONL) protocol. The boundary is intentionally independent
of SwiftUI and PySide so the processing engine can remain Python while the app
owns native lifecycle, presentation, and process supervision.

## Transport

- One worker process handles exactly one immutable job.
- The native app writes one UTF-8 JSON object followed by `\n` to standard
  input, then closes standard input.
- The worker writes one UTF-8 JSON event per line to standard output and flushes
  every event.
- Standard output is protocol-only. Python tracebacks and legacy tool output go
  to standard error as a secondary diagnostic stream.
- Requests are limited to 64 KiB and individual events to 1 MiB.

## Job Request

`inspect_source` reads metadata from one existing `.iso`, `.mkv`, `.mts`, or
`.m2ts` file. ISO inspection uses the installed MakeMKV application to identify
the longest MVC title; existing video files reuse the production FFprobe path.

```json
{
  "protocol_version": 1,
  "type": "job.start",
  "job_id": "d870b63d-939e-4584-a2a4-cd441f628cbe",
  "operation": "inspect_source",
  "source": {
    "path": "/absolute/path/to/movie.m2ts"
  }
}
```

`convert_source` converts one existing `.iso`, `.mkv`, `.mts`, or `.m2ts` file
through the production conversion engine. ISO conversion requires MakeMKV and
automatically selects the longest MVC title before continuing through the same
pipeline. The request is immutable: source and destination paths must be
absolute, all conversion options must be present, and unknown fields are
rejected. This slice does not enable physical disc, Blu-ray folder,
source-folder, queue, or batch conversion through the worker.

`destination.path` is an absolute output folder. The worker returns the actual
final `.mov` file path produced by the existing engine.

```json
{
  "protocol_version": 1,
  "type": "job.start",
  "job_id": "d870b63d-939e-4584-a2a4-cd441f628cbe",
  "operation": "convert_source",
  "source": {
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

## Event Envelope

```json
{
  "protocol_version": 1,
  "type": "stage.started",
  "job_id": "d870b63d-939e-4584-a2a4-cd441f628cbe",
  "sequence": 2,
  "payload": {
    "stage": "inspect_source",
    "message": "Reading video metadata"
  }
}
```

Every event repeats the protocol version and job UUID. `worker.ready` is required
at sequence zero and must report the launched worker PID as its owned process
group. `sequence` then increases by one without gaps. A missing, duplicated,
out-of-order, unknown, oversized, or truncated event fails the job in the shell
rather than leaving the interface in an ambiguous state.

## Event Types

| Type | Terminal | Purpose |
| --- | --- | --- |
| `worker.ready` | No | Confirms worker version and owned process group. |
| `job.started` | No | Confirms the immutable job was accepted. |
| `stage.started` | No | Names the current stage and user-facing activity. |
| `heartbeat` | No | Reports elapsed activity while a stage is still alive. |
| `log` | No | Structured diagnostic activity; raw logs remain on stderr. |
| `warning` | No | Reports a recoverable warning without ending the job. |
| `job.decision_required` | Yes | Stops safely when user input is required. |
| `job.completed` | Yes | Returns the operation result. |
| `job.failed` | Yes | Returns a stable error object. |
| `job.cancelled` | Yes | Confirms cancellation and owned-descendant cleanup. |

The inspection result contains `name`, `resolution`, `frame_rate`, `interlaced`,
and `size_bytes`.

Inspection completion places its metadata under `payload.result`. Conversion
completion places `source_path`, `destination_path`, `output_path`, and
`size_bytes` under `payload.conversion_result`. `output_path` is the completed
`.mov` file and is emitted only after the file exists. Existing output collisions
fail with retryable `output_exists` unless `job.overwrite` is true; unsupported
source kinds fail rather than being silently skipped or routed into disc/folder
conversion. Protocol v1 accepts only `job.output_length = "full_movie"`.

Conversion stages use the existing engine boundaries: `configure`, `preflight`,
`inspect_source`, `create_mkv`, `probe_color`, `detect_crop`,
`extract_mvc_and_audio`, `extract_subtitles`, `create_left_right_files`,
`combine_to_mv_hevc`, optional `upscale_video`, `transcode_audio`,
`create_final_file`, and `move_files`. Heartbeats report the current stage.
Structured `log` and `warning` events are protocol events; raw helper output and
Python diagnostics remain on standard error.

Subtitle extraction failures that require a user choice end with
`job.decision_required` with a structured `decision` containing `id`, `prompt`,
`choices`, and optional `details` instead of guessing. Cancellation maps to
`job.cancelled`; dependency failures, helper failures, FFmpeg failures, existing
outputs, missing outputs, and unsupported sources map to `job.failed` with stable
error codes and optional details.

MakeMKV warnings that stop ISO materialization use the same terminal decision
surface with `mkv_creation_decision_required`. The recovery details direct the
user to confirm that a usable MKV exists, enable Continue on Error, and restart
from Extract MVC and Audio; the worker never guesses that a partial MKV is safe.

## Cancellation And Ownership

The Python worker ensures it owns a process group before starting work. It uses
the dedicated group supplied by the native launch when present, otherwise it
creates a new POSIX session, and reports the group ID in `worker.ready`. The
native client trusts that group only when the event has the expected protocol
version and job UUID and the group ID equals the launched worker PID. It verifies
the live group with the kernel before signaling. Cancellation sends `SIGTERM`
only to that owned group, waits two seconds, and uses `SIGKILL` only as a bounded
fallback. Before the ready event, cancellation targets only the direct worker.

The worker's signal handler only marks cancellation, avoiding reentrant cleanup.
The worker and native group signal terminate descendants, with a final recursive
`psutil` cleanup before `job.cancelled` confirms completion. The shell also uses
this path for confirmed application Quit and last-window close. Terminal UI is
not committed until the worker process itself has exited.

## Compatibility

Protocol changes that alter required fields or event meaning require a new
integer `protocol_version`. Readers reject versions they do not understand.
Optional fields may be added to payloads without changing v1.

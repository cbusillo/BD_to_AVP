# Native Worker Protocol v1

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

The v1 prototype supports only `inspect_source` for `.mts` and `.m2ts` files.
The operation is read-only and reuses the production FFprobe metadata path.

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
| `worker.ready` | No | Confirms worker version and its owned process-group ID. |
| `job.started` | No | Confirms the immutable job was accepted. |
| `stage.started` | No | Names the current processing stage and user-facing activity. |
| `heartbeat` | No | Reports elapsed activity while a stage is still alive. |
| `log` | No | Optional structured diagnostic activity; raw logs remain on stderr. |
| `warning` | No | Reports a recoverable warning without ending the job. |
| `job.decision_required` | Yes | Stops safely when future work requires an unsupported user decision. |
| `job.completed` | Yes | Returns the operation result. |
| `job.failed` | Yes | Returns a stable error code, message, optional details, and retryability. |
| `job.cancelled` | Yes | Confirms cancellation and owned-descendant cleanup. |

The current inspection result contains `name`, `resolution`, `frame_rate`,
`interlaced`, and `size_bytes`.

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

# Native Worker Protocol v8

> Historical contract. Current clients must use
> [Native Worker Protocol v10](native-worker-protocol-v10.md).

Protocol v8 carries canonical structured observability through the worker JSONL
stream. The native app and bundled Python worker ship atomically and both
require version 8.

Request fields, conversion options, ownership, heartbeat behavior, and existing
event meanings are unchanged from
[Native Worker Protocol v7](native-worker-protocol-v7.md).

## Observability Event

The worker may emit a non-terminal `observability` event between `job.started`
and the terminal event:

```json
{
  "protocol_version": 8,
  "type": "observability",
  "job_id": "11111111-1111-4111-8111-111111111111",
  "sequence": 3,
  "payload": {
    "event": {
      "schema": "bd_to_avp.observability",
      "schema_version": 1,
      "emitter": "worker",
      "stream_id": "11111111-1111-4111-8111-111111111111",
      "sequence": 0,
      "occurred_at": "2026-07-18T00:00:00.000000Z",
      "elapsed_ms": 0,
      "kind": "tool.started",
      "severity": "info",
      "privacy": "private",
      "redaction": "raw",
      "context": {
        "correlation": {}
      },
      "data": {}
    }
  }
}
```

`payload.event` is the canonical event defined by the separately versioned
[structured observability contract](observability.md). It is not flattened into
the worker payload and must pass the canonical schema, privacy, and size checks.

The outer worker `sequence` remains the gap-free transport order for every JSONL
event. The nested observability `sequence` is independent and orders events only
within its canonical stream. Consumers must not require the two values to match.

Observability events advance worker sequence validation but do not alter job
lifecycle, progress, or terminal state. A canonical event that cannot be
serialized or emitted is dropped from diagnostics and must not fail the job.

## Compatibility

Protocol v7 readers model worker event types as a closed set and cannot decode
`observability`. Version 8 therefore makes the additive event explicit instead
of exposing it under the v7 contract. Requests and events with any other worker
protocol version remain rejected.

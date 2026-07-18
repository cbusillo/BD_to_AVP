# Structured Observability

BD_to_AVP uses one local structured event contract to describe conversion
activity without turning support diagnostics into background telemetry.

## Contract

`bd_to_avp.observability` schema version 1 is an append-only event record shared
by Python and Swift. Each emitter run owns a random `stream_id` and a strictly
increasing `sequence`. `occurred_at` is an RFC 3339 UTC timestamp and
`elapsed_ms` is monotonic time since the stream began.

Events use stable string `kind` values so newer nonterminal kinds remain
decodable by older readers. Structured context correlates a job, stage, tool
invocation, and local process. Structured data carries bounded messages,
progress, artifact growth, storage state, failures, cancellation state, and
retention counters.

Schema version 1 readers ignore unknown additive object fields. A change to a
required field, enum vocabulary, or field meaning requires a new schema version
and an atomic producer/consumer update.

The observability schema version is independent from the native worker protocol
version. Typed inspection and conversion results remain worker-control data
rather than being copied into observability events.

## Worker Channels

The native worker preserves two independent channels:

- Standard output contains low-volume JSONL control and semantic lifecycle
  events only.
- Standard error contains continuously drained, bounded raw child-tool
  diagnostics.

High-volume MakeMKV, FFmpeg, or splitter output must not be copied into worker
protocol events. Structured events identify the active tool, stage, recent
activity, process outcome, and active artifact while the bounded diagnostic tail
retains recent raw evidence.

## Privacy

Every event declares its maximum privacy level and redaction state. Secrets are
omitted instead of being logged. Executable paths, source paths, filenames,
movie titles, command arguments, environment values, raw process identifiers,
serial numbers, and reusable hashes are not exportable as-is.

Local raw records may contain private paths needed for active-file sampling.
User-created support bundles apply a second redaction boundary that replaces
paths and job identifiers with bundle-scoped tokens, removes process IDs and
command details, redacts credential-like text, and coarsens sizes.

## Retention

In-memory and on-disk sinks are bounded. Dropped event and byte counts are part
of the observable state so truncation is explicit. Rotating JSONL files are
written through an owner-only directory, use inter-process locking, reject
symlinks, and are created with mode `0600`. Sink failures and retention limits
must never fail or block a conversion. Sequence order is authoritative within a
single `stream_id`.

## Migration

1. Add the shared models, sinks, fixtures, and runtime context without changing
   conversion behavior.
2. Replace fragmented child execution with one binary-safe streaming runner.
3. Project semantic runner activity into the worker protocol and bounded raw
   output into the diagnostic channel.
4. Make the native diagnostic recorder and support bundle consume the shared
   event model.
5. Remove direct conversion `print` calls, stdout interception, sidecar logs,
   and superseded diagnostic structures after every producer has migrated.

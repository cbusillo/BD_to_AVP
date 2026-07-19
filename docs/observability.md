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

- Standard output contains low-volume JSONL control, semantic lifecycle, and
  canonical observability envelopes. An observability envelope uses worker
  type `observability` and nests the unchanged schema-v1 event under
  `payload.event`; worker and observability sequence numbers remain independent.
- Standard error contains continuously drained, bounded raw child-tool
  diagnostics.

High-volume MakeMKV, FFmpeg, or splitter output must not be copied into worker
protocol events. Structured events identify the active tool, stage, recent
activity, process outcome, and active artifact while the bounded diagnostic tail
retains recent raw evidence.

The macOS client decodes canonical events with the shared Swift model, advances
the worker transport sequence without changing lifecycle progress, and projects
their stable kind, stage, severity, message, detail, and failure fields into the
existing bounded diagnostic history. Unsupported schemas, secret privacy, and
oversized text fail decoding rather than falling back to unredacted strings.

Worker conversion stages pass the same `RunContext` and cancellation token into
every child-tool wrapper. Canonical child-process events therefore retain the
active stage identifier across preflight, preview preparation, probing, MVC/AV1
encoding, subtitles, audio preparation, upscale, and final muxing.

## Child Processes

`ChildProcessRunner` is the only supported path for ordinary conversion helper
processes. It launches each helper in a new process group, drains binary stdout
and stderr concurrently, applies incremental UTF-8 replacement for diagnostic
views, and retains bounded captures and tails. Output capture overflow fails the
tool by default rather than silently parsing incomplete MakeMKV or probe data;
callers that only need a diagnostic prefix may explicitly select bounded
truncation.

When a caller supplies a `RunContext`, the runner emits low-volume lifecycle,
heartbeat, progress, cancellation, failure, and active-artifact events through
a bounded asynchronous delivery queue. Raw command arguments, environment
values, and child output are never copied into those semantic events. A blocked
or failed event sink cannot block pipe draining or process cancellation.

Line, progress, raw-output, and artifact resolver hooks are internal producer
boundaries and must return promptly without performing unbounded I/O. Pipe
readers remain independent from those hooks, and bounded dispatch overflow
fails the tool rather than silently dropping parser input.

`ProcessPipelineRunner` connects linear helper graphs with native OS pipes while
using `ChildProcessRunner` to supervise every stage independently. Intermediate
binary payloads are never decoded or retained as diagnostics; bounded stderr,
heartbeats, exit state, cancellation, and final output artifact growth remain
observable for the producer, MVC splitter, and encoder. Parent pipe handles are
closed after launch so EOF and backpressure remain correct.

The legacy `run_command` API is a compatibility adapter over the shared runner.
MakeMKV inspection and ripping identify the tool explicitly, emit parsed robot
progress, sample the actively growing MKV, and honor cancellation while the tool
is running. Compatibility calls that ignore output retain a bounded prefix and
continue; callers that parse complete output explicitly fail on truncation.

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
   The ordinary command path, MakeMKV, FFmpeg graph execution, FFprobe metadata,
   FFmpeg capability checks, binary subtitle extraction, and the native MVC
   linear pipeline are migrated. Binary payloads are redirected to artifacts or
   native pipes rather than entering diagnostic capture.
3. Project semantic runner activity into the worker protocol and bounded raw
   output into the diagnostic channel.
4. Make the native diagnostic recorder and support bundle consume the shared
   event model.
5. Remove direct conversion `print` calls, stdout interception, sidecar logs,
   and superseded diagnostic structures after every producer has migrated.

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
- Standard error contains continuously drained, bounded raw child-tool stderr.

High-volume MakeMKV, FFmpeg, or splitter output must not be copied into worker
protocol events. Structured events identify the active tool, stage, recent
activity, process outcome, and active artifact. A nonblocking worker relay sends
raw child stderr to standard error with a `512 KiB` pending-byte bound and
`16 KiB` chunks; overload drops the oldest pending chunks and emits an explicit
structured truncation warning. Successful stdout parser payloads such as
FFprobe JSON are never relayed into the diagnostic channel.

The macOS client decodes canonical events with the shared Swift model, advances
the worker transport sequence without changing lifecycle progress, and projects
their stable kind, stage, severity, message, detail, and failure fields into the
existing bounded diagnostic history. Unsupported schemas, secret privacy, and
oversized text fail decoding rather than falling back to unredacted strings.
For multi-artifact stages such as `create_left_right_files`, the live status view
retains the current left-eye and right-eye artifact samples separately. A quiet
helper process with recently growing artifacts is therefore classified as active
work rather than as an immediate stall. The technical-details panel reserves the
stall warning for runs that have neither recent tool output nor recent expected
artifact growth.

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

All ordinary child-tool call sites use typed `run_process_capture`, FFmpeg, or
pipeline wrappers over the shared runner. MakeMKV inspection and ripping identify
the tool explicitly, emit parsed robot progress, sample the actively growing
MKV, and honor cancellation while the tool is running. Callers that need only a
diagnostic prefix select bounded truncation explicitly; callers that parse
complete output fail on truncation.

## Presentation Sinks

The remaining direct terminal output is intentional public presentation, not an
operational logging channel:

- the CLI command echo and spinner used when no worker `RunContext` is active;
- the explicit Apple Vision OCR import-smoke result;
- `cli_message` fallbacks for interactive CLI-only status and warnings.

Those fallbacks are suppressed during worker execution, where structured worker
events and the bounded raw diagnostic channel are authoritative. Unexpected
worker exceptions may still write a traceback to standard error for crash
diagnosis; standard output remains protocol-only.

## Privacy

Every event declares its maximum privacy level and redaction state. Secrets are
omitted instead of being logged. Executable paths, source paths, filenames,
movie titles, command arguments, environment values, raw process and thread
identifiers, serial numbers, and reusable hashes are not exportable as-is.

Local raw records may contain private paths needed for active-file sampling.
User-created support bundles apply a second redaction boundary that replaces
paths and job identifiers with bundle-scoped tokens, removes process and thread
IDs and command details, redacts credential-like text and arbitrary media-metadata
values, and coarsens sizes.
Public and private classifications are retained in exported event metadata;
private text is eligible for export only after that second redaction pass.
Events marked `redaction=omitted` never project their message or detail text.

## Retention

All diagnostic queues, histories, captures, and native on-disk segments are
bounded. Dropped event and byte counts are part of the observable state so
truncation is explicit. The Python worker does not maintain a second persistent
event store; it transports canonical events to the native owner. Sink failures
and retention limits must never fail or block a conversion. Sequence order is
authoritative within a single `stream_id`.

The macOS app persists canonical events under its Application Support container
in an owner-only `Observability` directory. It retains at most three `4 MiB`
JSONL segments (`12 MiB` total) and bounds pending writes to `4 MiB`; when that
queue is full, the oldest pending records are dropped so recent failure evidence
survives. Writes run on a utility queue and use a nonblocking inter-process lock.
Directory and file ACLs are cleared in addition to enforcing modes `0700` and
`0600`. Startup writes repair crash-truncated JSONL tails and remove segments
outside the current retention policy. Normal app termination gives pending
writes a bounded drain window. Raw local segments are never copied into support
bundles. Bundles include only the existing redacted event projection plus
path-free retention counters.

## Migration Status

The observability migration is complete. Shared schema models, the binary-safe
runner, worker transport, native persistence, live status, support-bundle
projection, and cancellation propagation are the only supported operational
paths. Superseded command adapters, Python event stores, global process killers,
raw native diagnostic-log state, worker stdout interception, and orphan sidecar
logging have been removed. The Python GUI retains a thread-scoped presentation
bridge so intentional CLI spinner/status text remains visible in that supported
interface; it is not used for worker protocol or support persistence.

`uv run python scripts/validate_observability_migration.py` is the durable
qualification guard. It fails when production code introduces a direct process
spawn outside `bd_to_avp.process_runner`, a conversion/runtime `print` outside
the reviewed presentation sinks, an ad-hoc `.log` path, or a removed legacy
symbol. CI runs the guard before formatting, lint, type checking, and tests.

# Native Support Bundle v1

The native macOS app can capture a local, privacy-safe support archive while a
worker is active or after it reaches a terminal state. Capture reads app state,
the bounded worker-output tail, and filesystem metadata. It does not signal,
pause, restart, or otherwise mutate the worker.

The main actor first freezes an immutable lifecycle, queue, event-history, and
active-worker snapshot. Filesystem probes, redaction, JSON encoding, DEFLATE,
and archive file I/O then run on a utility task so capture cannot stall worker
event delivery or stdout consumption.

## Archive Contract

The archive is a ZIP file using raw DEFLATE entries. Its filename contains only
a UTC timestamp and a random bundle identifier prefix.

- `manifest.json` contains the versioned contract, lifecycle, job/settings
  summary, file sizes, privacy rules, and truncation metadata. It is limited to
  64 KiB.
- `events.jsonl` contains the timestamped worker and client lifecycle-event
  tail. It is limited to 320 KiB.
- `storage.json` contains current storage probes and sampled
  destination/output growth. It is limited to 160 KiB.
- `tool-tail.txt` contains the redacted live worker stderr/tool-output tail and
  a metadata header. It is limited to 640 KiB.

The complete uncompressed payload is limited to 1,500,000 bytes. The ZIP is
rejected before it is exposed to callers if it exceeds 2,097,152 compressed
bytes.

## Manifest Fields

- `schema_version`: Literal `1`.
- `bundle_id`: Random UUID generated for this capture. It is not derived from
  user data.
- `created_at`: UTC capture timestamp.
- `archive`: Format and size ceilings plus payload bytes excluding the
  manifest.
- `app`: App version/build, distribution channel, numeric macOS version, CPU
  architecture, and worker protocol version. No hostname, hardware model,
  serial, locale, or environment is included.
- `worker`: Worker version, active state, whether cancellation had already been
  requested, and the most recent process exit status when available. Process
  and process-group identifiers remain in-memory for worker control and are
  never serialized.
- `lifecycle`: Phase, operation, active mode, elapsed time, progress, bounded
  stage/activity/warning text, failure code/message/details, retryability, and
  recovery-choice identifiers. All free text is redacted.
- `job`: Per-bundle job token, operation, source kind, path tokens, and selected
  non-identifying encoding/job settings. Custom profile names, title IDs/names,
  preferred language, and override values are omitted.
- `batch`: Queue kind, total/active counts, and counts by status. Item IDs,
  source names, and output names are omitted.
- `files`: Entry names, uncompressed byte counts, and truncation flags.
- `truncation`: Original, retained, history-dropped, archive-dropped, and
  field-truncated counts/bytes for each bounded stream.
- `privacy`: Human-readable included/excluded categories, redaction-rules
  version, bundle-only token scope, and the media/storage size-rounding
  contract. Privacy rules version `2` uses round-down quanta of 256 MiB for
  media/artifact file sizes and 16 GiB for volume capacities.

## Event Fields

Each `events.jsonl` line carries the schema version, reception timestamp,
source (`worker` or `client`), event name, per-bundle job token, sequence,
lifecycle phase, operation/mode, bounded stage/message/details,
elapsed/progress values, warning/failure identifiers, retryability, recovery
choices, rounded result size, worker version, exit status, and a field-truncation
flag. PID and process-group fields are not present.

Worker result objects are never serialized. Source inspection titles, output
paths, raw job JSON, and arbitrary event payload fields are excluded. The
history is a contiguous newest-history tail when older records must be dropped.
Its in-memory byte budget counts JSON string escaping and serialization
overhead rather than only the unescaped Swift strings.

Client lifecycle records carry an explicit originating job token when they are
job-specific. Batch-wide records carry no job token, and retry records use the
retried item's last job instead of inheriting whichever job ran most recently.

## Storage Fields

Current probes cover the selected source, destination directory, and planned
output path when those roles exist. Samples cover destination capacity and
planned-output size/modification age during worker heartbeats and terminal
transitions.

Each probe contains only a role, per-bundle path token, availability status,
directory/read/write booleans, rounded-down file size, modification age,
rounded-down free/total capacity, read-only state, and a normalized error
category. File sizes use 256 MiB quanta; volume capacities use 16 GiB quanta.
The JSON names these values `*_rounded_bytes` and the manifest records the
quanta and rounding direction. Volume names, device paths, filesystem labels,
filenames, exact media sizes, exact capacities, and exception descriptions are
excluded. Missing and inaccessible paths are represented explicitly. The probe
uses throwing filesystem metadata reads, including underlying POSIX errors, so
permission failures cannot be misclassified as a nonexistent path by a
non-throwing existence check.

Archive-entry sizes, truncation counters, and byte ceilings remain exact because
they enforce the local 1,500,000-byte payload and 2,097,152-byte ZIP limits;
they do not describe host storage or media files.

## Tool Tail

`WorkerProcessClient` continuously drains stderr while the process runs into a
512 KiB UTF-8 tail. DispatchIO callbacks append bounded chunks directly to the
tail; there is no unbounded `AsyncThrowingStream` continuation queue between
the pipe and the bound. This prevents pipe backpressure while preserving exact
original/retained/dropped byte accounting. `tool-tail.txt` begins with those
counts and a `truncated` flag before the redacted retained text.

The existing UI-facing diagnostic string is separately limited to 128 KiB.

The client does not copy the parent environment into the worker. It inherits
only `HOME`, `TMPDIR`, and `LANG`/`LC_ALL`/`LC_CTYPE`, supplies a fixed search
path for macOS system and conventional Homebrew tool locations, and sets
`PYTHONUNBUFFERED=1`. Development overrides, loader/Python injection variables,
credential variables, agent sockets, and unrelated host metadata are omitted.

## Redaction Rules

Redaction is deterministic for a fixed bundle ID and encounter order. It does
not use reusable hashes.

- Full paths, file URLs, home-relative paths, Windows paths, shell-escaped
  paths, volume paths, and `file:`, `dev:`, or `iso:` path forms become tokens
  such as `<path:89ABCDEF:001>`.
- The same path receives the same token in manifest, events, storage, and tool
  output for one bundle. A new bundle uses a new random scope.
- Known source display names, inspected names, selected-title names, output
  names, and volume names are replaced before serialization.
- Known conversion-tool command lines retain only the executable name plus
  `<arguments:redacted>`.
- Standalone command arguments carrying title values are replaced with
  `<title:redacted>`, including quoted and shell-escaped values.
- Authorization values, bearer tokens, API keys, passwords, secrets,
  generic secret-like environment assignments, GitHub/AWS-style credentials,
  JWTs, serial/device identifiers, long hexadecimal identifiers, email
  addresses, IP addresses, and MAC addresses are removed. Assignment matching
  accepts optional whitespace and quoted keys/values.
- PID/PGID assignments found in free text are removed in addition to omitting
  the structured fields.
- UUID-like identifiers found in free text receive per-bundle identifier tokens.
  Structured worker job UUIDs receive per-bundle job tokens.
- ANSI escapes and unsafe control characters are removed.
- Every free-text field is redacted and byte-bounded before JSON encoding.
  Truncation markers are included inside, not appended beyond, each advertised
  UTF-8 byte cap.

The bundle never contains media bytes, screenshots, environment dumps, raw
commands, source titles, raw paths, raw process identifiers, exact media/storage
sizes, serials, or reusable file fingerprints.

## Local Foundation APIs

`await ConversionViewModel.captureDiagnosticBundle(in:)` asynchronously returns
a `DiagnosticBundleArtifact` without changing worker state. The artifact
exposes:

- `archiveURL` and `suggestedFilename` for an upload or save panel.
- `preview` with exact included/excluded categories, entry sizes, archive size,
  and truncation notices.
- `sharingItems` for `NSSharingServicePicker` or equivalent native sharing UI.
- `saveCopy(to:overwrite:)` for Save-panel destinations.
- `removeLocalCopy()` for explicit cleanup after the caller no longer needs the
  archive.

## Native Submission Flow

The Activity footer exposes one support action whenever the current session has
diagnostic evidence. Failed source cards repeat the same action as a low-noise
secondary entry point. Capturing, reviewing, uploading, cancelling, saving, and
sharing diagnostics never call `stopActiveWorker()` or
`WorkerProcessRunning.cancel()`.

The flow captures the archive before presenting consent and displays
`DiagnosticBundleArtifact.preview` directly:

- Included and excluded categories are shown verbatim from the artifact.
- The compressed ZIP size, 2 MiB ceiling, member sizes, and truncation notices
  are visible before upload consent.
- Upload uses one immutable in-memory copy of the exact `archiveURL` bytes for
  both SHA-256 metadata and the authorized PUT.
- Cancelling or failing an upload keeps the reviewed local artifact and offers
  **Retry Send**, **Save a Copy…**, native **Share…**, and explicit discard.
- Every retry starts with a new `POST /v1/reports`; upload authorizations are
  never reused.
- Success presents a selectable and copyable support code plus the retention
  expiry before the temporary local copy is removed.

When no service endpoint is configured, the same action becomes **Save
Diagnostics…**. Capture and consent/review remain available, but the sheet
offers only local Save/Share fallback instead of a disabled network action.

## Endpoint Configuration

The app reads the public service origin only from the
`BDToAVPSupportDiagnosticsEndpoint` Info.plist key. It is not stored in user
defaults and there is no endpoint field in Settings. Missing, blank, malformed,
non-HTTPS, credential-bearing, path-bearing, query-bearing, or fragment-bearing
values fail closed to local-only mode.

`macos/project.yml` expands the key from the
`BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT` build setting. The native packaging
helper accepts the same-named environment value only at build time and rejects
anything except a credential-free HTTPS origin. For example:

```sh
BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT=https://support.example \
  uv run python scripts/native_app.py package
```

No service credential is embedded in the app. Debug builds may remain
local-only, but Release packaging fails unless the repository variable
`SUPPORT_DIAGNOSTICS_ENDPOINT` supplies an approved production HTTPS origin.
This prevents a signed build from silently shipping without online reporting.

## Upload Client Security

The native client uses an ephemeral `URLSession` with no cache, cookie store, or
credential store. It sends the v1 `application/zip` metadata request to
`POST /v1/reports`, validates the returned schema, support code, expiry,
authorization methods, required headers, and exact report paths, then sends the
same immutable ZIP bytes with the returned PUT headers.

Both returned upload and status URLs must remain HTTPS and match the configured
scheme, host, and effective port. Redirects are rejected for both requests, so
the private ZIP and short-lived bearer values cannot be forwarded to another
origin. Upload and status bearer values remain request-scoped in memory and are
never persisted or logged. Response bodies and headers are bounded, and
network/HTTP failures map to a small set of user-safe offline, timeout,
rate-limit, unavailable, rejected-bundle, expired-authorization, and invalid-
response states without displaying server bodies or token-bearing URLs.

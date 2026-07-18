# Native Support Bundle v1

The native macOS app can capture a local, privacy-safe support archive while a
worker is active or after it reaches a terminal state. Capture reads app state,
the bounded worker-output tail, and filesystem metadata. It does not signal,
pause, restart, or otherwise mutate the worker.

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
- `worker`: Worker version, active state, and whether cancellation had already
  been requested. Process and process-group identifiers remain in-memory for
  worker control and are never serialized.
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
excluded. Missing and inaccessible paths are represented explicitly.

Archive-entry sizes, truncation counters, and byte ceilings remain exact because
they enforce the local 1,500,000-byte payload and 2,097,152-byte ZIP limits;
they do not describe host storage or media files.

## Tool Tail

`WorkerProcessClient` continuously drains stderr while the process runs into a
512 KiB UTF-8 tail. This prevents pipe backpressure and makes active capture
possible. `tool-tail.txt` begins with original/retained/dropped byte counts and
a `truncated` flag before the redacted retained text.

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

The bundle never contains media bytes, screenshots, environment dumps, raw
commands, source titles, raw paths, raw process identifiers, exact media/storage
sizes, serials, or reusable file fingerprints.

## Local Foundation APIs

`ConversionViewModel.captureDiagnosticBundle(in:)` returns a
`DiagnosticBundleArtifact` without changing worker state. The artifact exposes:

- `archiveURL` and `suggestedFilename` for an upload or save panel.
- `preview` with exact included/excluded categories, entry sizes, archive size,
  and truncation notices.
- `sharingItems` for `NSSharingServicePicker` or equivalent native sharing UI.
- `saveCopy(to:overwrite:)` for Save-panel destinations.
- `removeLocalCopy()` for explicit cleanup after the caller no longer needs the
  archive.

No remote upload, consent UI, or final support action is implemented by this
contract.

## Issue #267 Integration Decisions

- Capture the archive first, then present `DiagnosticBundleArtifact.preview`.
  Do not reconstruct or separately redact payloads in the UI.
- Upload the exact bytes at `archiveURL`. The client builder has already
  enforced the 2 MiB contract.
- Upload cancellation must cancel only the upload task. It must not call
  `stopActiveWorker()` or `WorkerProcessRunning.cancel()`.
- Keep the local artifact after upload failure or cancellation and offer
  `sharingItems` and `saveCopy(to:)` as the offline fallback.
- A successful upload may remove the local copy only after the support code and
  expiry state are safely presented or persisted.
- The consent sheet can use the preview's included/excluded categories
  verbatim. An optional user description should remain a separately bounded,
  redacted service field if that feature is selected later.

# Native Worker Protocol v14

The Mac app sends protocol 14. The bundled worker also accepts protocol 13 jobs,
replies with the request's protocol version, and accepts Keep Waiting commands
in that same version. All other request, event, cancellation and control behavior
retains the [v13 contract](native-worker-protocol-v13.md). Earlier protocols remain
unsupported.

A `convert_source` request can include an optional top-level `uhd_reference`:

```json
"uhd_reference": "/Movies/Matching 4K copy.mkv"
```

The value is an absolute file path. An absent or null value keeps ordinary
conversion behavior. Protocol 13 rejects this field, and inspection, ripping
and short previews reject it in either version, so they cannot silently ignore
a requested 4K copy. The app disables short previews while a copy is attached.
The shared request example is `tests/fixtures/native_worker_convert_uhd_v14.json`.

Its video object is `{"mode":"mv_hevc","route_intent":"matching_4k"}`,
with no quality, bitrate or generated-fallback controls. The path and that intent
must appear together. Ordinary video shapes use the unchanged v13 fixtures with
the supported protocol version applied by the test readers; the new fixture
records only the changed request shape.

The worker selects `uhd_hdr_mv_hevc` before ordinary video routing and uses the
same option validation and automatic encoding quality as the CLI route. It
requires MV-HEVC, starts from the beginning, and refuses eye swapping,
frame-rate/resolution overrides, crop, AI upscale and software encoding. It
checks direct encoder support and never substitutes a generated or SDR route.
Audio and subtitle options still apply to the 3D source. See the
[matching contract](direct-pipeline-contracts.md#matched-4k-hdr10-reference) for
cut, eye, frame-rate, HDR color, left-eye mask and black-padding checks.

Converter refusal text is the `job.failed` message, displayed on the queue item.
Its error code is `uhd_reference_refused`, with `retryable: true`. The existing
Retry and Edit controls let the user reconnect, replace or remove the copy.
Editing returns the item to waiting and preserves its attempt history.
A failed item keeps its selected 4K path. Conversion options and the durable
queue save `uhdReferencePath`; old saved options without that field still load.
Profiles do not store this movie-specific path. Choosing another 3D source
clears it. Each queued movie can be edited to add, replace or remove its copy.

Swift and Python share `tests/fixtures/uhd_route_cases.json` to check acceptance
and refusal of route options. Protocol fixtures and worker tests cover both
supported request versions and version-matched event replies.

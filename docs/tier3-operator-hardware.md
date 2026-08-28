# Tier 3 Operator-Assisted Hardware Receipts

`scripts/tier3_operator_collect.py` is the guided collection boundary and
`scripts/tier3_operator_receipt.py` remains the sole evidence-writing boundary
for physical Tier 3 evidence that cannot be truthfully automated without an
operator and the declared hardware. They support:

- `usb-bluray-makemkv`;
- `protected-real-media-conversion`; and
- `vision-pro-physical-playback`.

The collector derives arm64/macOS version and build, public USB vendor/product
IDs and transport, the installed MakeMKV version, exact committed release
identity, and bounded native-worker conversion and cleanup outcomes where those
machine signals are available. It prompts only for physical actions and Vision
Pro presentation judgments. The preview and generated answers never include
disc titles, volume names, serial numbers, local file paths, media names,
screenshots, tokens, diagnostic identifiers, or raw logs. Unknown fields and
free-form observations are rejected before anything is written.

## Answers Contract

Every answers file has these exact top-level fields:

```json
{
  "schema_version": 1,
  "case_id": "usb-bluray-makemkv",
  "disposition": "completed",
  "reason_code": "all-assertions-passed",
  "started_at": "2026-08-06T12:00:00Z",
  "completed_at": "2026-08-06T12:10:00Z",
  "environment": {
    "environment_class": "dedicated-hardware",
    "architecture": "arm64",
    "macos_version": "26.0",
    "macos_build": "25A123"
  },
  "hardware": {
    "class": "usb-bluray-drive",
    "identity": {
      "vendor_id": "1234",
      "product_id": "5678",
      "transport": "usb",
      "makemkv_version": "1.18.1"
    }
  },
  "cleanup_status": "recovered",
  "observations": {
    "drive_discovery": "detected",
    "makemkv": "installed",
    "cancellation": "recovered",
    "ejection": "ejected"
  }
}
```

Completed observations are limited to the enums implemented by the helper.
The helper derives pass/fail assertion states; the operator cannot directly
mark assertions passed. A skipped receipt uses `disposition: skipped`, an empty
`observations` object, and one of `hardware-unavailable`,
`environment-unavailable`, or `operator-cancelled`. Required public hardware
identity remains mandatory even for skipped audit records.

USB IDs are four-digit public vendor/product identifiers, never serials.
`makemkv_version` is the application version, never an executable path. Vision
Pro identity uses only `model_family`, `chip_family`, and `visionos_major`.

## Guided Collection

Run the collector from the exact release-evidence branch. The release receipt
must be committed and byte-identical to repository `HEAD`. Use the maintained
qualification controller so the release receipt is derived from the checked
release tag and the selected case is confirmed as currently
`operator_required`. The collector shows a validated public-safe preview,
including exact release identity, and writes the answers only after a final
bounded confirmation:

```sh
uv run python -m scripts.release_qualification_controller collect-operator \
  --release-tag <candidate> \
  --case-id usb-bluray-makemkv \
  --environment-class dedicated-hardware \
  --output-answers /path/out/usb-bluray-makemkv-answers.json
```

The controller validates the same checked status used by `status`, rejects a
case that is not currently due, and supports `--as-of YYYY-MM-DD` for a
reproducible expiry boundary. Exit `0` means answers were written, exit `1`
means the checked release binding or due-case precondition failed, exit `2`
means bounded collection failed, and exit `3` means the operator cancelled and
nothing was written.

`scripts.tier3_operator_collect` remains the lower-level compatibility boundary
for an explicitly approved baseline or diagnostic collection that is not
currently due. It requires the checked `--release-receipt` path directly and
does not perform the controller's due-case gate.

For `protected-real-media-conversion`, also pass the private native-worker
NDJSON stream with `--worker-events` and each app-owned temporary path with
`--cleanup-path`. Those inputs are read transiently to derive only terminal,
output-verification, and cleanup enums. Long heartbeat-heavy runs are filtered
as a bounded stream instead of retained in memory. Input content and paths are
never copied to the preview, answers, receipt, or evidence. Vision Pro collection requires
the bounded public `--vision-model-family`, `--vision-chip-family`, and
`--visionos-major` identities. USB identity and MakeMKV version overrides are
required to record a `not-detected`/`missing` failure or a skip when those facts
cannot be probed. A completed passing run still requires the overrides to match
the live environment, USB, and MakeMKV probes. The matching `--architecture`,
`--macos-version`, and `--macos-build` overrides let an explicit skip retain the
last known public target identity when the target machine itself is unavailable.
All overrides remain subject to the same closed public-identifier validation;
partial override sets are rejected.

USB discovery intentionally requires an optical marker in the public device
label to avoid treating generic storage as a Blu-ray drive. If a real drive is
not recognized, record `hardware-unavailable` with its bounded public identity
rather than claiming a successful drive probe.

Use `--skip-reason` with `environment-unavailable`, `hardware-unavailable`, or
`operator-cancelled` when collection cannot proceed. Missing, skipped, and
failed operator-only evidence remains visible and nonblocking.

## Receipt Build

For a newly published candidate, run from the idempotent
`automation/release-evidence-<tag>` branch created by the Release Evidence
workflow. Add operator receipts and their evidence-index entries to that same
branch when collection is due. Passed receipts use `status: accepted`; skipped
or failed receipts use matching `skipped` or `failed` index status. Those
outcomes remain visible in the milestone report but cannot satisfy any automated
or blocking assertion.

```sh
uv run python -m scripts.tier3_operator_receipt \
  --answers /path/out/<case-id>-answers.json \
  --release-receipt docs/release-evidence/<candidate>/release-receipt.json \
  --output-receipt /path/out/<case-id>.json \
  --evidence-directory /path/out/<case-id>-evidence
```

Passed or failed runs produce only normalized JSON summaries and their digests.
Skipped runs produce no evidence summaries. Cleanup, cancellation, ejection,
recovery, conversion completion, and subjective spatial-presentation outcomes
remain distinct bounded fields.

Physical receipts run only when risk requires them: an explicitly scheduled
first baseline, receipt expiry, mapped invalidating changes, environment or
device-family changes, or a maintainer-requested retest. The USB/MakeMKV case
retains its first-RC baseline. A Stable milestone alone does not force any of
the three physical cases, and missing, skipped, failed, or due physical evidence
does not block publication, evidence-branch reconciliation, or milestone
closeout.

## Validation

```sh
uv run python -m unittest tests.test_tier3_operator_receipt
uv run python -m unittest tests.test_tier3_operator_collect
uv run python -m unittest tests.test_release_qualification_controller
uv run python -m scripts.qualify_release_scope --validate-policy
```

Fixtures cover safe probe detection, exact prompt bounds, machine-observable
conversion and cleanup derivation, passed/failed/skipped outcomes, privacy
rejection, cancellation, and exclusive writes. Real hardware collection is
performed only when a risk trigger requires it; release closeout never requires
repeated collection solely to refresh operator ceremony.

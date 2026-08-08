# Tier 3 Operator-Assisted Hardware Receipts

`scripts/tier3_operator_receipt.py` is the maintained boundary for physical
Tier 3 evidence that cannot be truthfully automated without an operator and the
declared hardware. It supports:

- `usb-bluray-makemkv`;
- `protected-real-media-conversion`; and
- `vision-pro-physical-playback`.

The helper never records disc titles, volume names, serial numbers, local file
paths, media, screenshots, tokens, or raw logs. An answers file contains exact
bounded outcomes, public device-class identity, the declared environment, and
timestamps. Unknown fields and free-form observations are rejected before a
receipt is built.

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

## Collection

The release receipt must be committed and byte-identical to repository `HEAD`:

For a newly published candidate, run from the idempotent
`automation/release-evidence-<tag>` branch created by the Release Evidence
workflow. Add operator receipts and their evidence-index entries to that same
branch when collection is due. Passed receipts use `status: accepted`; skipped
or failed receipts use matching `skipped` or `failed` index status. Those
outcomes remain visible in the milestone report but cannot satisfy any automated
or blocking assertion.

```sh
uv run python -m scripts.tier3_operator_receipt \
  --answers /path/to/bounded-answers.json \
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
does not block publication, the evidence PR, or milestone closeout.

## Validation

```sh
uv run python -m unittest tests.test_tier3_operator_receipt
uv run python -m scripts.qualify_release_scope --validate-policy
```

Fixture receipts cover passed, failed, skipped, private-field rejection, and
missing-hardware rejection. Real hardware collection is performed only when a
risk trigger requires it; release closeout never requires repeated collection
solely to refresh operator ceremony.

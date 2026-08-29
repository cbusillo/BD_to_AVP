# Test Audit Inventory

This directory records the first two bounded, evidence-only slices for issue #554.
`classifications-v1.json` is the hand-authored source of dispositions;
`inventory-v1.json` and `inventory-v1.md` are generated, file-granular views.
The generator requires every tracked test-file and fixture row to have exactly
one literal-path classification. Unknown, duplicate, and missing paths fail the
refresh or check instead of receiving an implicit default.

## Current Disposition

- **219 total rows:** 149 test files and 70 support fixtures.
- **165 `valuable`:** direct product, release/security, workflow, protocol, and
  structured-fixture contracts with no concrete evidence of redundancy.
- **54 `accepted-cost`:** conversion/worker coordination, media and codec
  qualification, clean-machine UI, and visionOS/device evidence. Their setup
  requirements are intentional costs, not removal signals.
- **0 `replace`, `consolidate`, or `remove`:** no behavioral counterexample,
  mutation proof, or historical regression evidence supports a change.
- **High-confidence implementation candidates:** none.

The current evidence supports closing milestone #10 as **no action after its
required final review**, unless later review provides a concrete candidate with
the required proof. Neither test age, static brittleness signals, nor zero
direct string references independently justify a non-retention decision.

## Refresh And Check

From the repository root, refresh the generated artifacts with:

```sh
uv run python scripts/test_audit_inventory.py \
  --baseline-sha "$(git rev-parse HEAD)"
```

Use `--check` for the deterministic drift check:

```sh
uv run python scripts/test_audit_inventory.py --check
```

`--check` compares source inventory, lane mapping, classifications, and the
generated Markdown. It intentionally ignores the recorded baseline SHA and the
point-in-time `execution_evidence` JSON object, so it never attempts to rerun
or infer results from the current host. The committed evidence does not include
hostname, username, private paths, credentials, or device IDs.

## Classification Source

`classifications-v1.json` keeps cohorts compact while listing every member path
literally. Each cohort provides one allowed classification, a file-expanded
rationale, and IDs from the evidence catalog. The generated JSON expands these
into `classification`, `classification_rationale`, `classification_evidence`,
and `classification_cohort` on every row; generated Markdown includes the same
rationale and evidence IDs plus an evidence catalog table.

Allowed classifications:

- `valuable`: retained because it protects demonstrated behavior or an explicit
  compatibility, operational, release, security, or product contract.
- `accepted-cost`: retained with known fixture, process, hardware, media,
  operator, or device setup cost.
- `replace`, `consolidate`, and `remove`: forbidden without a focused,
  independently revertible candidate supported by behavioral, mutation-style,
  or historical-regression evidence.

## Baseline Execution Evidence

The point-in-time capture is for exact start SHA
`b4f980642c6e36140f458af3f3eaffafc9ae14fa`:

- macOS 27.0 build `26A5421a` on arm64; Xcode 27.0 build `27A5194q`.
- Python 3.12.11 / uv 0.12.3; Node 26.7.0 / npm 11.19.0.
- Python discovery passed 1,966 tests with 3 skips in 235.950 test seconds
  (241.08 seconds wall time).
- Native macOS lane passed 567 tests in 25.010 test seconds (52.17 seconds
  wall time).
- Support diagnostics passed 23 tests in 164ms test duration (2.19 seconds
  wall time).
- Matching `main` CI run `33264262845` succeeded.

Local and CI timings are not compared across runners. The generated inventory
records this evidence as a fixed artifact; it is not a claim about a later
local invocation.

## Lane Boundaries And Stop Rules

The maintained executable lanes are Python unit discovery, the
`BluRayToVisionPro` macOS unit-test scheme, and support-diagnostics checks. The
installed UI and visionOS targets are maintained documented operator/device
lanes; their absence from the CI `validate` job is an evidence boundary, not
an orphan finding.

Preserve protocol-version fixtures, release/security workflow contract tests,
operator/device tests, and hardware/media-gated tests absent concrete contrary
evidence. Do not delete or relax tests, change CI, alter Swift schemes, modify
`native_app.py` scheme selection, or touch release/signing behavior in this
audit. Stop and record no action when there is no evidence-backed candidate.

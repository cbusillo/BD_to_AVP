# Test Audit Inventory

This directory records the bounded evidence-only first slice for issue #554.
`inventory-v1.json` is the machine-readable artifact and `inventory-v1.md` is
its generated human view. The inventory is intentionally file-granular: each
tracked test file and each support fixture is a row, with no test-count target
or deletion recommendation.

## Refresh

From the repository root, refresh both generated artifacts with:

```sh
uv run python scripts/test_audit_inventory.py \
  --baseline-sha "$(git rev-parse HEAD)"
```

Use `--check` for a read-only drift check:

```sh
uv run python scripts/test_audit_inventory.py --check
```

`--check` compares the deterministic inventory and generated Markdown. It
ignores the recorded baseline reference and the `execution_evidence` section,
so a later commit, a local runner identity, or future runtime measurements do
not become falsely deterministic. Runtime timings belong in a separate future
evidence capture; they are not inferred from this inventory.

## Baseline And Evidence

- Baseline reference: `b4f980642c6e36140f458af3f3eaffafc9ae14fa`.
- Local evidence is limited to the generator running on a local macOS arm64
  runner; hostname, username, private paths, credentials, and device identity
  are intentionally not recorded.
- CI evidence records the maintained `macos-26` runner and the baseline
  planning evidence from GitHub Actions run `33264262845`; this slice does not
  re-measure durations or claim a fresh pass.
- Authoritative commands are parsed from `.github/workflows/ci.yml`, with
  Xcode target/scheme membership parsed from `macos/project.yml`:
  `uv run python -m unittest discover -s tests -t .`,
  `uv run python scripts/native_app.py test`, and
  `(cd support-diagnostics && npm run check)`.

## Lane Findings

The maintained CI lanes are Python unit discovery, the `BluRayToVisionPro`
macOS unit-test scheme, and support-diagnostics checks. The installed UI and
visionOS playback targets are configured in `macos/project.yml` and maintained
through documented operator/device lanes, but are not part of the CI `validate`
job. Their absence from CI is recorded as an evidence boundary, not silently
treated as dead code. The inventory identifies 22 Swift cases in those
operator/device lanes (5 installed-UI cases and 17 visionOS probe cases).

The following are explicitly blocked or deferred evidence requirements, not
reasons to alter tests in this slice:

- real SSIF/ISO media and a physical Blu-ray drive for disc paths;
- Tier 3 clean-machine qualification and Accessibility control for installed UI;
- a visionOS simulator for automated probe checks and a physical Apple Vision
  Pro for stereoscopic presentation evidence;
- Developer ID signing, notarization credentials, or `macos-signing` approval
  for release lanes.

No private credentials or hardware are required to generate or check this
artifact.

## Semantics And Stop Rules

`valuable` means evidence demonstrates behavior worth retaining; `accepted-cost`
means maintenance cost is understood and deliberately retained. Any other
classification, including `unclassified`, must carry a rationale. Slice 1
leaves every row `unclassified` with the fixed rationale that no disposition is
assigned. Later slices must use one file or one coherent candidate class per
independently revertible PR, and must provide a behavioral counterexample,
mutation proof, or linked historical regression for a non-retention decision.

Do not delete or relax tests, change CI, change Swift schemes, change
`native_app.py` scheme selection, touch release/signing behavior, or convert a
workflow-text contract without mutation proof. Stop and record no action when
there is no evidence-backed candidate. Two consecutive slices without a
high-confidence candidate close the candidate search rather than creating a
test-count target.

## Open Questions

1. **Which tests are actually maintained?** The three CI lanes are maintained
   automated lanes; installed UI and visionOS are maintained documented
   operator/device evidence lanes. Every row names its lane explicitly.
2. **Which tests are orphaned or unmaintained?** The 22 Swift cases outside CI
   are not unmaintained: they have documented operator/device contracts. No
   test file is currently unmapped by this inventory.
3. **What evidence is missing before disposition?** Per-suite logs, per-file
   timings, and named-runner identity are missing from this slice; real media,
   Tier 3 UI, visionOS presentation, and release credentials remain blocked
   requirements where applicable.
4. **When should a candidate be changed?** Only after a focused slice supplies
   a behavioral counterexample, mutation proof, or historical regression and
   can be independently reverted; otherwise retain the test as accepted
   maintenance cost or leave it unclassified.

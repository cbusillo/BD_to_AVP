# Video Quality Ladder Calibration

The route-relative quality ladder is intentionally a calibration contract before it is a user-facing control. Its
seven ordered steps are `Space Saver`, `Compact`, `Efficient`, `Balanced`, `Detailed`, `High Detail`, and
`Maximum Detail`. `Custom` remains separate and preserves exact route controls.

The immutable preregistration baseline is `docs/qualification/video-quality-ladder-v1.json`. It records the
production defaults that were frozen before objective selection:

- direct MV-HEVC quality `0.7`
- direct MV-HEVC with MetalFX 2x quality `0.6`
- generated MV-HEVC at `20` Mbps per eye and merge quality `75`
- file-based upscale quality `75`
- AV1 CRF `32`, retained as `Custom · CRF` while physical M5 evidence remains pending in #409

The objective result is frozen separately in
`docs/qualification/video-quality-route-table-v2.json` so the preregistered evidence bindings remain reproducible.
Mapping version 2 supports all seven ordinary-direct and MetalFX-direct positions, generated `Balanced = 20/75`
only, and file-upscale `Balanced = 75` plus `Detailed = 100`. AV1 remains exact-CRF Custom. Unsupported positions
are explicit and have no values.

The native profile schema persists all seven stable step identifiers. Runtime mapping version 2 resolves the active
route only: direct routes expose seven steps, generated exposes only Balanced, and existing-artifact file upscale
exposes Balanced and Detailed. `Custom` remains separate with independently retained exact route controls, so
profile migration does not manufacture mappings or discard expert values. Worker protocol v13 retains v12's exact direct
quality and includes generated fallback only for Balanced or Custom. Non-Balanced direct capability failure stops
before input instead of aliasing to generated Balanced. Issue #422 records historical signed qualification,
while the frozen candidate records below preserve their original pending status. Its Beta 12 publication claim
conflicts with checked-in release history; reconciliation is tracked in
[#857](https://github.com/cbusillo/BD_to_AVP/issues/857). Use exact-candidate release evidence rather than
inferring current acceptance from either historical status alone.

## Calibration History

The objective mapping in `video-quality-route-table-v2.json` came from a series of experiments: direct quality sweeps
with ordinary and MetalFX 2x mapping confirmations, file-upscale sweeps with objective mapping selection,
repeatability calibration and confirmation, and generated interaction, merge-refinement, collapse, bitrate and
full-corpus confirmation runs. The route table cites the confirmation records and corpora it was selected from; those
files stay in `docs/qualification/`.

The experiment runners and their plans were retired in #1022 once no calibration decision remained open. To repeat an
experiment, restore its runner from the commit before that change. For new measurements, use
`scripts/qualify_mv_hevc_corpus.py` and the packaged-route check below.

## Validation

Run the structural and production-anchor check with:

```bash
uv run python scripts/validate_video_quality_ladder.py
```

The normal command exits successfully when the contract is valid, even while calibration is incomplete. It emits a
review receipt with the manifest and corpus hashes, source Git state, per-target status counts, blockers, and deferred
AV1 work. CI runs this mode to prevent contract drift.

Validate the frozen objective candidate and its evidence bindings with:

```bash
uv run python scripts/validate_video_quality_route_table.py \
  --output build/qualification/video-quality-route-table-v2-receipt.json
```

This validator requires the exact mapping-version-2 values, explicit unsupported positions, immutable evidence
receipt identities, and Balanced-only pre-input fallback. A successful result means the objective route table is
complete. Its immutable status remains `candidate_pending_qualification`; signed-candidate qualification is
separate from this objective-selection record and the historical record discrepancy tracked in #857.

## Packaged Route-Table Qualification

`BD_TO_AVP_RELEASE_MVC_SOURCE` is an operator-local pointer to the registered private full-length MVC source. It is
intentionally not committed because the media and its filesystem location are private. The checked source identity is
SHA-256 `a1e3adf85f64a5a667d16ef4aa5a982183acece5af1cf7142e49da620982f97c`; the package matrix uses the retained
65.649-second representative segment with SHA-256
`da31e6ae9749897ca199f4a37a781b2be9a2d82076885efea4d0c156673bbcec`.

Build the ad-hoc qualification package and run every supported mapping through its packaged worker:

```bash
BD_TO_AVP_SUPPORT_DIAGNOSTICS_ENDPOINT=https://diagnostics.shinycomputers.com \
uv run python scripts/native_app.py package

uv run python scripts/qualify_packaged_video_quality_routes.py \
  --app "macos/build/package/3D Blu-ray to Vision Pro.app" \
  --source "$BD_TO_AVP_REPRESENTATIVE_MVC_SEGMENT" \
  --expected-source-sha256 da31e6ae9749897ca199f4a37a781b2be9a2d82076885efea4d0c156673bbcec \
  --output "$BD_TO_AVP_QUALIFICATION_ROOT/package-guided-v2/evidence.json" \
  --fixture-directory "$BD_TO_AVP_QUALIFICATION_ROOT/package-guided-v2/fixtures"
```

The harness performs `32` worker executions: full/finalized preview pairs for all seven ordinary-direct values, all
seven direct MetalFX 2x values, and generated Balanced, followed by full-only stage-6 checks for file-upscale Balanced
and Detailed. The worker does not permit previews to resume at stage 6, so those two receipts record
`preview_not_applicable` instead of overstating parity. Every emitted artifact must pass Apple media validation,
spatial-box checks, duration, beginning/middle/end seeks, exact video/audio/subtitle stream checks, and expected
dimensions. The receipt is bounded to 256 KiB and published mode `0444`; retained low/Balanced/high and upscale
fixtures are mode `0444` inside a mode-`0555` directory. Qualification requires a clean Git tree and records the
exact source commit plus SHA-256 identities for the package-quality runner, shared route verifier, route-table
validator, runtime mapping catalog, and protocol parser.

A passing package receipt advances #422 but does not establish physical Vision Pro playback, full-length runtime and
perceptual acceptance, Developer ID/notarization provenance, or signed-beta qualification.

Use the completion gate only when evaluating whether the mappings are ready to expose:

```bash
uv run python scripts/validate_video_quality_ladder.py \
  --require-complete \
  --output build/qualification/video-quality-ladder-receipt.json
```

`--require-complete` remains the historical all-seven preregistration gate and intentionally does not describe the
evidence-backed partial generated/file-upscale product. Use the version-2 route-table validator for the frozen
candidate and #422 for release qualification.

## Evidence Rules

Measured mappings must include immutable evidence, source, and fixture SHA-256 identities plus quality delta, output
size ratio, encode time, and an acceptance rationale. Values and measured quality/storage must remain monotonic within
each target. Adjacent generated mappings may hold one control steady only when the other changes in the correct
direction.

`Space Saver`, `Balanced`, and `Maximum Detail` become qualified only with physical Vision Pro evidence for stereo
presentation, beginning/middle/end seeks, and sustained playback. `Balanced` must continue to match production
defaults exactly and defines quality delta `0.0` and output-size ratio `1.0`.

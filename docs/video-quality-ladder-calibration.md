# Video Quality Ladder Calibration

The route-relative quality ladder is intentionally a calibration contract before it is a user-facing control. Its
seven ordered steps are `Space Saver`, `Compact`, `Efficient`, `Balanced`, `Detailed`, `High Detail`, and
`Maximum Detail`. `Custom` remains separate and preserves exact route controls.

The checked contract is `docs/qualification/video-quality-ladder-v1.json`. It currently records only production
defaults at `Balanced`:

- direct MV-HEVC quality `0.7`
- direct MV-HEVC with MetalFX 2x quality `0.6`
- generated MV-HEVC at `20` Mbps per eye and merge quality `75`
- file-based upscale quality `75`
- AV1 CRF `32`, retained as `Custom · CRF` while physical M5 evidence remains pending in #409

Every other numeric mapping is `needs_calibration`; the manifest rejects values attached to that status. This keeps
the UI and profile model from shipping guessed mappings.

## Direct Quality Sweep

`docs/qualification/direct-mv-hevc-quality-sweep-coarse-v1.json` defines a seven-point exploratory grid from quality
`0.4` through `1.0`. Candidate IDs are measurements, not ladder step assignments. The sweep always includes and
freshly measures the production `Balanced` quality `0.7` in the same run as every candidate.

After the coarse sweep identifies the practical upper range,
`docs/qualification/direct-mv-hevc-quality-sweep-upper-v1.json` narrows measurement to `0.7`, `0.75`, `0.8`, `0.82`,
and `0.85`. The previously measured `0.9` and `1.0` points remain rejection-bound evidence because their storage
growth is disproportionate to their quality gain. The upper plan is still exploratory and cannot assign ladder steps.

Run the complete quality-gated corpus with the private source supplied only through its environment variable:

```bash
BD_TO_AVP_RELEASE_MVC_SOURCE=/private/source.mkv \
uv run python scripts/qualify_direct_mv_hevc_quality_sweep.py \
  --output build/qualification/direct-mv-hevc-quality-coarse-v1.json \
  --work-directory build/qualification/direct-mv-hevc-quality-coarse-v1-work
```

Select the checked upper-range plan with:

```bash
BD_TO_AVP_RELEASE_MVC_SOURCE=/private/source.mkv \
uv run python scripts/qualify_direct_mv_hevc_quality_sweep.py \
  --sweep-plan docs/qualification/direct-mv-hevc-quality-sweep-upper-v1.json \
  --output build/qualification/direct-mv-hevc-quality-upper-v1.json \
  --work-directory build/qualification/direct-mv-hevc-quality-upper-v1-work
```

The script requires a clean worktree, records exact source/tool/corpus identities, writes evidence atomically after
every encode, and supports `--resume`. It measures only quality-gated cases; the informational ITU conformance vector
does not influence quality calibration. A subset selected with `--case-id` is useful for smoke testing but cannot pass
the full-corpus acceptance field.

The output reports conservative per-candidate quality delta, size ratio, encode time, repeat noise, eye-order margin,
and monotonicity warnings. It never edits the ladder contract or selects public steps. Historical generated-route or
MetalFX evidence cannot replace this same-run direct `Balanced` comparison.

Exit `0` means the selected cells completed and passed eye-order checks; a subset smoke can therefore succeed while
remaining explicitly non-qualifying. A complete corpus exits `1` when monotonicity or repeat-noise warnings require
review, and fatal tool, source, ownership, or evidence errors exit `2`.

## Validation

Run the structural and production-anchor check with:

```bash
uv run python scripts/validate_video_quality_ladder.py
```

The normal command exits successfully when the contract is valid, even while calibration is incomplete. It emits a
review receipt with the manifest and corpus hashes, source Git state, per-target status counts, blockers, and deferred
AV1 work. CI runs this mode to prevent contract drift.

Use the completion gate only when evaluating whether the mappings are ready to expose:

```bash
uv run python scripts/validate_video_quality_ladder.py \
  --require-complete \
  --output build/qualification/video-quality-ladder-receipt.json
```

`--require-complete` fails until every non-AV1 target has all seven qualified mappings and ladder exposure is enabled.
AV1 may remain exact-CRF-only; enabling its ladder instead requires all seven qualified mappings and physical anchor
evidence.

## Evidence Rules

Measured mappings must include immutable evidence, source, and fixture SHA-256 identities plus quality delta, output
size ratio, encode time, and an acceptance rationale. Values and measured quality/storage must remain monotonic within
each target. Adjacent generated mappings may hold one control steady only when the other changes in the correct
direction.

`Space Saver`, `Balanced`, and `Maximum Detail` become qualified only with physical Vision Pro evidence for stereo
presentation, beginning/middle/end seeks, and sustained playback. `Balanced` must continue to match production
defaults exactly and defines quality delta `0.0` and output-size ratio `1.0`.

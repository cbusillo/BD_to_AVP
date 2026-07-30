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

## Generated Interaction Sweep

`docs/qualification/generated-mv-hevc-corpus-v1.json` binds a four-case stress subset to the existing direct
MV-HEVC corpus by path, corpus ID, and SHA-256. It reuses the exact source segments without duplicating their private
source paths, and pins each prepared segment or synthetic-filter identity from the completed direct sweep. The subset
covers darkness, grain, motion, animation, and disparity; it is an interaction-search corpus rather than the final
eight-case qualification corpus.

`docs/qualification/generated-mv-hevc-calibration-sweep-v1.json` defines a checked `3x3` experiment around the
production `Balanced` default of `20` Mbps per eye plus merge quality `75`. The initial levels are `16`, `20`, and
`24` Mbps per eye crossed with merge quality `65`, `75`, and `85`. Every cell runs three times with a checked cyclic
schedule that places each cell in the early, middle, and late third once, exposing repeat noise without confounding
one setting with thermal order.

The experiment also pins `vendor/ffmpeg-macos-arm64.toml` and refuses PATH-selected media tools. Install the checked
FFmpeg and FFprobe binaries into the task worktree before running:

```bash
uv run python -m scripts.vendor_ffmpeg_macos
```

Run the complete checked stress experiment with:

```bash
BD_TO_AVP_RELEASE_MVC_SOURCE=/private/source.mkv \
uv run python scripts/qualify_generated_mv_hevc_calibration.py \
  --output build/qualification/generated-mv-hevc-interaction-v1.json \
  --work-directory build/qualification/generated-mv-hevc-interaction-v1-work
```

Use repeatable `--case-id` options for a bounded smoke. A subset can prove execution and structural validity, but it
cannot claim the planned stress corpus. The runner requires a clean worktree and HEAD-identical checked manifests,
records exact source, tool, macOS-build, and Apple hardware identities, uses a single-writer lock, writes evidence
atomically after every encode, supports `--resume`, and freezes a completed canonical receipt read-only.

The receipt includes the fresh `20/75` repeatability baseline, every raw per-eye SSIM and eye-order measurement,
per-frame minimum and fifth-percentile SSIM, temporal quality variation and sudden-drop evidence, output size, runtime,
axis findings, and adjacent `2x2` interaction observations. Findings remain descriptive: this experiment neither
chooses thresholds nor assigns ladder steps. Its measured noise floor and interaction evidence must be pinned in a
later refinement plan before seven generated mappings are selected.

## Generated Merge Refinement

`docs/qualification/generated-mv-hevc-merge-refinement-v1.json` pins the next adaptive stage to the completed
interaction receipt SHA-256 `fe3c81e96771f9d0f4dc1f6461556d5fdf22c95034776b044f268978d68bd07f`. It holds
the production eye bitrate at `20` Mbps and measures merge quality `65`, `68`, `71`, `75`, `79`, `82`, and `85`
three times on the same four-case stress corpus. This maps the dominant control before any per-tier bitrate search;
it is not a seven-step product mapping.

The plan pre-registers conservative thresholds at twice the worst measured `Balanced` repeat spread, rounded upward:
`0.0006` aggregate SSIM for non-inferiority and distinguishability, `0.02` for storage separation and repeat-size
spread, plus checked per-frame minimum, fifth-percentile, temporal-variation, and sudden-drop limits. A `3.0x`
output-size cap prevents an extreme merge setting from becoming technically eligible. The runner records every
per-case threshold result and adjacent response, marks ambiguous pairs for collapse or blinded review, and keeps
`ladder_evidence_ready` and `ladder_mapping_selected` false.

After committing the plan and runner in a clean worktree, run the checked refinement with a new output and work
directory:

```bash
BD_TO_AVP_RELEASE_MVC_SOURCE=/private/source.mkv \
uv run python scripts/qualify_generated_mv_hevc_calibration.py \
  --experiment-plan docs/qualification/generated-mv-hevc-merge-refinement-v1.json \
  --source-evidence-receipt /private/full-stress-receipt.json \
  --output build/qualification/generated-mv-hevc-merge-refinement-v1.json \
  --work-directory build/qualification/generated-mv-hevc-merge-refinement-v1-work
```

The refinement receipt may identify technically eligible cells and objectively separable adjacent responses. It may
not assign public quality steps. The next checked stage searches downward for the Pareto-minimal per-eye bitrate at
each viable merge tier, followed by full eight-case confirmation and any required blinded review.

The refinement command exits `0` only when the complete stress corpus passes and every adjacent response clears all
pre-registered per-case quality and storage thresholds. It exits `1` with a complete immutable receipt when a pair
must be collapsed or escalated to blinded review, when no cell remains technically eligible, or when only a subset
was run. Fatal identity, source, tool, privacy, or evidence errors exit `2`.

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

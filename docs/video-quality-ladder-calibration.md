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

The native profile schema may persist all seven stable step identifiers before they are exposed. Runtime mapping
version 1 resolves only the checked `Balanced` defaults across current MV-HEVC targets; unsupported step selections
fail closed. `Custom` remains a separate mode with independently retained exact route controls, so profile migration
does not manufacture mappings or discard expert values. Worker protocol v11 sends only the requested route controls
plus a concrete generated fallback when direct capability selection can require one. The worker validates the
metadata against checked mappings and reports both requested and selected route settings.

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

## File Upscale Quality Sweep

`docs/qualification/file-upscale-quality-corpus-v1.json` binds a five-case stress subset to the existing direct
MV-HEVC corpus by exact path, corpus ID, and SHA-256. The subset covers real MVC footage, motion, grain, darkness,
animation, crop handling, disparity, frame-rate override, and 8-bit source material. Private source identity is pinned
from the direct anchor plan where the production-derived cases require it.

`docs/qualification/file-upscale-quality-sweep-v1.json` defines a checked exploratory sweep for the file-based
`upscale_quality` control only. It measures integer qualities `65`, `75`, and `85`, with production `Balanced = 75`
resolved from `DEFAULT_UPSCALE_QUALITY`. Each candidate runs three repeats in cyclic orders `[65,75,85]`,
`[75,85,65]`, and `[85,65,75]`. For every case and repeat, the runner creates one fresh production-parity generated
base at `20` Mbps per eye with merge quality `75`, then runs all three upscale candidates against exact copies of
that same base.

Run the complete checked stress subset from a clean committed worktree with:

```bash
BD_TO_AVP_RELEASE_MVC_SOURCE=/private/source.mkv \
uv run python scripts/qualify_file_upscale_quality_sweep.py \
  --output build/qualification/file-upscale-quality-sweep-v1.json \
  --work-directory build/qualification/file-upscale-quality-sweep-v1-work
```

Use `--resume` with the same output and work directory after an interrupted run. A bounded smoke can select one or
more planned cases, but it remains non-qualifying even when structurally valid:

```bash
BD_TO_AVP_RELEASE_MVC_SOURCE=/private/source.mkv \
uv run python scripts/qualify_file_upscale_quality_sweep.py \
  --case-id synthetic-animation \
  --output build/qualification/file-upscale-quality-sweep-smoke.json \
  --work-directory build/qualification/file-upscale-quality-sweep-smoke-work
```

The runner uses the same `fx_upscale_command(input_path, quality)` helper as production file upscale, records the
canonical quality factor as `quality / 100`, validates 2x spatial output, downsamples each final eye back to source
dimensions for aggregate and per-frame SSIM, and records cross-eye margins, frame-tail statistics, bytes, effective
bitrate, base ratio, upscale-only time, projected full-route time, source/base/final hashes, and source/tool/Git
provenance. The checked timing contract requires the pinned FFprobe `r_frame_rate` signature and decoded frame count to
match the source, checks integer stream duration within one source frame, and records the independently derived average
frame rate without requiring it to textually equal the nominal rate. Effective bitrate uses the validated container
duration. Evidence is written atomically
after every base and candidate, and work directories are owned and locked for safe resume.

Exit `0` means the complete planned stress subset is structurally decision-ready with valid eye order and monotonic
output-size response. Exit `1` means the complete planned stress subset produced valid but ambiguous or non-monotonic
exploratory evidence. Fatal schema, source, tool, provenance, ownership, privacy, or execution errors exit `2`.
Subset or otherwise incomplete resumable evidence exits `3`.

This slice cannot assign public ladder mappings, alter `docs/qualification/video-quality-ladder-v1.json`, choose
thresholds after seeing results, or claim perceptual 4K or Vision Pro playback quality. Passing evidence only
characterizes the independent file-upscale response around production `Balanced = 75`. SSIM deltas and repeat spread
remain descriptive in this stage; they are not converted into an undeclared pass/fail threshold. Public mappings still
require a later checked calibration and physical-device validation.

## File Upscale Objective Mapping Selection

`docs/qualification/file-upscale-quality-corpus-v2.json` selects exactly the seven quality-gated cases from the direct
MV-HEVC corpus in manifest order. It excludes informational `itu-mvcds-2`, reuses the checked v1 source identities,
and adds the pinned four-second snow-detail and motion segments. The objective stage is bound to the immutable v1
response receipt SHA-256 `d62f038afa796f7404bd47dabc6f84cfa47ba6e221b32a501ebc4314714c9bb6`, its source
Git SHA and response-plan SHA, the corpus-v2 SHA, the unchanged public ladder and `VideoQuality.swift` bytes, and the
same pinned production tools.

`docs/qualification/file-upscale-quality-mapping-selection-v1.json` preregisters integer qualities
`[45,55,65,75,85,95,100]`; q75 remains the non-substitutable production `Balanced` anchor and q100 uses canonical
bitrate factor `1`. Each case runs three fresh production-parity bases at `20` Mbps per eye plus merge quality `75`.
All seven candidates receive exact copies of their paired base. The plan materializes every per-case/per-repeat order
from the three checked permutations and rotations; the runner never shuffles at runtime.

Run the complete objective stage from a clean committed worktree with the frozen response receipt available at mode
`0444`:

```bash
BD_TO_AVP_RELEASE_MVC_SOURCE=/private/source.mkv \
uv run python scripts/qualify_file_upscale_quality_mapping_selection.py \
  --source-receipt build/qualification/file-upscale-quality-sweep-v1.json \
  --output build/qualification/file-upscale-quality-mapping-selection-v1.json \
  --work-directory build/qualification/file-upscale-quality-mapping-selection-v1-work \
  --artifact-directory build/qualification/file-upscale-quality-mapping-selection-v1-artifacts
```

Use `--resume` with all four paths unchanged after an interruption. Completed receipts are canonical JSON frozen to
`0444`, including complete objective failures. The owned artifact directory preserves the generated base and all seven
candidate outputs for preregistered repeat 1 of dark, grain-rain, snow-detail, and motion. Receipt artifact paths are
relative and include byte counts and SHA-256 identities; private source paths are forbidden. Other candidate outputs
are deleted after their measurements are durably recorded.

The runner recomputes source-noise maxima only from raw v1 records grouped by `(case_id,candidate_id)`. It explicitly
forbids the defective frozen aggregate `candidate_summaries[*].repeat_ssim_spread` field and verifies every checked
limit with `ceil(2 * source_maximum / quantum) * quantum`. New summaries expose per-case repeat ranges, each
candidate's maximum within-case repeat range, and cross-case median ranges; they do not emit a misleading aggregate
`repeat_ssim_spread`. The old immutable response receipt is not rewritten.

A candidate is technically eligible only when all 21 planned records pass structure, timing, 2x geometry, eye order,
exact-copy and hash provenance, every preregistered repeatability limit, and
`final_to_base_size_ratio <= 4.10`. Every lower/higher pair is evaluated, including non-adjacent pairs:

- storage must increase strictly in every paired repeat, and every case's median paired growth must be at least `0.02`;
- per-case median paired quality deltas must keep aggregate SSIM at least `-0.0002`, minimum-frame SSIM at least
  `-0.0016`, P05 SSIM at least `-0.0012`, frame-SSIM standard-deviation increase at most `0.0002`, adjacent-drop
  increase at most `0.0010`, and eye-order-margin loss at most `0.0011`;
- objective distinction requires corpus-median aggregate improvement of at least `0.0002` and at least two real cases
  clearing the aggregate or tail threshold, including grain-rain or snow-detail.

Failed boundaries collapse without threshold changes, interpolation, aliases, or post-outcome candidates. The
analyzer selects the deterministic maximum-cardinality ordered subset containing q75, then tie-breaks by minimum-case
storage coverage, objective-quality margin, storage margin, end-to-end storage coverage, lower first quality, and
candidate IDs. Surviving values are assigned outward from q75 to `Efficient`, `Compact`, and `Space Saver`, then
`Detailed`, `High Detail`, and `Maximum Detail`. Missing slots remain explicitly `unsupported`; no value is duplicated.

Exit `0` means the complete, structurally valid objective evidence selects all seven provisional candidates and is
decision-ready for this bounded stage. Exit `1` means complete immutable evidence collapsed a boundary, produced a
sparse or ambiguous selection, or otherwise lacked sufficient objective distinction. Fatal contract, provenance,
privacy, tool, or source errors exit `2`; incomplete resumable evidence exits `3`. Even exit `0` keeps
`public_mapping_changes_forbidden=true` and `ladder_mapping_selected=false` and does not edit the public ladder or
`VideoQuality.swift`.

Perceptual/blinded review, long-form runtime, packaged-app parity, and Vision Pro playback remain explicit downstream
checks. They are not performed by this objective stage and do not block its exit `0`; later public ladder exposure must
bind those separate receipts and still pass the normal completion gate.

## File Upscale Repeatability Calibration v2

`docs/qualification/file-upscale-quality-repeatability-calibration-v2.json` is a calibration-only response to the
completed mapping-selection receipt. It binds that schema-3 receipt at mode `0444`, exact SHA-256
`c8e2478913a8c458657f0f7904720d6f76e8761b8ba1922e7c5dda5b916d2cef`, and source Git SHA
`b93a9729a2396b3942e679a1a8db34967f9d4467`. It also binds the mapping-selection plan SHA-256
`3aa76c79adb81e72dd89f9fd548ef73698880eebf6332c149fe401c058d090ee`, its frozen repeatability limits and
quanta, corpus-v2 and all seven quality-gated cases in manifest order, and the unchanged production tools, public
ladder, and `VideoQuality.swift` bytes. The predecessor receipt must be supplied through the required
`--mapping-selection-receipt` CLI path; its raw records are never a calibration-threshold source.

The v2 run measures production `Balanced` q075 only. Every case receives five fresh generated bases at `20` Mbps per
eye plus merge quality `75`, and q075 receives one exact hash-verified copy of each paired base. The complete
seven-case/five-repeat/one-candidate schedule is materialized in the plan, and runtime shuffling is forbidden. The
runner preserves the mapping-selection structure, timing, 2x geometry, hash provenance, eye-order, `4.10` size-cap,
and raw metric-record checks.

Run the checked calibration from a clean committed macOS arm64 worktree with the accepted predecessor receipt
available at the supplied relative path:

```bash
BD_TO_AVP_RELEASE_MVC_SOURCE="$SOURCE_MVC" \
uv run python scripts/qualify_file_upscale_quality_repeatability_calibration.py \
  --mapping-selection-receipt build/qualification/file-upscale-quality-mapping-selection-v1.json \
  --output build/qualification/file-upscale-quality-repeatability-calibration-v2.json \
  --work-directory build/qualification/file-upscale-quality-repeatability-calibration-v2-work \
  --artifact-directory build/qualification/file-upscale-quality-repeatability-calibration-v2-artifacts
```

Use `--resume` with the same five paths after interruption. Checkpoints are canonical JSON written atomically under
single-writer and ownership markers. An incomplete receipt remains writable and resumable; a complete canonical
receipt is finalized at mode `0444`, including recovery when a crash occurs between the final write and freeze.
Completed work state is removed, unexpected work entries are rejected, and the artifact directory must contain
exactly eight hash-bound relative-path MOVs: the repeat-index-0 generated base and q075 output for dark, grain-rain,
snow-detail, and motion. Missing or orphan media is fatal.

For each frozen repeatability field, derivation reads only the 35 raw q075 candidate records. It computes each case's
five-repeat range, selects the maximum range across cases, and records the source case and q075 candidate. The new
limit is `max(previous_limit, ceil(2 * observed_maximum / quantum) * quantum)`, with the previous limit, observed
maximum, multiplier, quantum, and derived limit preserved in the receipt.

This stage performs no candidate selection, boundary evaluation, provisional mapping, or public contract change.
Its derived limits must be pinned by a separate later confirmation plan before any public ladder decision. Exit `0`
requires a complete, finalized, structurally valid calibration receipt with all derived limits and eight retained
artifacts. Incomplete resumable evidence exits `3`; contract, provenance, privacy, ownership, media, or execution
failures exit `2`.

## File Upscale Objective Mapping Confirmation v2

`docs/qualification/file-upscale-quality-mapping-confirmation-v2.json` is the separate confirmation plan that consumes
the accepted repeatability-calibration receipt without consuming candidate outcomes from that receipt's predecessor
mapping-selection run. It binds calibration schema `4`, experiment
`file-upscale-quality-repeatability-calibration-v2`, receipt SHA-256
`6d44f4c23df142d3a819f0aba1b87f9fa688435485f4f1798a103ea94ccbe49e`, source Git SHA
`1f988fbf198595d52084eabc3055edd2f1d14221`, and mode `0444`. It also binds
`docs/qualification/file-upscale-quality-repeatability-calibration-v2.json` at schema `1` and SHA-256
`c4cf953bd868eadd04f4ed11a7ca4f2211c81f5ee72f375347f5f3d9cf14ecdb`. The engine verifies the calibration's
accepted and finalized calibration-only scope, raw-q075 derivation, predecessor-record isolation, exact derived
metrics, and `later_confirmation.status = not_performed` before starting confirmation work.

Run the confirmation from a clean committed macOS arm64 worktree with the frozen calibration receipt supplied through
the generic checked-source option:

```bash
BD_TO_AVP_RELEASE_MVC_SOURCE="$SOURCE_MVC" \
uv run python scripts/qualify_file_upscale_quality_mapping_selection.py \
  --selection-plan docs/qualification/file-upscale-quality-mapping-confirmation-v2.json \
  --source-receipt build/qualification/file-upscale-quality-repeatability-calibration-v2.json \
  --output build/qualification/file-upscale-quality-mapping-confirmation-v2.json \
  --work-directory build/qualification/file-upscale-quality-mapping-confirmation-v2-work \
  --artifact-directory build/qualification/file-upscale-quality-mapping-confirmation-v2-artifacts
```

The confirmation keeps corpus-v2, qualities `[45,55,65,75,85,95,100]`, q075 as `Balanced`, the `20` Mbps-per-eye
plus merge-quality-`75` generated base, three fresh paired repeats, the checked materialized schedules, exact copied
bases, pinned tools and public contracts, all-pair boundary evaluation, deterministic selection/tie-breaks, and
unsupported missing slots. It retains exactly 32 relative-path, hash-bound MOVs: one generated base plus all seven
candidate outputs for repeat index `0` of dark, grain-rain, snow-detail, and motion.

The calibrated technical repeatability limits are separate from boundary distinction policy:

- technical within-case repeat ranges are at most `0.0002` aggregate SSIM, `0.03` final/base size ratio, `0.0054`
  minimum-frame SSIM, `0.0019` P05 SSIM, `0.0002` frame-SSIM standard deviation, `0.0058` maximum adjacent-frame
  SSIM drop, and `0.0011` eye-order margin;
- paired storage still must increase strictly in every repeat, and every case's median paired growth must remain at
  least `0.02`; the calibrated `0.03` size-repeatability limit does not replace this storage boundary;
- quality non-inferiority remains aggregate `-0.0002`, minimum-frame `-0.0054`, P05 `-0.0019`, frame-standard-
  deviation increase `0.0002`, adjacent-drop increase `0.0058`, and eye-margin loss `0.0011`;
- objective distinction requires corpus-median aggregate improvement `0.0002`, two real cases, real-case aggregate
  `0.0002`, minimum-frame `0.0054`, or P05 `0.0019`, with grain-rain and snow-detail as the sensitive cases and at
  least one of them clearing a distinction threshold.

Exit `0` requires complete evidence, q075 technical eligibility, all 21 pairwise boundaries, and all seven candidates
selected. A complete negative confirmation exits `1`; incomplete resumable evidence exits `3`; fatal contract,
provenance, privacy, ownership, media, or execution failures exit `2`. The confirmation records provisional objective
results only and cannot change public mappings.

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

Aggregate and per-frame non-inferiority to `Balanced` applies only to `Balanced` and higher merge-quality candidates:
a higher-quality choice must not improve the average while regressing tail or temporal quality. Lower merge-quality
candidates intentionally trade quality for storage, so they remain eligible when structural, eye-order, temporal,
repeatability, storage-direction, and size-cap gates pass. Their quality loss must still be monotonic and objectively
separable before a public step can use them. The later bitrate-minimization stage applies same-tier non-inferiority
against each accepted merge tier's `20 Mbps/eye` anchor.

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

## Generated Collapse Analysis

`docs/qualification/generated-mv-hevc-collapse-analysis-v1.json` binds the completed refinement receipt and its
source plan by SHA-256. `scripts/qualify_generated_mv_hevc_collapse.py` performs no encoding and cannot modify the
ladder. It verifies the frozen read-only source receipt, unchanged thresholds, source Git and plan identities,
successful refinement gates, technically eligible cells, and production `Balanced = 20/75` before analysis.

The analyzer evaluates every ordered boundary between technically eligible cells, including wider non-adjacent
boundaries, with the same pre-registered every-case quality and storage thresholds. This is a collapse operation over
existing measurements, not a new threshold search: skipped interior cells are not promoted or reclassified. It then
selects the maximum-cardinality ordered subset containing `Balanced`. Deterministic ties prefer wider guaranteed
storage coverage, larger minimum quality and storage margins, a lower first merge quality, then lexicographic cell
IDs.

Run the checked analysis from a clean commit with:

```bash
uv run python scripts/qualify_generated_mv_hevc_collapse.py \
  --source-receipt /private/generated-mv-hevc-merge-refinement-receipt.json \
  --output build/qualification/generated-mv-hevc-collapse-analysis-v1.json
```

The output records every evaluated boundary and valid subset but never records the private source path. It exits `0`
only if seven technically defensible cells survive. It exits `1` with a frozen immutable receipt when fewer survive,
setting `product_decision_required` and leaving bitrate search and ladder selection false. Fatal provenance, schema,
privacy, threshold, Balanced, or evidence inconsistencies exit `2`.

## Generated Bitrate Minimization

`docs/qualification/generated-mv-hevc-bitrate-search-v1.json` records the post-collapse product decision: generated
MV-HEVC has two guided anchors, merge `65` and `75`, plus exact reversible `Custom`. It binds the frozen merge
refinement and collapse receipts, the checked collapse plan, unchanged noise-derived thresholds, the four-case stress
corpus, and the production generated encoder/toolchain identities.

The checked design measures every integer bitrate from `1` through `20` Mbps per eye at both accepted merge tiers,
three times per cell. The exhaustive `40`-cell, `480`-encode stress sweep avoids interpolation and post-hoc refinement.
A checked three-group Latin schedule places every cell, including both `20 Mbps/eye` anchors, exactly once in early,
middle, and late execution windows across the three repeats. Every lower candidate is compared only with the anchor at
the same merge quality.

The technical frontier excludes storage benefit so an encoder cap that does not bind cannot create an artificial
fail/pass inversion. Every candidate must pass aggregate, minimum-frame, fifth-percentile, temporal-stability,
repeatability, eye-order, artifact-structure, and per-case size non-regression gates. The pass sequence must be a
single fail-then-pass frontier. The lowest technical pass is adopted only when total median bytes across the complete
stress subset improve by at least the checked `2%`; otherwise the same-tier `20 Mbps/eye` anchor remains selected.
The receipt fails closed on incomplete cases, a failing anchor, a non-monotone frontier, changed source evidence, or
changed thresholds.

After committing the plan and runner in a clean worktree, run:

```bash
BD_TO_AVP_RELEASE_MVC_SOURCE=/private/source.mkv \
uv run python scripts/qualify_generated_mv_hevc_calibration.py \
  --experiment-plan docs/qualification/generated-mv-hevc-bitrate-search-v1.json \
  --source-evidence-receipt /private/generated-mv-hevc-merge-refinement-receipt.json \
  --collapse-receipt /private/generated-mv-hevc-collapse-analysis-receipt.json \
  --output build/qualification/generated-mv-hevc-bitrate-search-v1.json \
  --work-directory build/qualification/generated-mv-hevc-bitrate-search-v1-work
```

The command exits `0` only when both integer frontiers are decision-ready on the complete checked stress subset. It
exits `1` with a frozen receipt for a subset run or an unresolved frontier, and `2` for fatal source, plan, tool,
privacy, schema, or evidence errors. Even exit `0` keeps `ladder_mapping_selected=false`: the selected stress minima
still require full eight-case confirmation, packaged-app validation, and physical Vision Pro playback before the
generated route table can freeze.

## Generated Full-Corpus Confirmation

`docs/qualification/generated-mv-hevc-full-corpus-confirmation-v1.json` binds the frozen bitrate-search receipt and
its checked schema-v3 plan. It does not search or select replacements. The fixed cells are lower bracket `11/65`,
selected `12/65`, and anchor `20/65`, plus lower bracket `12/75`, selected `13/75`, and anchor `20/75`. Each cell is
measured three times on all eight direct-corpus cases for exactly `144` encodes. A two-cell cyclic rotation places
every cell once in the early, middle, and late execution thirds.

The seven quality-gated cases determine confirmation. Each selected cell must pass the unchanged same-tier aggregate,
frame-tail, temporal, repeatability, eye-order, structure, and per-case size gates. Each lower bracket must reproduce
an aggregate quality rejection below `-0.0006` on at least one gated case. Selected storage passes only when exact
integer totals satisfy `100 × selected bytes <= 98 × anchor bytes`; equality passes. The public ITU `MVCDS-2` case is
required for execution completeness and artifact validity, but its quality metrics and bytes are informational and
cannot affect confirmation.

After committing the plan and runner in a clean worktree, run:

```bash
BD_TO_AVP_RELEASE_MVC_SOURCE=/private/source.mkv \
BD_TO_AVP_ITU_MVC_VECTOR=/private/MVCDS-2.264 \
uv run python scripts/qualify_generated_mv_hevc_calibration.py \
  --experiment-plan docs/qualification/generated-mv-hevc-full-corpus-confirmation-v1.json \
  --source-evidence-receipt /private/generated-mv-hevc-bitrate-search-receipt.json \
  --output build/qualification/generated-mv-hevc-full-corpus-confirmation-v1.json \
  --work-directory build/qualification/generated-mv-hevc-full-corpus-confirmation-v1-work
```

The command rejects `--case-id` subsets. Once all `144` records exist, it freezes the canonical receipt read-only
whether confirmation passes or fails. Exit `0` requires both fixed tiers to confirm; a completed fail-closed result
exits `1`, and fatal provenance, source, tool, privacy, schema, or execution errors exit `2`. No result changes the
fixed cells or sets `ladder_mapping_selected=true`. A passing receipt proceeds to packaged-app validation and then
physical Vision Pro playback.

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
